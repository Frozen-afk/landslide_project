"""FastAPI server: upload photos -> reconstruct -> mark -> measure volume.

Jobs survive a server restart: status/log/scale/result are persisted to
<job>/state.json and the reconstruction context is rebuilt lazily from the
cached COLMAP database when a job is touched again, so a browser refresh or
server upgrade mid-session doesn't lose work.
"""
from __future__ import annotations

import os

# bound glibc per-thread arenas before cv2/pycolmap load (see landslide/sfm.py)
os.environ.setdefault("MALLOC_ARENA_MAX", "4")

import gc
import json
import shutil
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landslide.densify import dense_cloud  # noqa: E402
from landslide.pipeline import import_photos, measure  # noqa: E402
from landslide.scaling import aruco_scale, manual_scale  # noqa: E402
from landslide.sfm import IMAGE_EXTS, count_photos, image_metadata, reconstruct  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data" / "jobs"
STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHOTOS = 200          # per job
MAX_FILE_MB = 80          # per uploaded photo
MAX_LOADED_CTX = 2        # reconstructions held in RAM (LRU-evicted, reloadable);
                          # each ctx carries a multi-million-point dense cloud
MAX_PHOTO_CACHE = 150     # rendered photo thumbnails kept in RAM


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_persisted_jobs()
    yield


app = FastAPI(title="Landslide Volume from Phone Photos", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """The page and its JS/CSS must never be served from browser cache —
    a stale app.js against a fresh index.html (or vice versa) breaks the
    upload wiring with no visible error."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

EXEC = ThreadPoolExecutor(max_workers=1)
JOBS_LOCK = threading.Lock()


class Job:
    def __init__(self, job_id: str, created: float | None = None,
                 status: str = "reconstructing", error: str | None = None,
                 log: list[str] | None = None, scale_info: dict | None = None,
                 result: dict | None = None, ortho: dict | None = None):
        self.id = job_id
        self.dir = DATA_DIR / job_id
        self.status = status          # reconstructing|ready|measuring|error
        self.error = error
        self.log = log or []
        self.created = created or time.time()
        self.ctx = None               # ReconCtx; rebuilt lazily from disk
        self.result = result
        self.scale_info = scale_info  # mirror of ctx.scale_info, persisted
        self.ortho = ortho            # orthophoto metadata, persisted
        self.lock = threading.Lock()

    # ---------- persistence ----------
    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def save_state(self) -> None:
        if self.ctx is not None and self.ctx.scale_info.get("applied"):
            self.scale_info = self.ctx.scale_info
        state = {
            "status": self.status, "error": self.error, "created": self.created,
            "log": self.log[-400:],
            "scale_info": (lambda s: {k: v for k, v in s.items() if k != "marker_px"}
                           if s else None)(self.scale_info),
            "result": self.result, "ortho": self.ortho,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_path)

    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log.append(line)
        self.log = self.log[-400:]
        print(line, flush=True)

    def set_status(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.save_state()

    # ---------- reconstruction context ----------
    @property
    def reconstructable(self) -> bool:
        """True once incremental mapping has written at least one model."""
        sparse = self.dir / "work" / "sparse"
        if not (self.dir / "work" / "database.db").exists() or not sparse.is_dir():
            return False
        return any(f.is_file() for sub in sparse.iterdir() if sub.is_dir()
                   for f in sub.iterdir())

    def ensure_ctx(self):
        """Return the ReconCtx, rebuilding it from the on-disk cache if needed.

        The ctx holds numpy arrays and the COLMAP model (~tens of MB); keeping
        every historical job in RAM forever is wasteful, so old contexts are
        dropped and rebuilt on demand.
        """
        with self.lock:
            if self.ctx is not None:
                _touch(self)
                return self.ctx
            if not self.reconstructable:
                raise HTTPException(
                    409, "this job has no cached reconstruction (it may have "
                    "failed or been interrupted before finishing) — re-upload "
                    "the photos")
            self.say("loading cached reconstruction…")
            try:
                ctx = reconstruct(self.dir / "photos", self.dir / "work",
                                  reuse=True, log=self.say)
            except Exception as e:
                self.say(f"failed to reload reconstruction: {e}")
                raise HTTPException(500, f"cannot reload reconstruction: {e}")
            # re-apply the scale that was set in a previous session
            if self.scale_info and self.scale_info.get("applied"):
                ctx.scale = self.scale_info["scale"]
                ctx.scale_info = self.scale_info
            self.ctx = ctx
            _evict_ctx()
            return ctx

    def snapshot(self) -> dict:
        with self.lock:
            out = {
                "id": self.id, "status": self.status, "error": self.error,
                "created": self.created, "log": self.log[-60:],
                "reconstructable": self.reconstructable,
                "ortho": self.ortho,
            }
            if self.ctx is not None:
                out["images"] = image_metadata(self.ctx)
                out["scale"] = self.ctx.scale_info or None
            else:
                out["images"] = []
                out["scale"] = self.scale_info or None
            if self.result is not None:
                out["result"] = self.result
            return out


def _touch(job: Job) -> None:
    with JOBS_LOCK:
        JOBS.move_to_end(job.id)


def _evict_ctx() -> None:
    """Drop the least-recently-used idle contexts (they reload on demand)."""
    dropped = False
    with JOBS_LOCK:
        loaded = [j for j in JOBS.values() if j.ctx is not None]
        for job in loaded[:-MAX_LOADED_CTX] if len(loaded) > MAX_LOADED_CTX else []:
            if job.status in ("measuring", "reconstructing"):
                continue
            job.ctx = None
            job.save_state()
            job.say("context unloaded (reloadable on demand)")
            dropped = True
    if dropped:
        gc.collect()   # hand the evicted point-cloud buffers back to the OS


def _load_persisted_jobs() -> None:
    """Reattach jobs from a previous server run."""
    with JOBS_LOCK:
        for d in sorted(DATA_DIR.iterdir()):
            if not (d / "photos").is_dir() or not d.name[0].isdigit():
                continue
            state = {}
            sp = d / "state.json"
            if sp.exists():
                try:
                    state = json.loads(sp.read_text())
                except Exception:
                    state = {}
            job = Job(d.name, created=state.get("created"),
                      status=state.get("status", "reconstructing"),
                      error=state.get("error"), log=state.get("log"),
                      scale_info=state.get("scale_info"),
                      result=state.get("result"), ortho=state.get("ortho"))
            if job.status in ("reconstructing", "measuring"):
                # no state file but a finished model on disk: a job from an
                # older server version (which didn't persist state)
                if job.reconstructable:
                    job.status = "ready"
                else:
                    job.status = "error"
                    job.error = "interrupted before the reconstruction finished"
            JOBS[job.id] = job
    if JOBS:
        print(f"[server] reattached {len(JOBS)} job(s) from {DATA_DIR}", flush=True)


JOBS: OrderedDict[str, Job] = OrderedDict()
_photo_cache: OrderedDict[tuple, bytes] = OrderedDict()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "no files uploaded")
    if len(files) > MAX_PHOTOS:
        raise HTTPException(400, f"too many photos ({len(files)}); max {MAX_PHOTOS}")
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in IMAGE_EXTS:
            raise HTTPException(400, f"'{f.filename}' is not a photo "
                                     f"({suffix or 'no extension'})")

    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job = Job(job_id)
    (job.dir / "photos").mkdir(parents=True, exist_ok=True)
    (job.dir / "work").mkdir(exist_ok=True)
    with JOBS_LOCK:
        JOBS[job_id] = job
    job.save_state()

    tmp_dir = job.dir / "upload_tmp"
    tmp_dir.mkdir(exist_ok=True)
    try:
        tmp_paths = []
        for i, f in enumerate(files):
            dst = tmp_dir / f"{i:03d}_{Path(f.filename or 'photo').name}"
            size = 0
            with open(dst, "wb") as out:
                while chunk := await f.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_FILE_MB << 20:
                        raise HTTPException(400, f"{f.filename} is larger "
                                                 f"than {MAX_FILE_MB} MB")
                    out.write(chunk)
            tmp_paths.append(dst)
        names = import_photos(tmp_paths, job.dir / "photos", log=job.say)
        job.say(f"stored {len(names)} photos")
    except HTTPException:
        job.set_status("error", "upload failed: unsupported/oversized file")
        raise
    except Exception as e:
        job.set_status("error", f"upload failed: {e}")
        raise HTTPException(400, str(e))
    finally:
        for p in tmp_dir.glob("*"):
            p.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

    EXEC.submit(_run_reconstruction, job)
    return {"id": job_id}


def _run_reconstruction(job: Job):
    try:
        job.ctx = reconstruct(job.dir / "photos", job.dir / "work", log=job.say)
        if job.scale_info and job.scale_info.get("applied"):
            job.ctx.scale = job.scale_info["scale"]
            job.ctx.scale_info = job.scale_info
        job.set_status("ready", None)
        job.say("ready — set the scale, then mark the region")
    except Exception as e:
        job.say(traceback.format_exc(limit=3))
        job.set_status("error", str(e))


@app.get("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    return [{"id": j.id, "status": j.status, "created": j.created,
             "n_photos": count_photos(j.dir / "photos") if (j.dir / "photos").is_dir() else 0,
             "has_result": j.result is not None}
            for j in sorted(jobs, key=lambda j: -j.created)]


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    _touch(job)
    # resume path: rebuild the ctx on first touch after a restart (fast, from
    # the COLMAP cache) so images/scale come back
    if job.ctx is None and job.status == "ready":
        job.ensure_ctx()
    return job.snapshot()


@app.get("/api/jobs/{job_id}/photo/{name}")
def job_photo(job_id: str, name: str, w: Optional[int] = 1400):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    path = (job.dir / "photos" / name).resolve()
    if job.dir.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "no such photo")
    key = (job_id, name, w)
    if key not in _photo_cache:
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(500, "cannot decode photo")
        if w and img.shape[1] > w:
            s = w / img.shape[1]
            img = cv2.resize(img, (w, int(img.shape[0] * s)),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        _photo_cache[key] = buf.tobytes()
        with JOBS_LOCK:  # LRU: bounded memory
            while len(_photo_cache) > MAX_PHOTO_CACHE:
                _photo_cache.popitem(last=False)
    else:
        with JOBS_LOCK:
            _photo_cache.move_to_end(key)
    return Response(content=_photo_cache[key], media_type="image/jpeg")


def _get_ready_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.status == "error":
        raise HTTPException(409, f"job failed: {job.error}")
    job.ensure_ctx()
    return job


@app.post("/api/jobs/{job_id}/scale/aruco")
def scale_aruco(job_id: str, spec: dict):
    job = _get_ready_job(job_id)
    try:
        info = aruco_scale(job.ctx, side_m=float(spec.get("side_m", 0.25)),
                           dict_name=spec.get("dict", "auto"),
                           marker_id=spec.get("id"), log=job.say)
        job.scale_info = {k: v for k, v in info.items() if k != "marker_px"}
        job.save_state()
        return {k: v for k, v in info.items() if k != "marker_px"}
    except Exception as e:
        job.say(f"aruco scaling failed: {e}")
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{job_id}/scale/manual")
def scale_manual(job_id: str, spec: dict):
    job = _get_ready_job(job_id)
    try:
        a, b = spec["a"], spec["b"]
        info = manual_scale(job.ctx, a, b, float(spec["length_m"]), log=job.say)
        job.scale_info = info
        job.save_state()
        return info
    except Exception as e:
        job.say(f"manual scaling failed: {e}")
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{job_id}/measure")
def run_measure(job_id: str, spec: dict):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.status in ("measuring", "orthorectifying"):
        raise HTTPException(409, "the server is busy on this job")
    polygon = spec.get("polygon") or []
    if len(polygon) < 3:
        raise HTTPException(400, "polygon needs at least 3 points")
    if not (job.scale_info or {}).get("applied"):
        raise HTTPException(400, "set the scale (reference object) first")
    mode = spec.get("mode", "photo")
    if mode == "ortho" and not job.ortho:
        raise HTTPException(400, "generate the top-down view first")
    job.ensure_ctx()
    with job.lock:
        job.status = "measuring"
        job.result = None
    job.save_state()
    EXEC.submit(_run_measure, job, spec)
    return {"queued": True}


def _run_measure(job: Job, spec: dict):
    try:
        res = measure(job.ctx, spec.get("image"), spec["polygon"],
                      dense=bool(spec.get("dense", True)),
                      rim_px=float(spec.get("rim_px", 12.0)),
                      mode=spec.get("mode", "photo"),
                      ortho=job.ortho,
                      artifacts_dir=job.dir / "artifacts", log=job.say)
        job.result = res
        job.set_status("ready", None)
        job.say(f"done: net {res['net_volume_m3']:.1f} m^3 | "
                f"cut {res['cut_volume_m3']:.1f} | fill {res['fill_volume_m3']:.1f}")
    except Exception as e:
        job.say(traceback.format_exc(limit=3))
        job.set_status("ready", f"measure failed: {e}")
    finally:
        gc.collect()   # free stereo/fit temporaries promptly between jobs


@app.get("/api/jobs/{job_id}/auto-detect")
async def auto_detect(job_id: str, image: Optional[str] = None):
    """Run hosted landslide segmentation to fill the region polygon.

    `image` names a stored photo (polygon comes back in stored-photo px);
    omit it to segment the orthophoto instead (polygon in ortho px — the
    most accurate frame, since the model sees a nadir view). The blocking
    HTTP call runs off the event loop.
    """
    from landslide.segment import detect_landslide, load_api_key

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if image is not None:
        path = (job.dir / "photos" / image).resolve()
        if job.dir.resolve() not in path.parents or not path.exists():
            raise HTTPException(404, "no such photo")
        frame = "photo"
    else:
        path = job.dir / "artifacts" / "ortho.jpg"
        if not path.exists():
            raise HTTPException(400, "generate the top-down view first, "
                                     "or pass ?image=<photo name>")
        frame = "ortho"
    key = load_api_key()
    if not key:
        raise HTTPException(400, "automatic detection is not configured: "
                                 "set ROBOFLOW_API_KEY (see .env)")
    import asyncio
    try:
        regions = await asyncio.get_running_loop().run_in_executor(
            None, lambda: detect_landslide(path, api_key=key, log=job.say))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        job.say(f"auto-detect failed: {e}")
        raise HTTPException(502, f"detection request failed: {e}")
    if not regions:
        return {"frame": frame, "image": image, "regions": [],
                "message": "no landslide detected — trace the boundary manually"}
    return {"frame": frame, "image": image, "regions": regions}


@app.post("/api/jobs/{job_id}/ortho")
def make_ortho(job_id: str):
    """Render the top-down orthophoto (runs dense stereo on first call)."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.status in ("measuring", "orthorectifying"):
        raise HTTPException(409, "the server is busy on this job")
    job.ensure_ctx()
    if job.ortho is not None:
        return {"ready": True}
    with job.lock:
        job.status = "orthorectifying"
    job.save_state()
    EXEC.submit(_run_ortho, job)
    return {"queued": True}


def _run_ortho(job: Job):
    from landslide.ortho import render_orthophoto
    try:
        art = job.dir / "artifacts"
        art.mkdir(parents=True, exist_ok=True)
        dense_cloud(job.ctx, log=job.say)
        _, meta = render_orthophoto(job.ctx, jpg_path=art / "ortho.jpg",
                                    meta_path=art / "ortho.json", log=job.say)
        job.ortho = meta
        job.set_status("ready", None)
        job.say("top-down view ready — trace the boundary on it")
    except Exception as e:
        job.say(traceback.format_exc(limit=3))
        job.set_status("ready", f"ortho failed: {e}")
    finally:
        gc.collect()


@app.get("/api/jobs/{job_id}/artifact/{name}")
def job_artifact(job_id: str, name: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    path = (job.dir / "artifacts" / name).resolve()
    if (job.dir / "artifacts").resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "no such artifact")
    media = {"png": "image/png", "ply": "application/octet-stream",
             "json": "application/json"}.get(path.suffix.lstrip("."), "image/jpeg")
    return FileResponse(str(path), media_type=media)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
        if job is None:
            raise HTTPException(404, "unknown job")
        for key in [k for k in _photo_cache if k[0] == job_id]:
            _photo_cache.pop(key, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    gc.collect()   # release the deleted job's point cloud, if it was loaded
    return {"deleted": job_id}

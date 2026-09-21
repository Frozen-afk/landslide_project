"""FastAPI routes: upload photos -> reconstruct -> mark -> measure volume.

Split out of `server/main.py` (T3.1). The heavy stages (SfM reconstruction,
dense stereo + orthophoto, measurement) are process-isolated via
`server/executor.py`/`server/worker.py` (S2) so a native pycolmap/OpenCV
crash can't take the server down; the light stages (ArUco/manual scale, DEM
alignment) stay in-thread on the live `ReconCtx`, per the plan's own scope
("Run SfM and dense stages in a ProcessPoolExecutor").
"""
from __future__ import annotations

import asyncio
import gc
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import cv2
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landslide.pipeline import import_photos  # noqa: E402
from landslide.scaling import aruco_scale, manual_scale  # noqa: E402
from landslide.sfm import IMAGE_EXTS, count_photos  # noqa: E402
from server import executor, jobs, worker  # noqa: E402
from server.jobs import JOBS, JOBS_LOCK, Job, _photo_cache  # noqa: E402
from server.schemas import ArucoScaleRequest, ManualScaleRequest, MeasureRequest  # noqa: E402

MAX_PHOTOS = 200          # per job
MAX_FILE_MB = 80          # per uploaded photo

router = APIRouter()

BUSY_STATUSES = ("measuring", "orthorectifying")


@router.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


@router.post("/api/jobs")
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

    executor.submit_job(job, worker.run_reconstruction, on_done=_on_reconstruction_done)
    return {"id": job_id}


def _on_reconstruction_done(job: Job, result: dict | None, exc: BaseException | None) -> None:
    if exc is not None:
        job.set_status("error", str(exc))
        return
    job.set_status("ready", None)
    job.say("ready — set the scale, then mark the region")


@router.get("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        job_list = list(JOBS.values())
    return [{"id": j.id, "status": j.status, "created": j.created,
             "n_photos": count_photos(j.dir / "photos") if (j.dir / "photos").is_dir() else 0,
             "has_result": j.result is not None}
            for j in sorted(job_list, key=lambda j: -j.created)]


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    jobs.touch(job)
    # resume path: rebuild the ctx on first touch after a restart (S4: in the
    # background, so this poll never blocks on the multi-second COLMAP reload)
    if job.ctx is None and job.status == "ready":
        job.start_ctx_reload()
    return job.snapshot()


@router.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, replay: int = 20):
    """SSE tail of the job log (S5) — additive to the snapshot endpoint
    above, which remains the source of truth for resume-after-refresh."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")

    def gen():
        sent = max(0, len(job.log) - replay)
        for line in job.log[sent:]:
            yield f"data: {line}\n\n"
        last_status = None
        while True:
            # ponytail: `sent` can drift if job.log trims past 400 lines
            # mid-stream (very long jobs); acceptable — SSE is a live tail,
            # not a durability guarantee, and GET /api/jobs/{id} remains
            # the authoritative snapshot.
            n = len(job.log)
            for line in job.log[sent:n]:
                yield f"data: {line}\n\n"
            sent = n
            if job.status != last_status:
                last_status = job.status
                yield f"event: status\ndata: {job.status}\n\n"
            if job.status not in BUSY_STATUSES and job.status != "reconstructing":
                yield f"event: done\ndata: {job.status}\n\n"
                break
            time.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/jobs/{job_id}/photo/{name}")
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
            while len(_photo_cache) > jobs.MAX_PHOTO_CACHE:
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


def _attach_geo(job: Job) -> None:
    """Georeference annotation once the metric scale is known."""
    try:
        from landslide.geo import attach_georef, load_gps
        gps = load_gps(job.dir / "photos")
        if gps:
            attach_georef(job.ctx, gps, log=job.say)
    except Exception as e:
        job.say(f"[geo] georeferencing skipped: {e}")


@router.post("/api/jobs/{job_id}/scale/aruco")
def scale_aruco(job_id: str, spec: ArucoScaleRequest):
    job = _get_ready_job(job_id)
    try:
        info = aruco_scale(job.ctx, side_m=spec.side_m,
                           dict_name=spec.dict_name,
                           marker_id=spec.id, log=job.say)
        job.scale_info = {k: v for k, v in info.items() if k != "marker_px"}
        _attach_geo(job)
        job.save_state()
        return {k: v for k, v in info.items() if k != "marker_px"}
    except Exception as e:
        job.say(f"aruco scaling failed: {e}")
        raise HTTPException(400, str(e))


@router.post("/api/jobs/{job_id}/scale/manual")
def scale_manual(job_id: str, spec: ManualScaleRequest):
    job = _get_ready_job(job_id)
    try:
        info = manual_scale(job.ctx, spec.a.model_dump(), spec.b.model_dump(),
                            spec.length_m, log=job.say)
        job.scale_info = info
        _attach_geo(job)
        job.save_state()
        return info
    except Exception as e:
        job.say(f"manual scaling failed: {e}")
        raise HTTPException(400, str(e))


@router.post("/api/jobs/{job_id}/dem")
def upload_dem(job_id: str, file: UploadFile = File(...)):
    """Import a prior-surface DEM (XYZ grid text) and align it to the model.

    Alignment is gravity-seeded trimmed ICP: the debris itself is rejected
    from the correspondence cut. Requires the metric scale to be set.
    """
    job = _get_ready_job(job_id)
    if not (job.scale_info or {}).get("applied"):
        raise HTTPException(400, "set the scale (reference object) first — "
                                 "the DEM is aligned to the metric model")
    dest = job.dir / "dem.xyz"
    with open(dest, "wb") as f:
        while chunk := file.file.read(1 << 20):
            f.write(chunk)
    try:
        from landslide.dem import align_to_dem, load_dem
        from landslide.densify import estimate_up
        dem = load_dem(dest, log=job.say)
        up = estimate_up(job.ctx.views, job.ctx.sparse)
        al = align_to_dem(job.ctx, dem["pts"], up, log=job.say)
        job.dem_info = {"file": "dem.xyz", "R": al["R"].tolist(),
                        "t": al["t"].tolist(), "rms_m": al["rms_m"]}
        job.save_state()
        return {"aligned": True, "rms_m": al["rms_m"],
                "n_points": len(dem["pts"])}
    except Exception as e:
        job.say(f"DEM import failed: {e}")
        raise HTTPException(400, str(e))


@router.delete("/api/jobs/{job_id}/dem")
def remove_dem(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job.dem_info = None
    (job.dir / "dem.xyz").unlink(missing_ok=True)
    job.save_state()
    return {"removed": True}


@router.post("/api/jobs/{job_id}/measure")
def run_measure_endpoint(job_id: str, spec: MeasureRequest):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if len(spec.polygon) < 3:
        raise HTTPException(400, "polygon needs at least 3 points")
    if not (job.scale_info or {}).get("applied"):
        raise HTTPException(400, "set the scale (reference object) first")
    if spec.mode == "ortho" and not job.ortho:
        raise HTTPException(400, "generate the top-down view first")
    if not job.reconstructable:
        raise HTTPException(409, "this job has no cached reconstruction (it "
                                 "may have failed or been interrupted before "
                                 "finishing) — re-upload the photos")
    # S3: check-and-set the busy status atomically — two racing POSTs must
    # not both see "not busy" and both submit.
    with job.lock:
        if job.status in BUSY_STATUSES:
            raise HTTPException(409, "the server is busy on this job")
        job.status = "measuring"
        job.result = None
    job.save_state()
    spec_dict = {"image": spec.image, "polygon": spec.polygon, "dense": spec.dense,
                 "rim_px": spec.rim_px, "rim_inner_px": spec.rim_inner_px,
                 "mode": spec.mode, "ortho": job.ortho}
    executor.submit_job(job, worker.run_measure, job.scale_info, job.dem_info,
                        spec_dict, job.dir / "artifacts", on_done=_on_measure_done)
    return {"queued": True}


def _on_measure_done(job: Job, result: dict | None, exc: BaseException | None) -> None:
    if exc is not None:
        job.set_status("ready", f"measure failed: {exc}")
        return
    job.result = result
    job.set_status("ready", None)
    job.say(f"done: net {result['net_volume_m3']:.1f} m^3 | "
            f"cut {result['cut_volume_m3']:.1f} | fill {result['fill_volume_m3']:.1f}")


@router.get("/api/jobs/{job_id}/auto-detect")
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


@router.post("/api/jobs/{job_id}/ortho")
def make_ortho(job_id: str):
    """Render the top-down orthophoto (runs dense stereo on first call)."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.ortho is not None:
        return {"ready": True}
    if not job.reconstructable:
        raise HTTPException(409, "this job has no cached reconstruction (it "
                                 "may have failed or been interrupted before "
                                 "finishing) — re-upload the photos")
    with job.lock:
        if job.status in BUSY_STATUSES:
            raise HTTPException(409, "the server is busy on this job")
        job.status = "orthorectifying"
    job.save_state()
    executor.submit_job(job, worker.run_ortho, job.scale_info, job.dir / "artifacts",
                        on_done=_on_ortho_done)
    return {"queued": True}


def _on_ortho_done(job: Job, result: dict | None, exc: BaseException | None) -> None:
    if exc is not None:
        job.set_status("ready", f"ortho failed: {exc}")
        return
    job.ortho = result
    job.set_status("ready", None)
    job.say("top-down view ready — trace the boundary on it")


@router.get("/api/jobs/{job_id}/artifact/{name}")
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


@router.delete("/api/jobs/{job_id}")
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

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
import hashlib
import shutil
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import cv2
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landslide.capture import image_checks, sequence_checks  # noqa: E402
from landslide.pipeline import import_photos  # noqa: E402
from landslide.scaling import aruco_scale, manual_scale  # noqa: E402
from landslide.sfm import IMAGE_EXTS, count_photos  # noqa: E402
from server import executor, jobs, worker  # noqa: E402
from server.jobs import JOBS, JOBS_LOCK, Job, _photo_cache  # noqa: E402
from server.schemas import (AnnotationRequest, ArucoScaleRequest,  # noqa: E402
                            CaptureFinalizeRequest,
                            CapturePhotoMeta, CapturePreviewRequest,
                            CaptureRoiRequest, CaptureSessionRequest,
                            ManualScaleRequest, MeasureRequest)

MAX_PHOTOS = 200          # per job
MAX_FILE_MB = 80          # per uploaded photo
CAPTURE_IMAGE_EXTS = {".jpg", ".jpeg"}   # §8: file-input capture is JPEG only

router = APIRouter()

BUSY_STATUSES = ("measuring", "orthorectifying")

# §9.2: T1 per-photo checks run in the FastAPI threadpool (not the SfM
# process pool) — capped at 2 concurrent so a burst of queued uploads (e.g.
# catching up after a connectivity gap) can't starve every other request on
# a small field laptop.
_T1_SEMAPHORE = threading.Semaphore(2)


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
        # F14: PIL EXIF-transpose + LANCZOS resize + Laplacian over up to 200
        # photos is tens of seconds of pure CPU — run off the event loop so
        # every other request (polls, SSE, other users' uploads) isn't
        # frozen for the duration.
        names = await run_in_threadpool(import_photos, tmp_paths,
                                        job.dir / "photos", log=job.say)
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


# ---------------------------------------------------------------------------
# S2 (GUIDED_CAPTURE_PLAN.md §9.2): capture-session backend. No preview
# gating yet (S4) — finalize always proceeds once >=3 kept photos exist.
# ---------------------------------------------------------------------------

def _get_capturing_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    if job.status != "capturing":
        raise HTTPException(409, f"job is not in a capture session (status={job.status})")
    return job


@router.post("/api/jobs/capture")
def create_capture_session(spec: CaptureSessionRequest):
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    job = Job(job_id, status="capturing")
    # photos/ empty at session start so load_persisted_jobs reattaches the
    # job (its own scan requires the dir to exist, §9.1)
    (job.dir / "photos").mkdir(parents=True, exist_ok=True)
    (job.capture_dir / "originals").mkdir(parents=True, exist_ok=True)
    (job.capture_dir / "feats").mkdir(parents=True, exist_ok=True)
    job.capture = {
        "version": 1,
        "marker_side_m": spec.marker_side_m,
        "marker_dict": spec.marker_dict,
        "has_pre_event_dem": spec.has_pre_event_dem,
        "roi": None,
        "photos": [],
        "sequence": {},
        "preview": None,
        "finalized": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    job.save_state()
    job.save_capture()
    return {"id": job_id}


@router.get("/api/jobs/{job_id}/capture")
def get_capture_session(job_id: str):
    job = JOBS.get(job_id)
    if job is None or job.capture is None:
        raise HTTPException(404, "no capture session for this job")
    return job.capture


@router.put("/api/jobs/{job_id}/capture/photos/{photo_uuid}")
async def put_capture_photo(job_id: str, photo_uuid: str,
                            file: UploadFile = File(...), meta: str = Form(...)):
    job = _get_capturing_job(job_id)
    try:
        photo_meta = CapturePhotoMeta.model_validate_json(meta)
    except Exception as e:
        raise HTTPException(400, f"bad meta: {e}")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in CAPTURE_IMAGE_EXTS:
        raise HTTPException(400, f"'{file.filename}' is not a JPEG "
                                 f"({suffix or 'no extension'}) — guided capture "
                                 "only accepts image/jpeg (§8)")
    data = await file.read()
    if len(data) > MAX_FILE_MB << 20:
        raise HTTPException(400, f"{file.filename} is larger than {MAX_FILE_MB} MB")
    sha256 = hashlib.sha256(data).hexdigest()

    with job.lock:
        existing = next((p for p in job.capture["photos"] if p["uuid"] == photo_uuid), None)
        if existing is not None:
            if existing["sha256"] != sha256:
                raise HTTPException(409, "uuid already used with different photo content")
            # idempotent retry: return the stored result, don't reprocess
            return {"verdict": existing["verdict"], "reasons": existing["reasons"],
                    "server": existing["server"], "sequence": job.capture["sequence"]}

    dest = job.capture_dir / "originals" / f"{photo_uuid}.jpg"
    dest.write_bytes(data)

    with job.lock:
        photos = job.capture["photos"]
        same_arm = [p for p in photos if p["arm"] == photo_meta.arm and not p["excluded"]]
        prev = same_arm[-1] if same_arm else None
        prev_path = (job.capture_dir / "originals" / f"{prev['uuid']}.jpg") if prev else None
        prev_feats_cache = (job.capture_dir / "feats" / f"{prev['uuid']}.npz") if prev else None
        prev_pivot = bool(prev and prev.get("_pivot_warn"))

        bridge_paths = None
        if photo_meta.arm in ("B", "T"):
            arm_a = [p for p in photos if p["arm"] == "A" and not p["excluded"]]
            b_count = len(same_arm) if photo_meta.arm == "B" else 0
            if arm_a and (photo_meta.arm == "T" or b_count < 3):
                bridge_paths = [job.capture_dir / "originals" / f"{p['uuid']}.jpg"
                                for p in arm_a[-3:]]

        laps = [p["server"]["lap"] for p in photos if not p["excluded"]]
        blur_median = float(statistics.median(laps)) if len(laps) >= 5 else None
        marker_side_m = job.capture["marker_side_m"]
        marker_dict = job.capture["marker_dict"]

    feats_cache = job.capture_dir / "feats" / f"{photo_uuid}.npz"

    def _run():
        with _T1_SEMAPHORE:
            return image_checks(dest, prev_path=prev_path, marker_side_m=marker_side_m,
                                marker_dict=marker_dict, blur_median=blur_median,
                                arm_bridge_paths=bridge_paths, prev_pivot=prev_pivot,
                                feats_cache=feats_cache, prev_feats_cache=prev_feats_cache)

    try:
        result = await run_in_threadpool(_run)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"could not check photo: {e}")

    with job.lock:
        entry = {
            "uuid": photo_uuid, "seq": photo_meta.seq, "arm": photo_meta.arm,
            "sha256": sha256, "client": photo_meta.client,
            "server": result["server"], "verdict": result["verdict"],
            "reasons": result["reasons"], "excluded": False,
            "_pivot_warn": result["server"].get("parallax_pivot") is True,
        }
        job.capture["photos"].append(entry)
        job.capture["photos"].sort(key=lambda p: p["seq"])
        job.capture["sequence"] = sequence_checks(job.capture["photos"])
        seq_snapshot = job.capture["sequence"]
    job.save_capture()

    return {"verdict": result["verdict"], "reasons": result["reasons"],
            "server": result["server"], "sequence": seq_snapshot}


@router.delete("/api/jobs/{job_id}/capture/photos/{photo_uuid}")
def delete_capture_photo(job_id: str, photo_uuid: str):
    job = _get_capturing_job(job_id)
    with job.lock:
        entry = next((p for p in job.capture["photos"] if p["uuid"] == photo_uuid), None)
        if entry is None:
            raise HTTPException(404, "no such photo in this session")
        entry["excluded"] = True
        job.capture["sequence"] = sequence_checks(job.capture["photos"])
    job.save_capture()
    return {"excluded": True}


def _polygon_valid(polygon: list) -> str | None:
    """§12c.8 ROI validation. Returns an error message, or None if ok."""
    n = len(polygon)
    if n < 3:
        return "polygon needs at least 3 vertices"
    if n > 200:
        return "polygon has more than 200 vertices"
    pts = [(float(x), float(y)) for x, y in polygon]

    def _seg_intersect(a, b, c, d):
        def ccw(p, q, r):
            return (r[1] - p[1]) * (q[0] - p[0]) - (q[1] - p[1]) * (r[0] - p[0])
        d1, d2 = ccw(a, b, c), ccw(a, b, d)
        d3, d4 = ccw(c, d, a), ccw(c, d, b)
        return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)

    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue   # adjacent edges share an endpoint, not a crossing
            if _seg_intersect(*edges[i], *edges[j]):
                return "polygon edges self-intersect"
    return None


@router.put("/api/jobs/{job_id}/capture/roi")
def put_capture_roi(job_id: str, spec: CaptureRoiRequest):
    """S5C (§12c.3, §12c.8): store the tapped slide outline. Idempotent —
    the last write wins. A preview in flight is marked to re-run once it
    finishes, same mechanism as a mid-run second preview request."""
    job = _get_capturing_job(job_id)
    err = _polygon_valid(spec.polygon)
    if err:
        raise HTTPException(422, err)
    with job.lock:
        photo = next((p for p in job.capture["photos"] if p["uuid"] == spec.photo_uuid), None)
        if photo is None:
            raise HTTPException(404, "no such photo in this session")
        if photo["excluded"]:
            raise HTTPException(409, "that photo was excluded — draw the outline on a kept photo")
        job.capture["roi"] = {"photo_uuid": spec.photo_uuid,
                              "polygon_px": [list(p) for p in spec.polygon],
                              "set_at": time.time()}
        preview = job.capture.get("preview")
        stale = False
        if preview and preview.get("status") == "running":
            preview["rerun_after"] = True
            stale = True
        elif preview and preview.get("status") == "done":
            stale = True
    job.save_capture()
    return {"ok": True, "preview_stale": stale}


@router.post("/api/jobs/{job_id}/capture/preview")
def start_capture_preview(job_id: str, spec: CapturePreviewRequest = CapturePreviewRequest()):
    """S4/S5C (§9.2, §12c.8): one preview SfM attempt (cheap: 1000 px, single
    attempt) on the kept photos so far, run in the same worker pool as
    reconstruction (`executor.submit_job`) so a crash there can't take the
    server down. One preview in flight per job; a request that arrives
    mid-run is queued to re-run once the current one finishes.

    `spec.roi_only` (§12c.8): when true AND the kept-photo uuid set matches
    the last *done* preview's own `photo_set`, the worker reuses the cached
    preview SfM (`reconstruct(..., reuse=True)`) and only recomputes the
    coverage/instruction report — seconds, not the full ~100s attempt.
    Falls back to a full run when the photo set changed or no prior done
    preview exists, since `reconstruct`'s own `reuse` does not itself check
    the photo set (`sfm.py`)."""
    job = _get_capturing_job(job_id)
    with job.lock:
        photos = sorted((p for p in job.capture["photos"] if not p["excluded"]),
                        key=lambda p: p["seq"])
        if len(photos) < 3:
            raise HTTPException(400, "need at least 3 kept photos to preview")
        preview = job.capture.get("preview")
        if preview and preview.get("status") == "running":
            preview["rerun_after"] = True
            job.save_capture()
            return {"running": True}
        photo_set = sorted(p["uuid"] for p in photos)
        max_seq = photos[-1]["seq"] if photos else -1
        reuse = bool(spec.roi_only and preview and preview.get("status") == "done"
                    and preview.get("photo_set") == photo_set)
        job.capture["preview"] = {"status": "running", "started": time.time(),
                                  "finished": None, "n_photos": len(photos),
                                  "report": None, "error": None,
                                  "photo_set": photo_set, "max_seq": max_seq}
        originals_order = [{"uuid": p["uuid"], "seq": p["seq"], "arm": p["arm"]}
                           for p in photos]
        marker_side_m = job.capture["marker_side_m"]
        marker_dict = job.capture["marker_dict"]
        roi = job.capture.get("roi")
    job.save_capture()
    executor.submit_job(job, worker.run_preview, job.capture_dir, originals_order,
                        marker_side_m, marker_dict, roi, reuse, on_done=_on_preview_done)
    return {"queued": True}


def _on_preview_done(job: Job, result: dict | None, exc: BaseException | None) -> None:
    rerun = False
    with job.lock:
        preview = (job.capture or {}).get("preview")
        if preview is None:
            return
        rerun = bool(preview.pop("rerun_after", False))
        if exc is not None:
            preview.update(status="error", finished=time.time(), error=str(exc),
                           report=None)
        else:
            preview.update(status="done", finished=time.time(), report=result,
                           error=None)
    job.save_capture()
    job.say("preview reconstruction done" if exc is None else f"preview failed: {exc}")
    if rerun and job.status == "capturing":
        try:
            start_capture_preview(job.id)
        except HTTPException as e:
            job.say(f"preview re-run skipped: {e.detail}")


@router.post("/api/jobs/{job_id}/capture/finalize")
async def finalize_capture(job_id: str, spec: CaptureFinalizeRequest):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    with job.lock:
        if job.status in ("reconstructing", "measuring", "orthorectifying"):
            raise HTTPException(409, "the server is busy on this job")
        if job.capture is None:
            raise HTTPException(404, "no capture session for this job")
        # §10 "finalize interrupted": a crash between import_photos and
        # submit_job leaves the job `reconstructing` with no complete model,
        # which load_persisted_jobs already demotes to `error` (P3d) on
        # restart; retrying finalize from status=="error" re-runs it from
        # capture/originals (kept on disk until the job is deleted), same
        # as any normal first finalize.
        if job.status not in ("capturing", "error"):
            raise HTTPException(409, f"job is not in a capture session (status={job.status})")
        photos = sorted((p for p in job.capture["photos"] if not p["excluded"]),
                        key=lambda p: p["seq"])
        if len(photos) < 3:
            raise HTTPException(400, "need at least 3 kept photos to finalize")
        # S4: block on the most recent finished preview's own findings, if
        # any — a job that never ran a preview (or whose preview is still
        # running/stale) has nothing to block on, same as before S4.
        preview = job.capture.get("preview")
        blocking_ids: list[str] = []
        blocking: list[dict] = []
        if preview and preview.get("status") == "done" and preview.get("report"):
            report = preview["report"]
            blocking_ids = list(report.get("blocking", []))
            by_id = {c["id"]: c["text"] for c in report.get("checks", [])}
            blocking = [{"id": rid, "text": by_id.get(rid, rid)} for rid in blocking_ids]
        # §12c.8: true when the kept-photo set has drifted since the last
        # finished preview (new photos, a retake, an exclusion) — the UI
        # offers "re-run preview" instead of "finish anyway" in that case.
        current_set = sorted(p["uuid"] for p in photos)
        stale_preview = bool(preview) and preview.get("photo_set") != current_set
        if blocking_ids and not spec.override:
            raise HTTPException(409, {"blocking": blocking, "stale_preview": stale_preview})
        job.status = "reconstructing"   # marks the session closed to further PUT/DELETE
    job.save_state()

    tmp_dir = job.dir / "capture_finalize_tmp"
    tmp_dir.mkdir(exist_ok=True)
    try:
        tmp_paths = []
        for p in photos:
            src = job.capture_dir / "originals" / f"{p['uuid']}.jpg"
            dst = tmp_dir / f"{p['seq']:03d}_{p['uuid']}.jpg"
            shutil.copyfile(src, dst)
            tmp_paths.append(dst)
        names = await run_in_threadpool(import_photos, tmp_paths,
                                        job.dir / "photos", log=job.say)
        job.say(f"stored {len(names)} photos from the guided capture session")
    except Exception as e:
        job.set_status("error", f"finalize failed: {e}")
        raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    with job.lock:
        job.capture["finalized"] = {"at": time.time(), "override": spec.override,
                                    "blocking_at_finalize": blocking_ids}
    job.save_capture()

    executor.submit_job(job, worker.run_reconstruction, on_done=_on_reconstruction_done)
    return {"queued": True}


def _n_photos(job: Job) -> int:
    if job.status == "capturing" and job.capture is not None:
        # photos/ stays empty until finalize (§9.1) — count the manifest
        return sum(1 for p in job.capture["photos"] if not p["excluded"])
    return count_photos(job.dir / "photos") if (job.dir / "photos").is_dir() else 0


@router.get("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        job_list = list(JOBS.values())
    return [{"id": j.id, "status": j.status, "created": j.created,
             "n_photos": _n_photos(j),
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


def _invalidate_scale_dependents(job: Job) -> None:
    """A re-scale (ArUco or manual) changes every metric quantity derived
    from the old scale — the orthophoto's u0/v0/res, the DEM alignment
    (R/t fit against the old-scale cloud) and any prior measurement — so all
    three must be dropped (F5), not silently left stale for the next
    ortho-mode trace or measure to use without the user ever re-running
    them."""
    job.ortho = None
    job.dem_info = None
    job.result = None
    for name in ("ortho.jpg", "ortho.json"):
        (job.dir / "artifacts" / name).unlink(missing_ok=True)
    (job.dir / "dem.xyz").unlink(missing_ok=True)


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
        _invalidate_scale_dependents(job)
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
        _invalidate_scale_dependents(job)
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


# ---------------------------------------------------------------------------
# Multi-view region outlines (MULTIVIEW_REGION_PROGRESS.md). One outline per
# photo, persisted in state.json; every write is an idempotent PUT/DELETE so
# a retried or doubled request cannot create a second copy.
# ---------------------------------------------------------------------------

MV_PREVIEW_POINTS = 150_000   # cloud subsample for the in-request fusion preview


def _accepted(job: Job) -> list[dict]:
    return [{"image": n, "polygon": a["polygon"]}
            for n, a in sorted(job.annotations.items())
            if a.get("status") == "accepted" and len(a.get("polygon") or []) >= 3]


def _mv_preview(job: Job) -> dict | None:
    """Fusion of the accepted outlines on a subsample of the cached cloud
    (sparse cloud when no dense cache exists yet), for the marking UI:
    agreement per outline and the 3D rings to reproject. Cached per
    (annotations_rev, scale); None when there is nothing to fuse."""
    import numpy as np

    from landslide import multiview
    from landslide.densify import load_cached_dense

    anns = [a for a in _accepted(job) if a["image"] in job.ctx.views]
    if not anns:
        return None
    key = (job.annotations_rev, job.ctx.scale)
    cached = job._mv_preview
    if cached is not None and cached[0] == key:
        return cached[1]
    dense = load_cached_dense(job.ctx, log=lambda *_: None)
    pts = dense["points"] if dense is not None and len(dense["points"]) else job.ctx.sparse
    pts = np.ascontiguousarray(pts[:: max(1, len(pts) // MV_PREVIEW_POINTS)])
    try:
        f = multiview.fuse(job.ctx, anns, pts=pts, log=lambda *_: None)
        out = {"stats": f["stats"], "rings": f["rings"], "error": None}
    except Exception as e:
        out = {"stats": None, "rings": {}, "error": str(e)}
    job._mv_preview = (key, out)
    return out


def _mv_summary(job: Job) -> dict:
    """Everything the marking step shows: POV groups and progress toward
    4 x 2, the photos to outline next, agreement per outline, and what to
    do about each problem."""
    from landslide import multiview

    pov = multiview.pov_groups(job.ctx)
    registered = {n: a for n, a in job.annotations.items() if n in pov["views"]}
    preview = _mv_preview(job)
    stats = (preview or {}).get("stats")
    by_img = {a["image"]: a for a in (stats or {}).get("annotations", [])}
    used = [n for n, a in registered.items() if a.get("status") == "accepted"
            and not by_img.get(n, {}).get("rejected")]
    req = multiview.requirement(pov, used)
    ring = (preview or {}).get("rings", {}).get("best")
    suggestions = multiview.suggest_images(job.ctx, pov, registered, target=ring)

    todo: list[str] = []
    for a in by_img.values():
        if a["rejected"]:
            todo.append(f"{a['image']}: outline marks a different area than the "
                        "others — redraw it or delete it")
        elif a["weight"] < 1.0:
            todo.append(f"{a['image']}: outline only partly agrees with the others "
                        "— check its edge")
    if not req["met"]:
        if not req["achievable"]:
            todo.append(f"these photos cover only {req['n_groups_available']} viewpoint(s) "
                        f"with {req['min_per_group']}+ photos; a high-confidence result "
                        f"needs {req['min_groups']} — add photos from further around the "
                        "slide, or compute an indicative result")
        if suggestions:
            s0 = suggestions[0]
            todo.append(f"next: outline the slide on {s0['image']} (viewpoint "
                        f"P{s0['group']}, {s0['azimuth_deg']:.0f}° around)")
    if preview and preview.get("error"):
        todo.append(f"the outlines could not be fused: {preview['error']}")

    return {
        "groups": [{"id": g, "azimuth_deg": info["azimuth_deg"],
                    "elevation_deg": info["elevation_deg"],
                    "n_images": len(info["images"]), "images": info["images"],
                    "n_accepted": req["counts"].get(g, 0)}
                   for g, info in sorted(pov["groups"].items())],
        "views": pov["views"],
        "requirement": req,
        "suggestions": suggestions,
        "agreement": by_img,
        "fusion": ({k: stats[k] for k in ("n_groups", "n_used", "area_lower_m2",
                                           "area_best_m2", "area_upper_m2",
                                           "uncertain_area_frac", "weak_support_frac",
                                           "components")} if stats else None),
        "instructions": todo,
    }


def _annotations_payload(job: Job) -> dict:
    with job.lock:
        anns = {n: dict(a) for n, a in job.annotations.items()}
        rev = job.annotations_rev
    return {"annotations": anns, "rev": rev, "summary": _mv_summary(job)}


@router.get("/api/jobs/{job_id}/annotations")
def list_annotations(job_id: str):
    return _annotations_payload(_get_ready_job(job_id))


@router.get("/api/jobs/{job_id}/annotations/{image}")
def get_annotation(job_id: str, image: str):
    """One photo's outline plus the current 3D region reprojected into that
    photo (`projected.{lower,best,upper}`) — the editable suggestion, and the
    confident/uncertain band to draw against."""
    from landslide import multiview

    job = _get_ready_job(job_id)
    view = job.ctx.views.get(image)
    if view is None:
        raise HTTPException(404, "that photo is not part of the reconstruction")
    preview = _mv_preview(job)
    rings = (preview or {}).get("rings") or {}
    pov = multiview.pov_groups(job.ctx)
    return {"image": image, "annotation": job.annotations.get(image),
            "rev": job.annotations_rev, "view": pov["views"][image],
            "projected": {lvl: multiview.project_ring(view, rings.get(lvl))
                          for lvl in ("lower", "best", "upper")}}


@router.put("/api/jobs/{job_id}/annotations/{image}")
def put_annotation(job_id: str, image: str, spec: AnnotationRequest):
    job = _get_ready_job(job_id)
    view = job.ctx.views.get(image)
    if view is None:
        raise HTTPException(404, "that photo is not part of the reconstruction")
    poly = [[float(x), float(y)] for x, y in spec.polygon]
    if spec.status != "skipped":
        if len(poly) < 3:
            raise HTTPException(422, "polygon needs at least 3 vertices")
        m = 0.5 * max(view.width, view.height)      # generous: zoomed-out drags
        if not all(-m <= x <= view.width + m and -m <= y <= view.height + m
                   for x, y in poly):               # also rejects NaN/inf
            raise HTTPException(422, "polygon lies outside the photo")
    entry = {"polygon": poly, "status": spec.status, "source": spec.source}
    with job.lock:
        old = job.annotations.get(image)
        if old is None or {k: old.get(k) for k in entry} != entry:   # idempotent re-PUT
            job.annotations[image] = {**entry, "updated": time.time()}
            job.annotations_rev += 1
        saved, rev = dict(job.annotations[image]), job.annotations_rev
    job.save_state()
    return {"image": image, "annotation": saved, "rev": rev}


@router.delete("/api/jobs/{job_id}/annotations/{image}")
def delete_annotation(job_id: str, image: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    with job.lock:
        if job.annotations.pop(image, None) is not None:   # deleting twice is fine
            job.annotations_rev += 1
        rev = job.annotations_rev
    job.save_state()
    return {"deleted": image, "rev": rev}


@router.post("/api/jobs/{job_id}/measure")
def run_measure_endpoint(job_id: str, spec: MeasureRequest):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    multiview_anns = None
    if spec.multiview:
        if spec.mode != "photo":
            raise HTTPException(400, "multi-view outlines are a photo-mode workflow")
        with job.lock:
            multiview_anns, anns_rev = _accepted(job), job.annotations_rev
        if not multiview_anns:
            raise HTTPException(400, "accept at least one outline first")
    elif len(spec.polygon) < 3:
        raise HTTPException(400, "polygon needs at least 3 points")
    if not (job.scale_info or {}).get("applied"):
        raise HTTPException(400, "set the scale (reference object) first")
    if spec.mode == "ortho" and not job.ortho:
        raise HTTPException(400, "generate the top-down view first")
    if spec.mode == "ortho" and job.ortho:
        ortho_scale = job.ortho.get("scale")
        cur_scale = (job.scale_info or {}).get("scale")
        if ortho_scale is not None and cur_scale is not None \
                and abs(ortho_scale - cur_scale) > 1e-9 * max(abs(cur_scale), 1.0):
            raise HTTPException(409, "the top-down view was rendered at a "
                                     "different scale (re-scaled since) — "
                                     "generate it again")
    if not job.reconstructable:
        raise HTTPException(409, "this job has no cached reconstruction (it "
                                 "may have failed or been interrupted before "
                                 "finishing) — re-upload the photos")
    if multiview_anns and not spec.indicative:
        # a final (high-confidence-eligible) multi-view measurement needs
        # 4 viewpoints x 2 photos; below that the caller must ask for an
        # indicative result explicitly
        if job.status in BUSY_STATUSES:
            raise HTTPException(409, "the server is busy on this job")
        summary = _mv_summary(_get_ready_job(job_id))
        if not summary["requirement"]["met"]:
            raise HTTPException(409, {
                "message": "outline the slide on 2 photos from each of 4 viewpoints "
                           "first, or ask for an indicative result",
                "requirement": summary["requirement"],
                "instructions": summary["instructions"]})
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
    if multiview_anns:
        spec_dict.update(annotations=multiview_anns, annotations_rev=anns_rev)
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
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    # P3a: a worker (reconstruct/measure/ortho) writes into job.dir; deleting
    # it out from under a running worker leaves the worker recreating
    # artifacts/ into a directory that no longer exists.
    with job.lock:
        if job.status in BUSY_STATUSES or job.status == "reconstructing":
            raise HTTPException(409, "the server is busy on this job — wait "
                                     "for it to finish before deleting it")
        # §9.1: a running preview writes into capture/preview/ from a worker
        # process; same reasoning as the reconstructing/measuring guard above.
        if job.capture and (job.capture.get("preview") or {}).get("status") == "running":
            raise HTTPException(409, "a preview reconstruction is running for "
                                     "this job — wait for it to finish before "
                                     "deleting it")
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
        for key in [k for k in _photo_cache if k[0] == job_id]:
            _photo_cache.pop(key, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    gc.collect()   # release the deleted job's point cloud, if it was loaded
    return {"deleted": job_id}

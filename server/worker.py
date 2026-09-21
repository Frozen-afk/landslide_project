"""Top-level, picklable worker functions for the process-isolated heavy
stages: SfM reconstruction, dense stereo + orthophoto, measurement (S2).

A native crash inside pycolmap/OpenCV must not take down the server, so
these run in a `ProcessPoolExecutor` (see `server/executor.py`), not a
thread. Each function only touches its job's own directory on disk and a
log queue — never a live `Job`/`ReconCtx` object from the parent process
(those hold a `threading.Lock` and aren't picklable, and per the plan "the
job directory is the only shared state"). Every function reloads whatever
context it needs from the on-disk COLMAP cache via `reconstruct(reuse=True)`.
"""
from __future__ import annotations

import time
from pathlib import Path


def _log(queue, job_id: str):
    def say(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            queue.put((job_id, line))
        except Exception:
            pass
    return say


def _load_dem(job_dir: Path, dem_info: dict, log):
    import numpy as np
    from landslide.dem import DemSurface, load_dem
    dem = load_dem(job_dir / dem_info["file"], log=log)
    surface = DemSurface(dem["pts"])
    return np.asarray(dem_info["R"]), np.asarray(dem_info["t"]), surface


def run_reconstruction(job_id: str, photos_dir, work_dir, queue) -> dict:
    from landslide.sfm import reconstruct
    log = _log(queue, job_id)
    ctx = reconstruct(Path(photos_dir), Path(work_dir), log=log)
    return {"n_views": len(ctx.views)}


def run_ortho(job_id: str, photos_dir, work_dir, queue,
              scale_info: dict, artifacts_dir) -> dict:
    from landslide.densify import dense_cloud
    from landslide.ortho import render_orthophoto
    from landslide.sfm import reconstruct
    log = _log(queue, job_id)
    ctx = reconstruct(Path(photos_dir), Path(work_dir), reuse=True, log=log)
    if scale_info and scale_info.get("applied"):
        ctx.scale = scale_info["scale"]
        ctx.scale_info = scale_info
    art = Path(artifacts_dir)
    art.mkdir(parents=True, exist_ok=True)
    dense_cloud(ctx, log=log)
    _, meta = render_orthophoto(ctx, jpg_path=art / "ortho.jpg",
                                meta_path=art / "ortho.json", log=log)
    return meta


def run_measure(job_id: str, photos_dir, work_dir, queue,
                 scale_info: dict, dem_info: dict | None,
                 spec: dict, artifacts_dir) -> dict:
    from landslide.pipeline import measure
    from landslide.sfm import reconstruct
    log = _log(queue, job_id)
    photos_dir = Path(photos_dir)
    work_dir = Path(work_dir)
    ctx = reconstruct(photos_dir, work_dir, reuse=True, log=log)
    if scale_info and scale_info.get("applied"):
        ctx.scale = scale_info["scale"]
        ctx.scale_info = scale_info
    dem = _load_dem(work_dir.parent, dem_info, log) if dem_info else None
    res = measure(ctx, spec.get("image"), spec["polygon"],
                  dense=spec.get("dense", True),
                  rim_px=spec.get("rim_px", 12.0),
                  rim_inner_px=spec.get("rim_inner_px"),
                  mode=spec.get("mode", "photo"),
                  ortho=spec.get("ortho"),
                  dem=dem,
                  artifacts_dir=Path(artifacts_dir), log=log)
    return res

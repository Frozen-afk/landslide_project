"""Job bookkeeping: the `Job` model, its on-disk persistence, and the process
registry. Split out of `server/main.py` (T3.1) so routes and worker-process
orchestration can be tested/imported independently.

Jobs survive a server restart: status/log/scale/result are persisted to
<job>/state.json and the reconstruction context is rebuilt lazily from the
cached COLMAP database when a job is touched again, so a browser refresh or
server upgrade mid-session doesn't lose work.
"""
from __future__ import annotations

import gc
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "jobs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_LOADED_CTX = 2        # reconstructions held in RAM (LRU-evicted, reloadable);
                          # each ctx carries a multi-million-point dense cloud
MAX_PHOTO_CACHE = 150     # rendered photo thumbnails kept in RAM


class Job:
    def __init__(self, job_id: str, created: float | None = None,
                 status: str = "reconstructing", error: str | None = None,
                 log: list[str] | None = None, scale_info: dict | None = None,
                 result: dict | None = None, ortho: dict | None = None,
                 dem_info: dict | None = None):
        self.id = job_id
        self.dir = DATA_DIR / job_id
        self.status = status          # reconstructing|ready|measuring|orthorectifying|error
        self.error = error
        self.log = log or []
        self.created = created or time.time()
        self.ctx = None               # ReconCtx; rebuilt lazily from disk
        self.result = result
        self.scale_info = scale_info  # mirror of ctx.scale_info, persisted
        self.ortho = ortho            # orthophoto metadata, persisted
        self.dem_info = dem_info      # prior-DEM transform, persisted
        # RLock, not Lock: ensure_ctx() holds this for its whole body
        # (including its evict_ctx() call), and evict_ctx() can pick this
        # same job as an eviction candidate and call its own save_state(),
        # which now also takes this lock (P3b) — on the same thread, a
        # plain Lock would self-deadlock there.
        self.lock = threading.RLock()
        self._ctx_loading = False     # guarded by self.lock (S4)

    # ---------- persistence ----------
    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def save_state(self) -> None:
        # P3b: request threads and the worker-callback thread can call this
        # concurrently; without the lock, two interleaved builds/writes can
        # publish a torn or stale state.json. A per-call unique temp suffix
        # means one caller's write can never be clobbered by another's
        # before its own os.replace runs.
        with self.lock:
            if self.ctx is not None and self.ctx.scale_info.get("applied"):
                self.scale_info = self.ctx.scale_info
            state = {
                "status": self.status, "error": self.error, "created": self.created,
                "log": self.log[-400:],
                "scale_info": (lambda s: {k: v for k, v in s.items() if k != "marker_px"}
                               if s else None)(self.scale_info),
                "result": self.result, "ortho": self.ortho,
                "dem_info": self.dem_info,
            }
            tmp = self.state_path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self.state_path)

    def say(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log.append(line)
        self.log = self.log[-400:]
        print(line, flush=True)

    def set_status(self, status: str, error: str | None = None) -> None:
        # S3: status transitions must be atomic w.r.t. concurrent readers
        # (job_status/measure/ortho busy checks read self.status).
        with self.lock:
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
        from landslide.sfm import reconstruct
        with self.lock:
            if self.ctx is not None:
                touch(self)
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
            # informational GPS georeferencing (annotation only, never scale)
            try:
                from landslide.geo import attach_georef, load_gps
                gps = load_gps(self.dir / "photos")
                if gps:
                    attach_georef(ctx, gps, log=self.say)
            except Exception as e:
                self.say(f"[geo] georeferencing skipped: {e}")
            self.ctx = ctx
            evict_ctx()
            return ctx

    def start_ctx_reload(self) -> None:
        """Kick off a background reload of `self.ctx` if not already loaded
        or in flight (S4): the GET /api/jobs/{id} status poll must not block
        on the (multi-second) COLMAP reload, so it fires this once and
        returns the current snapshot with `ctx_loading=True` immediately;
        the next poll picks up the populated ctx."""
        with self.lock:
            if self.ctx is not None or self._ctx_loading:
                return
            self._ctx_loading = True

        def _work():
            try:
                self.ensure_ctx()
            except Exception as e:
                self.say(f"[ctx] background reload failed: {e}")
            finally:
                with self.lock:
                    self._ctx_loading = False

        threading.Thread(target=_work, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            out = {
                "id": self.id, "status": self.status, "error": self.error,
                "created": self.created, "log": self.log[-60:],
                "reconstructable": self.reconstructable,
                "ortho": self.ortho,
                "ctx_loading": self._ctx_loading,
            }
            if self.ctx is not None:
                from landslide.sfm import image_metadata
                out["images"] = image_metadata(self.ctx)
                out["scale"] = self.ctx.scale_info or None
                try:
                    from landslide.geo import geo_summary
                    out["geo"] = geo_summary(self.ctx)
                except Exception:
                    out["geo"] = None
            else:
                out["images"] = []
                out["scale"] = self.scale_info or None
            if self.result is not None:
                out["result"] = self.result
            return out


JOBS: OrderedDict[str, Job] = OrderedDict()
JOBS_LOCK = threading.Lock()
_photo_cache: OrderedDict[tuple, bytes] = OrderedDict()


def touch(job: Job) -> None:
    with JOBS_LOCK:
        JOBS.move_to_end(job.id)


def evict_ctx() -> None:
    """Drop the least-recently-used idle contexts (they reload on demand).

    B2: `ensure_ctx()` takes `job.lock` then (via this function) `JOBS_LOCK`.
    Taking `JOBS_LOCK` first and then blocking on a candidate's `job.lock`
    (the old body did this via `save_state()`) is the reverse order, so two
    threads picking each other's job as a candidate could deadlock forever,
    wedging `JOBS_LOCK` for every other route. Candidates are gathered under
    `JOBS_LOCK`, which is released before touching any job.lock, and each
    candidate's lock is a non-blocking try: a job in active use is simply
    left loaded and retried on the next eviction instead of being waited on.
    """
    with JOBS_LOCK:
        loaded = [j for j in JOBS.values() if j.ctx is not None]
        candidates = loaded[:-MAX_LOADED_CTX] if len(loaded) > MAX_LOADED_CTX else []
    dropped = False
    for job in candidates:
        if not job.lock.acquire(blocking=False):
            continue   # busy elsewhere right now; try again on the next eviction
        try:
            if job.ctx is None or job.status in ("measuring", "reconstructing", "orthorectifying"):
                continue
            job.ctx = None
            job.save_state()
            job.say("context unloaded (reloadable on demand)")
            dropped = True
        finally:
            job.lock.release()
    if dropped:
        gc.collect()   # hand the evicted point-cloud buffers back to the OS


_MODEL_FILES = ("cameras", "images", "points3D")


def _model_complete(sparse_dir: Path) -> bool:
    """True if at least one model directory under `sparse_dir` has all
    three COLMAP output files (P3d): `Job.reconstructable` accepts *any*
    file under `sparse/*/`, which is also true mid-write, so it isn't
    strict enough to decide whether an interrupted job's model is safe to
    promote to `ready` on server restart.
    """
    if not sparse_dir.is_dir():
        return False
    for sub in sparse_dir.iterdir():
        if not sub.is_dir():
            continue
        if all((sub / f"{name}.bin").exists() or (sub / f"{name}.txt").exists()
               for name in _MODEL_FILES):
            return True
    return False


def load_persisted_jobs() -> None:
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
                      result=state.get("result"), ortho=state.get("ortho"),
                      dem_info=state.get("dem_info"))
            if job.status in ("reconstructing", "measuring", "orthorectifying"):
                # a job that was mid-reconstruction (or mid-measure/-ortho,
                # which both require a finished model) when the server
                # stopped: only promote it if COLMAP finished writing a
                # complete model, not merely started one (P3d).
                if _model_complete(d / "work" / "sparse"):
                    job.status = "ready"
                else:
                    job.status = "error"
                    job.error = "interrupted before the reconstruction finished"
            JOBS[job.id] = job
    if JOBS:
        print(f"[server] reattached {len(JOBS)} job(s) from {DATA_DIR}", flush=True)

"""Process-isolated execution of the heavy worker functions (S2).

Centralizes the crash-recovery logic (`BrokenProcessPool` handling, pool
recreation, log-queue draining) so the three call sites in `server/routes.py`
(reconstruct/measure/ortho) don't each reimplement it. Lazily initialized —
importing this module must not itself spawn a `multiprocessing.Manager`
process, or every test that imports `server.routes` pays that cost.
"""
from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import Manager
from typing import Callable

from server.jobs import JOBS, JOBS_LOCK, Job

_state_lock = threading.Lock()
_manager = None
_log_queue = None
_pool: ProcessPoolExecutor | None = None
_drain_thread: threading.Thread | None = None

_LOW_RAM_BYTES = 4 * (1024 ** 3)


def _total_ram_bytes() -> int:
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0


def max_workers() -> int:
    """Pool size (F21/M8): 2 heavy workers x (1.5-3 GB SfM + dense stereo)
    can exceed a small field laptop's RAM well before either worker OOMs on
    its own; on a box with < 4 GB physical RAM, run one job at a time
    instead. `SLOPELENS_WORKERS` overrides for an operator who knows their
    hardware better than this heuristic.
    """
    env = os.environ.get("SLOPELENS_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    total = _total_ram_bytes()
    return 1 if total and total < _LOW_RAM_BYTES else 2


def _worker_mem_limit_bytes() -> int:
    """Per-worker RLIMIT_AS (F21): an allocation past this raises (a
    catchable) MemoryError inside the worker — surfaced as a normal job
    error via `submit_job`'s exception handling — instead of the kernel OOM
    -killing the process, which is exactly the crash F1 exists to survive
    but is nicer to avoid triggering at all. Sized from actual machine RAM
    (not a flat guess) so it neither starves a real measurement on a big
    box nor allows one worker to take down a small one: `total * 0.8`
    split evenly across the pool, leaving ~20% for the parent process + OS.
    """
    env = os.environ.get("SLOPELENS_WORKER_MEM_MB")
    if env:
        try:
            return max(256, int(env)) * 1024 * 1024
        except ValueError:
            pass
    total = _total_ram_bytes()
    if not total:
        return 0   # unknown RAM: no limit rather than guessing wrong
    return max(int(1.5 * 1024 ** 3), int(0.8 * total) // max(max_workers(), 1))


def _worker_init() -> None:
    limit = _worker_mem_limit_bytes()
    if not limit:
        return
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass   # e.g. platform without RLIMIT_AS — best-effort only


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=max_workers(), initializer=_worker_init)


def _drain_log_queue() -> None:
    while True:
        job_id, line = _log_queue.get()
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is not None:
            job.log.append(line)
            job.log = job.log[-400:]


def _ensure_started() -> None:
    global _manager, _log_queue, _pool, _drain_thread
    with _state_lock:
        if _manager is None:
            _manager = Manager()
            _log_queue = _manager.Queue()
        if _pool is None:
            _pool = _new_pool()
        if _drain_thread is None:
            _drain_thread = threading.Thread(target=_drain_log_queue, daemon=True)
            _drain_thread.start()


def _recreate_pool() -> None:
    """Swap in a fresh pool without calling `shutdown()` on the broken one.

    `shutdown()` on a pool that `ProcessPoolExecutor` itself is in the middle
    of tearing down (a worker crash) deadlocks: `terminate_broken` holds
    `self.shutdown_lock` while it sets exceptions on pending futures, which
    invokes this function's caller (`_cb`) *on that same thread*; re-entering
    `shutdown()` then blocks forever on the non-reentrant lock it already
    holds (F1). A broken pool is already tearing itself down, so just drop
    the reference — nothing to shut down.
    """
    global _pool
    with _state_lock:
        _pool = _new_pool()


def _crash_message(exc: BaseException) -> str:
    if isinstance(exc, BrokenProcessPool):
        return f"worker process crashed (native crash): {exc}"
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]


def submit_job(job: Job, fn: Callable, *extra_args,
               on_done: Callable[[Job, object, BaseException | None], None]) -> None:
    """Run `fn(job.id, job.dir/"photos", job.dir/"work", log_queue, *extra_args)`
    in the worker pool. `on_done(job, result, exc)` runs in the parent
    process once it finishes (success, a raised exception, or — if the
    worker process itself died — a `BrokenProcessPool`)."""
    _ensure_started()
    args = (job.id, job.dir / "photos", job.dir / "work", _log_queue, *extra_args)
    try:
        fut = _pool.submit(fn, *args)
    except BrokenProcessPool as e:
        _recreate_pool()
        on_done(job, None, e)
        return

    def _cb(f):
        try:
            result = f.result()
        except BaseException as e:  # noqa: BLE001 - surface any worker failure as a job error
            if isinstance(e, BrokenProcessPool):
                _recreate_pool()
            job.say(_crash_message(e))
            on_done(job, None, e)
            return
        on_done(job, result, None)

    fut.add_done_callback(_cb)

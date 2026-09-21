"""Process-isolated execution of the heavy worker functions (S2).

Centralizes the crash-recovery logic (`BrokenProcessPool` handling, pool
recreation, log-queue draining) so the three call sites in `server/routes.py`
(reconstruct/measure/ortho) don't each reimplement it. Lazily initialized —
importing this module must not itself spawn a `multiprocessing.Manager`
process, or every test that imports `server.routes` pays that cost.
"""
from __future__ import annotations

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


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=2)


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
    global _pool
    with _state_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
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

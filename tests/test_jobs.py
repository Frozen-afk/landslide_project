"""Unit tests for `server/jobs.py` lifecycle bookkeeping (P3).

Complements `tests/test_server.py` (which exercises the same behavior
through the HTTP layer where applicable). These tests call `Job`/module
functions directly since `save_state` concurrency and `load_persisted_jobs`
promotion logic don't go through a route.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from server import jobs
from server.jobs import JOBS, JOBS_LOCK, Job


# ---------- P3b: save_state must not publish torn JSON under concurrency ----------

def test_concurrent_save_state_never_publishes_torn_json():
    job = Job("test-" + uuid.uuid4().hex[:8])
    (job.dir / "photos").mkdir(parents=True)
    try:
        def hammer(i):
            job.log.append(f"line {i}")
            job.save_state()

        with ThreadPoolExecutor(max_workers=16) as tp:
            list(tp.map(hammer, range(100)))

        # every write must have been a complete, parseable JSON object
        json.loads(job.state_path.read_text())
        # no leftover unique-suffixed temp files
        assert list(job.dir.glob("state.tmp-*")) == []
    finally:
        import shutil
        shutil.rmtree(job.dir, ignore_errors=True)


# ---------- P3d: interrupted reconstruction must not resume as ready ----------

def _make_job_dir(base: Path, job_id: str, status: str) -> Path:
    d = base / job_id
    (d / "photos").mkdir(parents=True)
    (d / "work").mkdir()
    (d / "state.json").write_text(json.dumps({"status": status}))
    return d


def _reload(job_id: str, base, monkeypatch) -> Job:
    monkeypatch.setattr(jobs, "DATA_DIR", base)
    try:
        jobs.load_persisted_jobs()
        return JOBS[job_id]
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)


def test_empty_sparse_dir_resumes_as_error(tmp_path, monkeypatch):
    jid = "20990101-000000-aaaaaa"
    d = _make_job_dir(tmp_path, jid, "reconstructing")
    (d / "work" / "sparse" / "0").mkdir(parents=True)

    job = _reload(jid, tmp_path, monkeypatch)
    assert job.status == "error"
    assert "interrupted" in job.error


def test_partial_model_resumes_as_error(tmp_path, monkeypatch):
    jid = "20990101-000001-bbbbbb"
    d = _make_job_dir(tmp_path, jid, "measuring")
    sparse0 = d / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    (sparse0 / "points3D.bin").write_bytes(b"x")   # SfM died mid-write

    job = _reload(jid, tmp_path, monkeypatch)
    assert job.status == "error"
    assert "interrupted" in job.error


# ---------- P3b regression: save_state's new lock must not self-deadlock ----------

def test_ensure_ctx_evicting_itself_does_not_deadlock(monkeypatch):
    """ensure_ctx() holds `self.lock` for its whole body, including its
    evict_ctx() call; evict_ctx() can pick the very job being loaded as an
    eviction candidate (it hasn't been `touch()`-ed to the end of JOBS yet)
    and calls that job's own save_state() — which, after P3b, also takes
    `self.lock`. A plain (non-reentrant) Lock would hang here forever on
    the same thread; this proves the RLock switch avoids that.
    """
    monkeypatch.setattr(jobs, "MAX_LOADED_CTX", 1)

    job_a = Job("test-a-" + uuid.uuid4().hex[:8])
    job_b = Job("test-b-" + uuid.uuid4().hex[:8])
    for job in (job_a, job_b):
        (job.dir / "photos").mkdir(parents=True)
    sparse0 = job_a.dir / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    (sparse0 / "points3D.bin").write_bytes(b"x")
    (job_a.dir / "work" / "database.db").write_bytes(b"x")
    job_a.status = "ready"   # matches the real trigger (start_ctx_reload on a
                             # "ready" job) -- evict_ctx() skips busy statuses

    job_b.ctx = SimpleNamespace(scale_info={})   # already loaded, "older" slot
    job_b.status = "ready"
    with JOBS_LOCK:
        JOBS[job_a.id] = job_a   # inserted first -> not last in eviction order
        JOBS[job_b.id] = job_b

    fake_ctx_a = SimpleNamespace(scale_info={})
    monkeypatch.setattr("landslide.sfm.reconstruct", lambda *a, **kw: fake_ctx_a)

    try:
        result = {}

        def run():
            result["ctx"] = job_a.ensure_ctx()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "ensure_ctx() deadlocked evicting its own job"
        assert result.get("ctx") is fake_ctx_a
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_a.id, None)
            JOBS.pop(job_b.id, None)
        import shutil
        shutil.rmtree(job_a.dir, ignore_errors=True)
        shutil.rmtree(job_b.dir, ignore_errors=True)


# ---------- B2 regression: evict_ctx() must not invert the lock order ----------

def test_concurrent_ensure_ctx_does_not_deadlock_on_eviction(monkeypatch):
    """ensure_ctx() takes job.lock then (via evict_ctx) JOBS_LOCK. The old
    evict_ctx() took JOBS_LOCK then a candidate's job.lock (via save_state)
    -- the reverse order. Two jobs reloading concurrently, each an eviction
    candidate for the other, could then deadlock forever and wedge
    JOBS_LOCK for every route (list_jobs, touch, status polls).

    Forces the exact interleaving: job_b's evict_ctx runs first and must
    try to lock job_a (held by job_a's own ensure_ctx, delayed past that
    point) while still under the old code's JOBS_LOCK.
    """
    monkeypatch.setattr(jobs, "MAX_LOADED_CTX", 1)

    job_a = Job("test-a-" + uuid.uuid4().hex[:8])
    job_b = Job("test-b-" + uuid.uuid4().hex[:8])
    for job in (job_a, job_b):
        (job.dir / "photos").mkdir(parents=True)
    sparse0 = job_a.dir / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    (sparse0 / "points3D.bin").write_bytes(b"x")
    (job_a.dir / "work" / "database.db").write_bytes(b"x")
    job_a.status = "ready"

    job_old = Job("test-old-" + uuid.uuid4().hex[:8])
    job_old.dir.mkdir(parents=True)   # evict_ctx() saves state on eviction
    job_old.ctx = SimpleNamespace(scale_info={})   # already loaded, oldest slot
    job_old.status = "ready"
    job_b.ctx = SimpleNamespace(scale_info={})     # already loaded, next-oldest
    job_b.status = "ready"
    with JOBS_LOCK:
        JOBS[job_old.id] = job_old
        JOBS[job_a.id] = job_a     # inserted before job_b -> an older candidate
        JOBS[job_b.id] = job_b

    monkeypatch.setattr("landslide.sfm.reconstruct", lambda *a, **kw: SimpleNamespace(scale_info={}))

    real_evict_ctx = jobs.evict_ctx
    ctx_a_ready = threading.Event()

    def delayed_evict_ctx():
        if threading.current_thread().name == "A":
            # job_a.ctx is set by this point (ensure_ctx sets it right
            # before calling evict_ctx) and job_a.lock is still held
            # (ensure_ctx's outer `with self.lock`) -- signal B, then hold
            # both while B's evict_ctx reaches (and, pre-fix, blocks on)
            # job_a's lock.
            ctx_a_ready.set()
            time.sleep(0.2)
        real_evict_ctx()

    monkeypatch.setattr(jobs, "evict_ctx", delayed_evict_ctx)

    try:
        result = {}

        def run_a():
            result["a"] = job_a.ensure_ctx()

        def run_b():
            # job_b.ctx is already set, so its own ensure_ctx would short-
            # circuit; call evict_ctx() directly the way ensure_ctx does,
            # under job_b.lock, to reproduce the second thread's lock order
            # -- only once job_a's ctx assignment is confirmed done.
            assert ctx_a_ready.wait(timeout=5)
            with job_b.lock:
                jobs.evict_ctx()

        t_a = threading.Thread(target=run_a, name="A")
        t_b = threading.Thread(target=run_b, name="B")
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)
        assert not t_a.is_alive() and not t_b.is_alive(), \
            "deadlock: evict_ctx()'s lock order still inverts ensure_ctx()'s"
    finally:
        with JOBS_LOCK:
            for j in (job_a, job_b, job_old):
                JOBS.pop(j.id, None)
        import shutil
        shutil.rmtree(job_a.dir, ignore_errors=True)
        shutil.rmtree(job_b.dir, ignore_errors=True)
        shutil.rmtree(job_old.dir, ignore_errors=True)


def test_complete_model_resumes_as_ready(tmp_path, monkeypatch):
    jid = "20990101-000002-cccccc"
    d = _make_job_dir(tmp_path, jid, "orthorectifying")
    sparse0 = d / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    for name in ("cameras", "images", "points3D"):
        (sparse0 / f"{name}.bin").write_bytes(b"x")

    job = _reload(jid, tmp_path, monkeypatch)
    assert job.status == "ready"
    assert job.error is None

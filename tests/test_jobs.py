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

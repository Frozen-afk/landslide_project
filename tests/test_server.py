"""API tests for the FastAPI server (T3.1 — S1/S2/S3 coverage; S7 gap fill).

Fast tests (validation, 404s, the busy-409 race) monkeypatch the heavy
pipeline calls. One test drives the real `ProcessPoolExecutor` with a worker
that calls `os._exit(1)` to prove the literal T3.1 acceptance criterion —
"server survives a killed worker" — without mocking anything about process
isolation itself.
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.main as main
from server import executor
from server.jobs import JOBS, JOBS_LOCK, Job

client = TestClient(main.app)


@pytest.fixture
def ready_job():
    """A job with a fake-but-reconstructable on-disk layout, status=ready,
    no scale set — cheap to build, no real COLMAP output needed since these
    tests never touch `reconstruct()`."""
    job = Job("test-" + uuid.uuid4().hex[:8])
    (job.dir / "photos").mkdir(parents=True)
    sparse0 = job.dir / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    (job.dir / "work" / "database.db").write_bytes(b"x")
    (sparse0 / "points3D.bin").write_bytes(b"x")
    job.status = "ready"
    with JOBS_LOCK:
        JOBS[job.id] = job
    yield job
    with JOBS_LOCK:
        JOBS.pop(job.id, None)
    shutil.rmtree(job.dir, ignore_errors=True)


# ---------- S1: request validation ----------

def test_create_job_no_files_rejected():
    assert client.post("/api/jobs").status_code in (400, 422)


def test_create_job_too_many_files_rejected():
    files = [("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(201)]
    r = client.post("/api/jobs", files=files)
    assert r.status_code == 400
    assert "too many" in r.json()["detail"]


def test_create_job_bad_extension_rejected():
    r = client.post("/api/jobs", files=[("files", ("notes.txt", b"x", "text/plain"))])
    assert r.status_code == 400
    assert "not a photo" in r.json()["detail"]


# ---------- unknown-job 404s ----------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/jobs/nope"),
    ("get", "/api/jobs/nope/events"),
    ("post", "/api/jobs/nope/scale/aruco"),
    ("post", "/api/jobs/nope/scale/manual"),
    ("post", "/api/jobs/nope/measure"),
    ("post", "/api/jobs/nope/ortho"),
    ("delete", "/api/jobs/nope"),
])
def test_unknown_job_404(method, path):
    kwargs = {}
    if path.endswith("scale/aruco"):
        kwargs["json"] = {}
    elif path.endswith("scale/manual"):
        kwargs["json"] = {"length_m": 1.0,
                           "a": {"image": "x", "p1": [0, 0], "p2": [1, 1]},
                           "b": {"image": "x", "p1": [0, 0], "p2": [1, 1]}}
    elif path.endswith("measure"):
        kwargs["json"] = {"polygon": [[0, 0], [1, 0], [1, 1]]}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 404


# ---------- measure requires scale (T0.6/S1 regression) ----------

def test_measure_without_scale_rejected(ready_job):
    r = client.post(f"/api/jobs/{ready_job.id}/measure",
                    json={"polygon": [[0, 0], [1, 0], [1, 1]]})
    assert r.status_code == 400
    assert "scale" in r.json()["detail"]


# ---------- S3: busy check-and-set must be atomic ----------

def test_measure_busy_race_is_atomic(ready_job, monkeypatch):
    ready_job.scale_info = {"applied": True, "scale": 1.0}
    calls = []
    monkeypatch.setattr(
        "server.routes.executor.submit_job",
        lambda *a, **kw: calls.append(a))

    body = {"polygon": [[0, 0], [1, 0], [1, 1]]}

    def post():
        return client.post(f"/api/jobs/{ready_job.id}/measure", json=body)

    with ThreadPoolExecutor(max_workers=2) as tp:
        r1, r2 = [f.result() for f in
                  [tp.submit(post), tp.submit(post)]]

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (
        f"expected exactly one queued (200) and one busy (409), got {codes}")
    assert len(calls) == 1


# ---------- S2: a crashed worker must not take the server (or other jobs) down ----------

def _crash_worker(job_id, photos_dir, work_dir, queue):
    os._exit(1)  # simulate a native (pycolmap/OpenCV) hard crash, not a Python exception


def _ok_worker(job_id, photos_dir, work_dir, queue) -> dict:
    return {"ok": True}


def test_worker_crash_is_isolated_and_pool_recovers(ready_job):
    done = {}

    def on_done(job, result, exc):
        done["result"] = result
        done["exc"] = exc

    executor.submit_job(ready_job, _crash_worker, on_done=on_done)

    deadline = time.time() + 30
    while "exc" not in done and time.time() < deadline:
        time.sleep(0.1)
    assert "exc" in done, "crash callback never fired"
    assert done["exc"] is not None, "a killed worker must surface as a failure, not a silent success"

    # the pool must have recovered: a second, well-behaved job still completes
    ready_job2 = Job("test2-" + uuid.uuid4().hex[:8])
    try:
        done2 = {}
        executor.submit_job(ready_job2, _ok_worker,
                            on_done=lambda job, result, exc: done2.update(result=result, exc=exc))
        deadline = time.time() + 30
        while "result" not in done2 and time.time() < deadline:
            time.sleep(0.1)
        assert done2.get("result") == {"ok": True}, (
            f"pool did not recover after a killed worker: {done2}")
    finally:
        shutil.rmtree(ready_job2.dir, ignore_errors=True)

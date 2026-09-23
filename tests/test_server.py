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


# ---------- F14: photo import must not block the event loop ----------

def test_photo_upload_import_does_not_block_other_requests(monkeypatch):
    def slow_import(paths, dest, log=print):
        time.sleep(1.0)
        return [f"{i:03d}_x.jpg" for i in range(len(paths))]
    monkeypatch.setattr("server.routes.import_photos", slow_import)

    files = [("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(3)]
    with ThreadPoolExecutor(max_workers=2) as tp:
        fut = tp.submit(lambda: client.post("/api/jobs", files=files))
        time.sleep(0.2)   # let the upload land inside the slow import
        t0 = time.time()
        r = client.get("/api/jobs")
        dt = time.time() - t0
        upload = fut.result(timeout=10)
    assert r.status_code == 200
    assert dt < 0.5, f"GET /api/jobs took {dt:.2f}s while an import was in flight"
    assert upload.status_code == 200


# ---------- F21/M8: pool size / per-worker memory limit follow available RAM ----------

def test_max_workers_drops_to_one_below_4gb(monkeypatch):
    monkeypatch.delenv("SLOPELENS_WORKERS", raising=False)
    monkeypatch.setattr(executor, "_total_ram_bytes", lambda: 3 * 1024 ** 3)
    assert executor.max_workers() == 1
    monkeypatch.setattr(executor, "_total_ram_bytes", lambda: 8 * 1024 ** 3)
    assert executor.max_workers() == 2


def test_max_workers_env_override_wins(monkeypatch):
    monkeypatch.setenv("SLOPELENS_WORKERS", "3")
    monkeypatch.setattr(executor, "_total_ram_bytes", lambda: 1 * 1024 ** 3)
    assert executor.max_workers() == 3


def test_worker_mem_limit_scales_with_ram_and_worker_count(monkeypatch):
    monkeypatch.delenv("SLOPELENS_WORKER_MEM_MB", raising=False)
    monkeypatch.delenv("SLOPELENS_WORKERS", raising=False)
    monkeypatch.setattr(executor, "_total_ram_bytes", lambda: 8 * 1024 ** 3)
    limit_2 = executor._worker_mem_limit_bytes()
    assert limit_2 == pytest.approx(0.8 * 8 * 1024 ** 3 / 2)
    monkeypatch.setattr(executor, "_total_ram_bytes", lambda: 0)   # unknown RAM
    assert executor._worker_mem_limit_bytes() == 0


# ---------- F5: a re-scale must invalidate the stale ortho/dem/result ----------

def test_rescale_invalidates_ortho_dem_and_result(ready_job, monkeypatch):
    import types
    # ensure_ctx() short-circuits on a non-None ctx: no real COLMAP data
    # needed, just something with the attributes save_state()/aruco_scale
    # touch.
    ready_job.ctx = types.SimpleNamespace(scale_info={})
    ready_job.scale_info = {"applied": True, "scale": 1.0}
    ready_job.ortho = {"scale": 1.0, "u0": 0.0, "v0": 0.0, "res": 1.0}
    ready_job.dem_info = {"file": "dem.xyz", "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                          "t": [0, 0, 0], "rms_m": 0.1}
    ready_job.result = {"net_volume_m3": 1.0}
    monkeypatch.setattr(
        "server.routes.aruco_scale",
        lambda *a, **kw: {"applied": True, "scale": 2.0, "method": "aruco"})

    r = client.post(f"/api/jobs/{ready_job.id}/scale/aruco", json={})
    assert r.status_code == 200
    assert ready_job.ortho is None
    assert ready_job.dem_info is None
    assert ready_job.result is None

    calls = []
    monkeypatch.setattr("server.routes.executor.submit_job",
                        lambda *a, **kw: calls.append(a))
    r2 = client.post(f"/api/jobs/{ready_job.id}/ortho")
    assert r2.status_code == 200 and r2.json() == {"queued": True}
    assert len(calls) == 1


def test_ortho_measure_rejects_ortho_from_a_stale_scale(ready_job):
    ready_job.scale_info = {"applied": True, "scale": 2.0}
    ready_job.ortho = {"scale": 1.0, "u0": 0.0, "v0": 0.0, "res": 1.0}   # rendered pre-rescale
    body = {"polygon": [[0, 0], [1, 0], [1, 1]], "mode": "ortho"}
    r = client.post(f"/api/jobs/{ready_job.id}/measure", json=body)
    assert r.status_code == 409


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


# ---------- P3a: DELETE must not race a running worker ----------

def test_delete_busy_job_rejected_then_succeeds_once_ready(ready_job):
    ready_job.status = "measuring"
    r = client.delete(f"/api/jobs/{ready_job.id}")
    assert r.status_code == 409
    assert ready_job.dir.exists()
    with JOBS_LOCK:
        assert ready_job.id in JOBS

    ready_job.status = "ready"
    r2 = client.delete(f"/api/jobs/{ready_job.id}")
    assert r2.status_code == 200
    assert not ready_job.dir.exists()
    with JOBS_LOCK:
        assert ready_job.id not in JOBS


# ---------- P3c: shutdown must not wait out a running worker ----------

def _sleep_worker(job_id, photos_dir, work_dir, queue):
    import time as _t
    _t.sleep(30)
    return {"ok": True}


def test_shutdown_now_terminates_running_worker(ready_job):
    executor.submit_job(ready_job, _sleep_worker, on_done=lambda *a: None)

    deadline = time.time() + 10
    procs = []
    while time.time() < deadline and not procs:
        procs = list(executor._pool._processes.values())
        if not procs:
            time.sleep(0.05)
    assert procs, "no worker process spawned in time"

    t0 = time.time()
    executor.shutdown_now()
    dt = time.time() - t0
    assert dt <= 2.0, f"shutdown_now() took {dt:.2f}s"

    deadline = time.time() + 2
    while time.time() < deadline and any(p.is_alive() for p in procs):
        time.sleep(0.05)
    assert all(not p.is_alive() for p in procs), "worker process(es) outlived shutdown_now()"


def test_app_lifespan_shutdown_is_fast_with_a_running_worker(monkeypatch):
    monkeypatch.setattr(main, "load_persisted_jobs", lambda: None)
    job = Job("shut-" + uuid.uuid4().hex[:8])
    (job.dir / "photos").mkdir(parents=True)
    with JOBS_LOCK:
        JOBS[job.id] = job
    try:
        with TestClient(main.app) as c:
            executor.submit_job(job, _sleep_worker, on_done=lambda *a: None)
            time.sleep(0.3)   # let the worker process actually start
            t0 = time.time()
        dt = time.time() - t0
        assert dt < 5.0, f"app shutdown took {dt:.2f}s with a running worker"
    finally:
        with JOBS_LOCK:
            JOBS.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

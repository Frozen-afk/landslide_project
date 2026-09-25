"""Capture-session backend (GUIDED_CAPTURE_PLAN.md S2, §9.1/§9.2). Covers
criterion 6(a-d): idempotent PUT, sha256-mismatch 409, `capturing` jobs
surviving `load_persisted_jobs`, and finalize recoverability after a
simulated crash. Real per-photo checks run (small synthetic fixtures,
tests/fixtures/capture_t1/) — only the heavy reconstruction submit is
monkeypatched out, same pattern as tests/test_server.py.
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import server.main as main
from server import jobs as jobs_mod
from server.jobs import JOBS, JOBS_LOCK

client = TestClient(main.app)

F = Path(__file__).resolve().parent / "fixtures" / "capture_t1"


@pytest.fixture
def capture_job():
    r = client.post("/api/jobs/capture", json={"marker_side_m": 0.5})
    assert r.status_code == 200
    job_id = r.json()["id"]
    yield job_id
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if job is not None:
        shutil.rmtree(job.dir, ignore_errors=True)


def _put(job_id, photo_uuid, fixture_name, seq, arm="A", filename=None):
    data = (F / fixture_name).read_bytes()
    meta = json.dumps({"seq": seq, "arm": arm, "client": {}})
    return client.put(f"/api/jobs/{job_id}/capture/photos/{photo_uuid}",
                      files={"file": (filename or fixture_name, data, "image/jpeg")},
                      data={"meta": meta})


# ---------- 6(a): same uuid twice -> one manifest entry ----------

def test_same_uuid_put_twice_is_idempotent(capture_job):
    r1 = _put(capture_job, "u1", "overlap_a.jpg", 0)
    assert r1.status_code == 200
    r2 = _put(capture_job, "u1", "overlap_a.jpg", 0)
    assert r2.status_code == 200
    assert r1.json() == r2.json()

    snap = client.get(f"/api/jobs/{capture_job}/capture").json()
    assert len(snap["photos"]) == 1


# ---------- 6(b): same uuid, different sha256 -> 409 ----------

def test_same_uuid_different_content_is_409(capture_job):
    r1 = _put(capture_job, "u1", "overlap_a.jpg", 0)
    assert r1.status_code == 200
    r2 = _put(capture_job, "u1", "overlap_b.jpg", 0)
    assert r2.status_code == 409


# ---------- capture-session plumbing sanity ----------

def test_put_rejects_non_jpeg():
    r = client.post("/api/jobs/capture", json={"marker_side_m": 0.5})
    job_id = r.json()["id"]
    try:
        r = client.put(f"/api/jobs/{job_id}/capture/photos/u1",
                       files={"file": ("photo.png", b"x", "image/png")},
                       data={"meta": json.dumps({"seq": 0, "arm": "A", "client": {}})})
        assert r.status_code == 400
    finally:
        with JOBS_LOCK:
            job = JOBS.pop(job_id, None)
        if job is not None:
            shutil.rmtree(job.dir, ignore_errors=True)


def test_delete_excludes_photo_and_updates_sequence(capture_job):
    _put(capture_job, "u1", "overlap_a.jpg", 0)
    r = client.delete(f"/api/jobs/{capture_job}/capture/photos/u1")
    assert r.status_code == 200
    snap = client.get(f"/api/jobs/{capture_job}/capture").json()
    entry = next(p for p in snap["photos"] if p["uuid"] == "u1")
    assert entry["excluded"] is True
    assert snap["sequence"]["total"] == 0


def test_put_after_capturing_status_leaves_is_rejected(capture_job, monkeypatch):
    for i in range(3):
        r = _put(capture_job, f"u{i}", "overlap_a.jpg", i)
        assert r.status_code == 200
    monkeypatch.setattr("server.routes.executor.submit_job", lambda *a, **kw: None)
    r = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={})
    assert r.status_code == 200, r.text

    r = _put(capture_job, "u9", "overlap_a.jpg", 9)
    assert r.status_code == 409


# ---------- 6(c): load_persisted_jobs keeps a `capturing` job + its photos ----------

def test_load_persisted_jobs_reattaches_capturing_session(tmp_path, monkeypatch):
    jid = "20990101-000003-dddddd"
    d = tmp_path / jid
    (d / "photos").mkdir(parents=True)   # empty, per §9.1 -- reattach marker
    (d / "capture" / "originals").mkdir(parents=True)
    manifest = {
        "version": 1, "marker_side_m": 0.5, "marker_dict": "auto",
        "has_pre_event_dem": None, "roi": None,
        "photos": [{"uuid": "u1", "seq": 0, "arm": "A", "sha256": "x",
                    "client": {}, "server": {}, "verdict": "ok", "reasons": [],
                    "excluded": False}],
        "sequence": {"total": 1}, "preview": None, "finalized": None,
    }
    (d / "capture" / "session.json").write_text(json.dumps(manifest))
    (d / "state.json").write_text(json.dumps({"status": "capturing"}))

    monkeypatch.setattr(jobs_mod, "DATA_DIR", tmp_path)
    try:
        jobs_mod.load_persisted_jobs()
        job = JOBS[jid]
        assert job.status == "capturing"
        assert job.capture == manifest
    finally:
        with JOBS_LOCK:
            JOBS.pop(jid, None)


# ---------- 6(d): finalize is recoverable after a simulated crash ----------

def test_finalize_recoverable_after_simulated_crash(capture_job, monkeypatch):
    for i in range(3):
        r = _put(capture_job, f"u{i}", "overlap_a.jpg", i)
        assert r.status_code == 200

    def boom(*a, **kw):
        raise RuntimeError("simulated crash mid-import")

    monkeypatch.setattr("server.routes.import_photos", boom)
    r = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={})
    assert r.status_code == 400
    with JOBS_LOCK:
        job = JOBS[capture_job]
    assert job.status == "error"
    assert job.capture["finalized"] is None   # never reached the success write

    monkeypatch.undo()   # restore the real import_photos
    monkeypatch.setattr("server.routes.executor.submit_job", lambda *a, **kw: None)
    r2 = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={})
    assert r2.status_code == 200, r2.text
    assert job.status == "reconstructing"
    assert job.capture["finalized"] is not None
    assert any((job.dir / "photos").iterdir())

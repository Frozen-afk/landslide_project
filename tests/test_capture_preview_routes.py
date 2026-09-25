"""S4 backend wiring (GUIDED_CAPTURE_PLAN.md §9.2): the `/capture/preview`
endpoint's lifecycle, finalize's preview-blocking gate, the delete-while-
running guard, and stale-on-restart. The heavy reconstruction itself
(`worker.run_preview`) is monkeypatched out via `executor.submit_job`, same
pattern as `tests/test_capture_session.py` — this file is about the
server-side plumbing around a preview run, not the reconstruction itself
(covered by `tests/test_capture_preview.py` and `tests/test_sfm.py`).
"""
from __future__ import annotations

import json
import shutil
import sys
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


def _put(job_id, photo_uuid, fixture_name, seq, arm="A"):
    data = (F / fixture_name).read_bytes()
    meta = json.dumps({"seq": seq, "arm": arm, "client": {}})
    return client.put(f"/api/jobs/{job_id}/capture/photos/{photo_uuid}",
                      files={"file": (fixture_name, data, "image/jpeg")},
                      data={"meta": meta})


def _put_three(job_id):
    for i in range(3):
        r = _put(job_id, f"u{i}", "overlap_a.jpg", i)
        assert r.status_code == 200


def _fake_submit(report=None, exc=None):
    """Runs `on_done` synchronously with a canned result, in place of the
    real `executor.submit_job` (which would spawn `worker.run_preview` in
    a real process-pool worker)."""
    def _submit(job, fn, *extra_args, on_done):
        on_done(job, report, exc)
    return _submit


def test_preview_requires_at_least_3_kept_photos(capture_job):
    _put(capture_job, "u0", "overlap_a.jpg", 0)
    r = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r.status_code == 400


def test_preview_runs_and_stores_the_report(capture_job, monkeypatch):
    _put_three(capture_job)
    report = {"checks": [], "blocking": [], "n_registered": 3}
    monkeypatch.setattr("server.routes.executor.submit_job", _fake_submit(report))

    r = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r.status_code == 200
    assert r.json() == {"queued": True}

    snap = client.get(f"/api/jobs/{capture_job}/capture").json()
    assert snap["preview"]["status"] == "done"
    assert snap["preview"]["report"] == report
    assert snap["preview"]["error"] is None


def test_preview_failure_is_recorded_as_an_error_not_a_500(capture_job, monkeypatch):
    _put_three(capture_job)
    monkeypatch.setattr("server.routes.executor.submit_job",
                        _fake_submit(exc=RuntimeError("SfM failed")))

    r = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r.status_code == 200

    snap = client.get(f"/api/jobs/{capture_job}/capture").json()
    assert snap["preview"]["status"] == "error"
    assert "SfM failed" in snap["preview"]["error"]


def test_preview_request_while_running_sets_rerun_after(capture_job, monkeypatch):
    _put_three(capture_job)
    calls = {"n": 0}

    def _submit(job, fn, *extra_args, on_done):
        calls["n"] += 1
        # deliberately never call on_done — simulates a still-running preview

    monkeypatch.setattr("server.routes.executor.submit_job", _submit)
    r1 = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r1.status_code == 200 and r1.json() == {"queued": True}
    assert calls["n"] == 1

    r2 = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r2.status_code == 200 and r2.json() == {"running": True}
    assert calls["n"] == 1   # no second submit while one is in flight

    snap = client.get(f"/api/jobs/{capture_job}/capture").json()
    assert snap["preview"]["status"] == "running"
    assert snap["preview"]["rerun_after"] is True


def test_finalize_blocks_on_a_predicted_blocking_check_unless_overridden(capture_job, monkeypatch):
    _put_three(capture_job)
    report = {"checks": [{"id": "G4_collinear", "level": "block", "text": "walk a curve"}],
             "blocking": ["G4_collinear"]}
    monkeypatch.setattr("server.routes.executor.submit_job", _fake_submit(report))
    r = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r.status_code == 200

    r = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["blocking"] == [{"id": "G4_collinear", "text": "walk a curve"}]

    monkeypatch.setattr("server.routes.executor.submit_job", lambda *a, **kw: None)
    r = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={"override": True})
    assert r.status_code == 200, r.text


def test_finalize_unaffected_by_a_stale_or_missing_preview(capture_job, monkeypatch):
    _put_three(capture_job)
    monkeypatch.setattr("server.routes.executor.submit_job", lambda *a, **kw: None)
    # no preview was ever run — same as before S4
    r = client.post(f"/api/jobs/{capture_job}/capture/finalize", json={})
    assert r.status_code == 200, r.text


def test_delete_job_refuses_while_a_preview_is_running(capture_job, monkeypatch):
    _put_three(capture_job)

    def _submit(job, fn, *extra_args, on_done):
        pass   # never resolves

    monkeypatch.setattr("server.routes.executor.submit_job", _submit)
    r = client.post(f"/api/jobs/{capture_job}/capture/preview")
    assert r.status_code == 200

    r = client.delete(f"/api/jobs/{capture_job}")
    assert r.status_code == 409


def test_load_persisted_jobs_marks_a_running_preview_stale(tmp_path, monkeypatch):
    jid = "20990101-000004-eeeeee"
    d = tmp_path / jid
    (d / "photos").mkdir(parents=True)
    (d / "capture" / "originals").mkdir(parents=True)
    manifest = {
        "version": 1, "marker_side_m": 0.5, "marker_dict": "auto",
        "has_pre_event_dem": None, "roi": None, "photos": [],
        "sequence": {}, "finalized": None,
        "preview": {"status": "running", "started": 1.0, "finished": None,
                    "n_photos": 3, "report": None, "error": None},
    }
    (d / "capture" / "session.json").write_text(json.dumps(manifest))
    (d / "state.json").write_text(json.dumps({"status": "capturing"}))

    monkeypatch.setattr(jobs_mod, "DATA_DIR", tmp_path)
    try:
        jobs_mod.load_persisted_jobs()
        job = JOBS[jid]
        assert job.capture["preview"]["status"] == "stale"
    finally:
        with JOBS_LOCK:
            JOBS.pop(jid, None)

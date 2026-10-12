"""Multi-view outline API (server/routes.py): per-photo create / update /
delete / retrieve, persistence across a restart, progress by POV group, and
the 4 viewpoints x 2 photos gate on a final measurement. Runs on the analytic
scene from test_multiview.py — no COLMAP, no worker pool."""
from __future__ import annotations

import json
import shutil
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_multiview import FOUR_BY_TWO, _ctx, _disc

import server.main as main
from server import jobs
from server.jobs import JOBS, JOBS_LOCK, Job

client = TestClient(main.app)


@pytest.fixture
def job():
    j = Job("test-" + uuid.uuid4().hex[:8])
    (j.dir / "photos").mkdir(parents=True)
    sparse0 = j.dir / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    (j.dir / "work" / "database.db").write_bytes(b"x")
    for name in ("cameras", "images", "points3D"):
        (sparse0 / f"{name}.bin").write_bytes(b"x")
    j.status = "ready"
    j.ctx = _ctx(FOUR_BY_TWO)
    j.scale_info = {"applied": True, "scale": 1.0}
    with JOBS_LOCK:
        JOBS[j.id] = j
    yield j
    with JOBS_LOCK:
        JOBS.pop(j.id, None)
    shutil.rmtree(j.dir, ignore_errors=True)


def _put(job, image, polygon=None, **kw):
    poly = _disc(job.ctx.views[image]) if polygon is None else polygon
    return client.put(f"/api/jobs/{job.id}/annotations/{image}",
                      json={"polygon": poly, **kw})


def test_crud_roundtrip_is_idempotent(job):
    url = f"/api/jobs/{job.id}/annotations"
    assert client.get(url).json()["annotations"] == {}

    r = _put(job, "v00")
    assert r.status_code == 200 and r.json()["annotation"]["status"] == "accepted"
    rev = r.json()["rev"]
    assert _put(job, "v00").json()["rev"] == rev, "an identical re-PUT is a no-op"

    moved = (np.asarray(_disc(job.ctx.views["v00"])) + 5.0).tolist()
    r = _put(job, "v00", moved, status="draft")                       # update
    assert r.json()["rev"] == rev + 1 and r.json()["annotation"]["status"] == "draft"

    one = client.get(f"{url}/v00").json()                             # retrieve
    assert np.allclose(one["annotation"]["polygon"], moved)
    assert one["view"]["group"] >= 1

    assert client.delete(f"{url}/v00").json()["rev"] == rev + 2       # delete
    assert client.delete(f"{url}/v00").json()["rev"] == rev + 2, "deleting twice is fine"
    assert client.get(f"{url}/v00").json()["annotation"] is None
    assert client.get(url).json()["annotations"] == {}


def test_validation(job):
    url = f"/api/jobs/{job.id}/annotations"
    assert _put(job, "nope", [[0, 0], [1, 0], [1, 1]]).status_code == 404
    assert client.get(f"{url}/nope").status_code == 404
    assert _put(job, "v00", [[0, 0], [1, 1]]).status_code == 422
    assert _put(job, "v00", [[0, 0], [9e6, 0], [0, 9e6]]).status_code == 422
    assert _put(job, "v00", [[0, 0], [1, 0], [1, 1]], status="bogus").status_code == 422
    assert _put(job, "v00", [], status="skipped").status_code == 200   # a skip needs no polygon
    assert client.get("/api/jobs/nope/annotations").status_code == 404
    assert client.delete("/api/jobs/nope/annotations/v00").status_code == 404


def test_progress_suggestions_and_requirement(job):
    url = f"/api/jobs/{job.id}/annotations"
    s = client.get(url).json()["summary"]
    assert len(s["groups"]) == 4 and not s["requirement"]["met"]
    assert len({x["group"] for x in s["suggestions"][:4]}) == 4

    # follow the suggestions: 8 accepted outlines complete 4 x 2
    for _ in range(8):
        nxt = client.get(url).json()["summary"]["suggestions"][0]["image"]
        assert _put(job, nxt).status_code == 200
    s = client.get(url).json()["summary"]
    assert s["requirement"]["met"] and s["suggestions"] == []
    assert all(g["n_accepted"] == 2 for g in s["groups"])
    assert all(a["iou"] > 0.9 and not a["rejected"] for a in s["agreement"].values())
    assert s["fusion"]["area_best_m2"] == pytest.approx(np.pi * 16, rel=0.08)

    # a draft or a skipped photo does not count
    _put(job, "v00", status="draft")
    assert not client.get(url).json()["summary"]["requirement"]["met"]


def test_disagreeing_outline_is_reported_with_an_instruction(job):
    for n in job.ctx.views:
        _put(job, n)
    bad = (np.asarray(_disc(job.ctx.views["v02"])) + [260.0, 0.0]).tolist()
    _put(job, "v02", bad)
    s = client.get(f"/api/jobs/{job.id}/annotations").json()["summary"]
    assert s["agreement"]["v02"]["rejected"]
    assert not s["requirement"]["met"], "a rejected outline does not count toward 4 x 2"
    assert any("v02" in t for t in s["instructions"])


def test_projected_region_is_offered_as_a_suggestion(job):
    for n in ("v00", "v02", "v04", "v06"):
        _put(job, n)
    r = client.get(f"/api/jobs/{job.id}/annotations/v03").json()
    assert r["annotation"] is None
    for lvl in ("lower", "best", "upper"):
        assert len(r["projected"][lvl]) >= 3
    truth = np.asarray(_disc(job.ctx.views["v03"], n=360))
    poly = np.asarray(r["projected"]["best"])
    d = np.linalg.norm(poly[:, None] - truth[None], axis=2).min(axis=1)
    assert d.mean() < 8.0


def test_annotations_survive_a_restart(job, monkeypatch):
    _put(job, "v00")
    _put(job, "v03", status="draft")
    _put(job, "v05", [], status="skipped")
    state = json.loads(job.state_path.read_text())
    assert set(state["annotations"]) == {"v00", "v03", "v05"}

    with JOBS_LOCK:
        JOBS.pop(job.id)
    monkeypatch.setattr(jobs, "DATA_DIR", job.dir.parent)
    # load_persisted_jobs only reattaches digit-named job dirs
    digit = job.dir.parent / ("2099" + uuid.uuid4().hex[:8])
    shutil.copytree(job.dir, digit)
    try:
        jobs.load_persisted_jobs()
        back = JOBS[digit.name]
        assert back.annotations == state["annotations"]
        assert back.annotations_rev == state["annotations_rev"] == 3
    finally:
        with JOBS_LOCK:
            for k in [k for k in JOBS if k != job.id and not k.startswith("test-")]:
                if JOBS[k].dir == digit or k == digit.name:
                    JOBS.pop(k)
        shutil.rmtree(digit, ignore_errors=True)


def test_saved_job_without_annotations_loads(tmp_path, monkeypatch):
    # a state.json written before multi-view existed has neither key
    jid = "20990101-000009-eeeeee"
    d = tmp_path / jid
    (d / "photos").mkdir(parents=True)
    sparse0 = d / "work" / "sparse" / "0"
    sparse0.mkdir(parents=True)
    for name in ("cameras", "images", "points3D"):
        (sparse0 / f"{name}.bin").write_bytes(b"x")
    (d / "state.json").write_text(json.dumps(
        {"status": "measuring", "result": None, "scale_info": {"applied": True, "scale": 2.0}}))
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path)
    try:
        jobs.load_persisted_jobs()
        j = JOBS[jid]
        assert j.annotations == {} and j.annotations_rev == 0
        # interrupted mid-measure: back to ready, and it says what happened
        assert j.status == "ready" and "interrupted" in j.error
    finally:
        with JOBS_LOCK:
            JOBS.pop(jid, None)


def test_final_measurement_needs_four_by_two_unless_indicative(job, monkeypatch):
    calls = []
    monkeypatch.setattr("server.routes.executor.submit_job",
                        lambda *a, **kw: calls.append(a))
    url = f"/api/jobs/{job.id}/measure"

    r = client.post(url, json={"multiview": True})
    assert r.status_code == 400 and "outline" in r.json()["detail"]

    _put(job, "v00")
    _put(job, "v02")
    r = client.post(url, json={"multiview": True})
    assert r.status_code == 409
    assert r.json()["detail"]["requirement"]["n_annotations"] == 2
    assert not calls and job.status == "ready"

    r = client.post(url, json={"multiview": True, "indicative": True})
    assert r.status_code == 200 and r.json() == {"queued": True}
    spec = calls[0][4]
    assert [a["image"] for a in spec["annotations"]] == ["v00", "v02"]
    assert spec["annotations_rev"] == job.annotations_rev

    # duplicate submission while that one is running: refused, not queued twice
    r = client.post(url, json={"multiview": True, "indicative": True})
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)
    assert len(calls) == 1

    job.status = "ready"
    for n in job.ctx.views:
        _put(job, n)
    r = client.post(url, json={"multiview": True})
    assert r.status_code == 200 and len(calls) == 2
    assert len(calls[1][4]["annotations"]) == 8


def test_legacy_single_photo_measure_body_is_unchanged(job, monkeypatch):
    calls = []
    monkeypatch.setattr("server.routes.executor.submit_job",
                        lambda *a, **kw: calls.append(a))
    _put(job, "v00")          # stored outlines must not leak into a legacy request
    r = client.post(f"/api/jobs/{job.id}/measure",
                    json={"polygon": [[0, 0], [9, 0], [9, 9]], "image": "v01"})
    assert r.status_code == 200
    spec = calls[0][4]
    assert "annotations" not in spec and spec["image"] == "v01" and spec["mode"] == "photo"
    job.status = "ready"
    assert client.post(f"/api/jobs/{job.id}/measure", json={}).status_code == 400

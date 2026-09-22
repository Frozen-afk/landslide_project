"""F7/F12 regression tests: no real COLMAP run needed — `_run_attempt` is
monkeypatched, and F12's helpers are exercised directly on tiny synthetic
camera rigs."""
from pathlib import Path

import numpy as np
import pytest

from landslide import sfm as sfm_mod

ROOT = Path(__file__).resolve().parents[1]
CACHED_SPARSE8 = ROOT / "data" / "bench" / "sparse8" / "work" / "sparse"


def _load_cached_reconstruction():
    import pycolmap
    model_dir = next(CACHED_SPARSE8.iterdir())
    return pycolmap.Reconstruction(str(model_dir))


@pytest.mark.skipif(not CACHED_SPARSE8.is_dir(),
                    reason="needs the cached sparse8 benchmark reconstruction")
def test_reconstruct_writes_the_best_attempt_to_disk_not_the_last(tmp_path, monkeypatch):
    """F7: the ladder tries a worse attempt AFTER a decent one whenever the
    decent one didn't hit the 'done' (~all images, well-triangulated)
    threshold — `reconstruct()` must leave the BEST attempt on disk, not
    whichever one happened to run last."""
    good = _load_cached_reconstruction()
    bad = _load_cached_reconstruction()
    for pid in list(bad.points3D.keys())[:-5]:   # strip to 5 points: unusable
        bad.delete_point3D(pid)
    good_pts, bad_pts = len(good.points3D), len(bad.points3D)
    assert good_pts > 240 > bad_pts               # sane preconditions for _attempt_score

    calls = {"n": 0}

    def fake_run_attempt(*a, **kw):
        calls["n"] += 1
        rec = good if calls["n"] == 1 else bad
        return rec, sfm_mod._prop(rec, "num_reg_images"), 9999.0

    n_photos = 12   # nreg=8 must stay < 0.9*n (not "done") and >= 0.5*n (not fatal)
    monkeypatch.setattr(
        sfm_mod, "_build_attempts",
        lambda n, size: [
            {"label": "first (good)", "matcher": "exhaustive", "overlap": 12,
             "size": size, "shared_camera": False},
            {"label": "second (bad)", "matcher": "exhaustive", "overlap": 12,
             "size": size, "shared_camera": False},
        ])
    monkeypatch.setattr(sfm_mod, "_run_attempt", fake_run_attempt)

    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    for i in range(n_photos):
        (photos_dir / f"{i}.jpg").write_bytes(b"x")
    workdir = tmp_path / "work"

    ctx = sfm_mod.reconstruct(photos_dir, workdir, reuse=False, log=lambda *_: None)
    assert len(ctx.sparse) == good_pts
    assert calls["n"] == 2   # both attempts ran; the ladder didn't stop early

    # simulate a restart: reload straight from disk, the way ensure_ctx()
    # does, and confirm the BEST attempt is what's actually there
    (workdir / "database.db").touch()
    ctx2 = sfm_mod.reconstruct(photos_dir, workdir, reuse=True, log=lambda *_: None)
    assert len(ctx2.sparse) == good_pts, \
        "disk held the worse (last) attempt, not the best one"


# ---------- F12: per-camera focal spread / camera-path collinearity ----------

def test_focal_spread_flags_a_diverged_camera():
    import pycolmap
    rec = pycolmap.Reconstruction()
    good_cam = pycolmap.Camera.create_from_model_id(
        1, pycolmap.CameraModelId.SIMPLE_PINHOLE, 1300.0, 800, 600)
    bad_cam = pycolmap.Camera.create_from_model_id(
        2, pycolmap.CameraModelId.SIMPLE_PINHOLE, 2400.0, 800, 600)  # +85%, F12's nadir repro
    rec.add_camera(good_cam)
    rec.add_camera(bad_cam)
    lo, hi, ratio = sfm_mod._focal_spread(rec)
    assert lo == pytest.approx(1300.0) and hi == pytest.approx(2400.0)
    assert ratio > sfm_mod.FOCAL_SPREAD_RATIO_BAD


def test_focal_spread_is_one_when_cameras_agree():
    import pycolmap
    rec = pycolmap.Reconstruction()
    for i in range(3):
        rec.add_camera(pycolmap.Camera.create_from_model_id(
            i + 1, pycolmap.CameraModelId.SIMPLE_PINHOLE, 1300.0, 800, 600))
    _, _, ratio = sfm_mod._focal_spread(rec)
    assert ratio == pytest.approx(1.0)

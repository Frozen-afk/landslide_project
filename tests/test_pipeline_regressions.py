"""Regression tests for the Tier 0 correctness fixes (see implementation_plan.md)."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest
from PIL import Image

from landslide.densify import dense_cloud
from landslide.pipeline import import_photos
from landslide.sfm import ImageView, ReconCtx
from landslide.volume import prism_volume, select_region


# ---------- T0.1: EXIF focal prior survives the re-encode ----------

def test_import_preserves_exif_focal_length(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    im = Image.new("RGB", (640, 480), (120, 120, 120))
    exif = im.getexif()
    exif[271] = "TestMake"          # Make
    exif[272] = "TestModel"         # Model
    exif[37386] = 4.2               # FocalLength
    path = src / "a.jpg"
    im.save(path, "JPEG", exif=exif.tobytes())
    other_paths = [src / f"b{i}.jpg" for i in range(2)]
    for p in other_paths:
        Image.new("RGB", (640, 480), (60, 60, 60)).save(p, "JPEG")

    out_dir = tmp_path / "photos"
    names = import_photos([path, *other_paths], out_dir, log=lambda *_: None)
    a_name = next(n for n in names if "a" in n)

    saved = Image.open(out_dir / a_name)
    saved_exif = dict(saved.getexif())
    assert saved_exif.get(37386) == 4.2
    assert saved_exif.get(272) == "TestModel"


# ---------- T0.2: dense cache is rejected when the fingerprint mismatches ----------

def test_stale_dense_cache_is_rejected(tmp_path, monkeypatch):
    import landslide.densify as densify_mod

    ctx = ReconCtx(rec=None, views={}, sparse=np.zeros((1, 3)),
                   sparse_colors=np.zeros((1, 3)), photos_dir=tmp_path,
                   workdir=tmp_path, fingerprint="aaaaaaaa")
    cache = tmp_path / "dense_1280_aaaaaaaa.npz"
    np.savez_compressed(cache, points=np.ones((5, 3)), colors=np.ones((5, 3)),
                        fingerprint="bbbbbbbb")   # written by a different pose set

    # ctx.views is empty, so dense_cloud has nothing to fuse regardless —
    # a trusted cache would have loaded the 5 stale points; a rejected one
    # falls through to "no registered views" and returns empty
    out = dense_cloud(ctx, log=lambda *_: None)
    assert len(out["points"]) == 0


# ---------- T0.3: occlusion — a background plane behind a ridge is excluded ----------

def _view(R, t, K, width, height):
    return ImageView(name="cam0", image_id=0, camera_id=0, R=R, t=t, K=K,
                     dist=np.zeros(5), width=width, height=height,
                     path=Path("."))


def test_select_region_excludes_occluded_background():
    width, height = 800, 600
    K = np.array([[800.0, 0, 400.0], [0, 800.0, 300.0], [0, 0, 1]])
    R = np.eye(3)
    t = np.array([0.0, 0.0, 5.0])       # camera center at world (0,0,-5)

    # a foreground ridge (depth ~2) directly in the camera's line of sight,
    # a tight grid so every raster cell near the optical axis has a front
    # sample — including the exact cell the background point falls in
    gx, gy = np.meshgrid(np.arange(-0.03, 0.03, 0.005), np.arange(-0.03, 0.03, 0.005))
    ridge = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, -3.0)])
    # a background plane point on the SAME optical ray (depth ~10) — would
    # project into the polygon too, but sits behind the ridge
    background = np.array([[0.0, 0.0, 5.0]])
    pts = np.vstack([ridge, background])

    ctx = ReconCtx(rec=None, views={"cam0": _view(R, t, K, width, height)},
                   sparse=pts, sparse_colors=np.zeros_like(pts),
                   photos_dir=Path("."), workdir=Path("."))
    ctx.dense = {"points": pts, "colors": np.zeros_like(pts)}
    ctx._covis = Counter()    # extra_views=0 -> unused, but avoids touching ctx.rec

    polygon = np.array([[350, 250], [450, 250], [450, 350], [350, 350]])
    _, _, interior, _ = select_region(ctx, "cam0", polygon, rim_px=200,
                                      rim_inner_px=0)
    n_ridge = len(ridge)
    assert interior[:n_ridge].any()      # some ridge points selected
    assert not interior[n_ridge]         # occluded background point excluded


# ---------- T0.4: symmetric clip keeps legitimate cuts, drops floaters ----------

def test_symmetric_low_clip_keeps_deep_cuts_drops_floaters():
    r_poly = 5.0
    xs = np.arange(-r_poly, r_poly, 0.25)
    X, Y = np.meshgrid(xs, xs)
    r = np.hypot(X, Y)
    inside = r <= r_poly
    depth = 2.0 * (1.0 - (r[inside] / r_poly) ** 2)   # 0 at rim, 2 m at centre
    interior = np.stack([X[inside], Y[inside], -depth], axis=1)
    theta = np.linspace(0, 2 * np.pi, 60, endpoint=False)
    rim = np.stack([r_poly * np.cos(theta), r_poly * np.sin(theta),
                    np.zeros_like(theta)], axis=1)
    floater = np.array([[0.1, 0.1, -40.0]])   # isolated stereo junk far below

    res_clean = prism_volume(interior, rim, log=lambda *_: None)
    res_floater = prism_volume(np.vstack([interior, floater]), rim,
                               log=lambda *_: None)
    assert res_floater["n_low_dropped"] == 1
    assert abs(res_floater["cut_volume_m3"] - res_clean["cut_volume_m3"]) < 0.5


# ---------- T0.6: request bodies reject malformed input instead of crashing ----------

def test_measure_request_rejects_bad_polygon():
    from server.schemas import MeasureRequest

    with pytest.raises(Exception):
        MeasureRequest(polygon="not-a-polygon")

    ok = MeasureRequest(polygon=[[0, 0], [1, 0], [1, 1]], rim_inner_px=4.0)
    assert ok.rim_inner_px == 4.0
    assert ok.mode == "photo"

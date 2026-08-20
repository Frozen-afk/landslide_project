"""Orthophoto rendering and ground-coordinate region selection."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

from landslide.ortho import render_orthophoto, select_region_ortho

UP = np.array([0.0, 0.0, 1.0])


def make_ctx(n=150000, seed=1):
    """A sloped, bumpy terrain patch (0..20 m) with per-height colors."""
    rng = np.random.default_rng(seed)
    pts = rng.uniform(0, 20, size=(n, 3))
    pts[:, 2] = 1.0 + 0.05 * pts[:, 0] + 0.5 * np.sin(pts[:, 0] * 0.3) \
        + rng.normal(0, 0.02, n)
    cols = np.clip(80 + pts[:, 2:3] * 60 + rng.normal(0, 5, (n, 3)), 0, 255)
    return SimpleNamespace(
        cloud=lambda dense=True: (pts, cols), scale=1.0, views={}, sparse=None)


def test_render_orthophoto_mapping(tmp_path):
    ctx = make_ctx()
    jpg = tmp_path / "ortho.jpg"
    img, meta = render_orthophoto(ctx, up=UP, max_side=200, jpg_path=jpg,
                                  log=lambda *_: None)
    assert jpg.exists() and img.shape[0] > 0
    assert abs(img.shape[1] - meta["width"]) < 2
    pts, _ = ctx.cloud()
    # a point's ground coords must land at the pixel the meta predicts
    e1, e2 = np.array(meta["e1"]), np.array(meta["e2"])
    u, v = pts[0] @ e1, pts[0] @ e2
    col = int(round((u - meta["u0"]) / meta["res"]))
    row = int(round((v - meta["v0"]) / meta["res"]))
    assert img.shape[0] > row >= 0 and img.shape[1] > col >= 0
    # mostly-covered raster: the terrain fills its own bounds
    dark = (img[:, :, 0] == 24) & (img[:, :, 1] == 28) & (img[:, :, 2] == 34)
    assert dark.mean() < 0.2


def test_select_region_ortho_no_parallax(tmp_path):
    ctx = make_ctx()
    _, meta = render_orthophoto(ctx, up=UP, max_side=200, log=lambda *_: None)
    pts, _ = ctx.cloud()
    e1, e2 = np.array(meta["e1"]), np.array(meta["e2"])
    # ground-truth box: x in [5, 12], y in [6, 14]
    box_world = np.array([[5, 6], [12, 6], [12, 14], [5, 14]])
    inside_true = ((pts @ e1 >= 5) & (pts @ e1 <= 12) &
                   (pts @ e2 >= 6) & (pts @ e2 <= 14))

    # same box expressed in ortho PIXELS (what the user clicks)
    poly_px = np.column_stack([
        (box_world[:, 0] - meta["u0"]) / meta["res"],
        (box_world[:, 1] - meta["v0"]) / meta["res"]])
    interior, rim, info = select_region_ortho(ctx, meta, poly_px, log=lambda *_: None)
    assert interior.sum() == inside_true.sum()          # exact selection
    assert rim.sum() > 100                              # annulus has points
    assert info["rim_outer_m"] > info["rim_inner_m"] > 0

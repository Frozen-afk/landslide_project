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


def test_render_orthophoto_splats_sparse_cloud(tmp_path):
    """P1: a cloud sparser than the pixel grid (point spacing ~5x res) must
    still cover most of the raster, and the highest point must still win
    every pixel its splat block shares with a lower one."""
    n_side, spacing_m, max_side = 15, 1.0, 70   # -> res = 0.2 m/px, spacing/res = 5
    xs = np.arange(n_side) * spacing_m
    xv, yv = np.meshgrid(xs, xs)
    pts = np.column_stack([xv.ravel(), yv.ravel(), np.zeros(xv.size)])
    cols = np.full((len(pts), 3), 100, dtype=np.uint8)

    low = np.array([[0.0, 0.0, -1.0]])
    high = np.array([[0.0, 0.0, 5.0]])
    pts = np.vstack([pts, low, high])
    cols = np.vstack([cols, [[10, 10, 10]], [[250, 250, 250]]]).astype(np.uint8)

    ctx = SimpleNamespace(
        cloud=lambda dense=True: (pts, cols), scale=1.0, views={}, sparse=None)
    img, meta = render_orthophoto(ctx, up=UP, max_side=max_side, log=lambda *_: None)

    painted = ~((img[:, :, 0] == 24) & (img[:, :, 1] == 28) & (img[:, :, 2] == 34))
    assert painted.mean() >= 0.8

    col = int(round((0.0 - meta["u0"]) / meta["res"]))
    row = int(round((0.0 - meta["v0"]) / meta["res"]))
    assert tuple(int(x) for x in img[row, col]) == (250, 250, 250)


def _jittered_lattice(n_side, spacing_m, jitter_frac, seed):
    rng = np.random.default_rng(seed)
    xs = np.arange(n_side) * spacing_m
    xv, yv = np.meshgrid(xs, xs)
    pts = np.column_stack([xv.ravel(), yv.ravel(), np.zeros(xv.size)])
    pts[:, :2] += rng.uniform(-jitter_frac * spacing_m, jitter_frac * spacing_m,
                              size=(len(pts), 2))
    cols = np.full((len(pts), 3), 100, dtype=np.uint8)
    return pts, cols


def test_render_orthophoto_jittered_cloud_no_speckle(tmp_path):
    """P1 correction: a jittered lattice at spacing ~1.5x res (k=2 under the
    old even-k formula) must not leave interior speckle — the block radius
    must be one full spacing, centred on the point."""
    n_side, spacing_m = 20, 1.0   # spacing/res ~1.5 -> old formula: k=2, off-centre
    span = (n_side - 1) * spacing_m
    res_target = spacing_m / 1.5
    max_side = int(round(span / res_target))
    pts, cols = _jittered_lattice(n_side, spacing_m, jitter_frac=0.15, seed=2)

    ctx = SimpleNamespace(
        cloud=lambda dense=True: (pts, cols), scale=1.0, views={}, sparse=None)
    img, meta = render_orthophoto(ctx, up=UP, max_side=max_side, log=lambda *_: None)

    bg = (img[:, :, 0] == 24) & (img[:, :, 1] == 28) & (img[:, :, 2] == 34)
    # interior only: margin of one spacing in pixels to avoid the true border
    margin = int(round(spacing_m / meta["res"]))
    interior_bg = bg[margin:-margin, margin:-margin]
    assert interior_bg.mean() < 0.01


def test_render_orthophoto_keeps_genuine_gap(tmp_path):
    """P1 correction: widening the splat block must not paint over a real,
    multi-spacing hole in the cloud — this fails if the cap is raised or
    hole filling is added later."""
    n_side, spacing_m = 20, 1.0
    span = (n_side - 1) * spacing_m
    res_target = spacing_m / 1.5
    max_side = int(round(span / res_target))
    pts, cols = _jittered_lattice(n_side, spacing_m, jitter_frac=0.15, seed=3)

    center = np.array([span / 2, span / 2])
    hole_radius_m = 20 * spacing_m if 20 * spacing_m < span / 2 - 2 else span / 2 - 2
    hole_radius_m = max(hole_radius_m, 4 * spacing_m)
    d = np.hypot(pts[:, 0] - center[0], pts[:, 1] - center[1])
    keep = d > hole_radius_m
    pts, cols = pts[keep], cols[keep]

    ctx = SimpleNamespace(
        cloud=lambda dense=True: (pts, cols), scale=1.0, views={}, sparse=None)
    img, meta = render_orthophoto(ctx, up=UP, max_side=max_side, log=lambda *_: None)

    bg = (img[:, :, 0] == 24) & (img[:, :, 1] == 28) & (img[:, :, 2] == 34)
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    u = meta["u0"] + xx * meta["res"]
    v = meta["v0"] + yy * meta["res"]
    dist_px = np.hypot(u - center[0], v - center[1]) / meta["res"]
    # more than 4 px inside the disc edge -> still background
    deep_hole = dist_px < (hole_radius_m / meta["res"] - 4)
    assert deep_hole.sum() > 0
    assert bg[deep_hole].all()

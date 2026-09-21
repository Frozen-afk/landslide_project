"""Memory-bounding behaviour of the densify stage (no SfM needed)."""
import numpy as np

from landslide.densify import (StereoConfig, _cap_voxel, _fuse_depth_candidates,
                               _pair_geometry_ok, surface_filter, voxel_downsample)
from landslide.sfm import ImageView


def _scene(n_ground=4000, n_wall=400, seed=0):
    rng = np.random.default_rng(seed)
    ground = np.column_stack([rng.uniform(0, 10, n_ground),
                              rng.uniform(0, 10, n_ground),
                              rng.normal(0, 0.01, n_ground)])
    # a thin vertical wall (like a marker board): x fixed, z spread
    wall = np.column_stack([np.full(n_wall, 5.0),
                            rng.uniform(4, 6, n_wall),
                            rng.uniform(0, 2, n_wall)])
    return ground, wall


def test_surface_filter_chunking_matches_single_shot():
    ground, wall = _scene()
    pts = np.vstack([ground, wall])
    up = np.array([0.0, 0.0, 1.0])
    one_shot = surface_filter(pts, up, chunk=10 ** 9)
    chunked = surface_filter(pts, up, chunk=997)       # odd chunk, many batches
    assert (one_shot == chunked).all()
    # ground survives, most of the wall is dropped
    assert one_shot[: len(ground)].mean() > 0.95
    assert one_shot[len(ground):].mean() < 0.2


def test_cap_voxel_scales_and_respects_bound():
    # a surface-like cloud: count scales with voxel^-2
    assert _cap_voxel(100_000, 0.01) == 0.01                     # under cap
    v = _cap_voxel(10_000_000, 0.01, max_points=2_500_000)
    assert v == np.float64(0.01 * np.sqrt(4.0))
    pts = np.random.default_rng(1).uniform(0, 100, (10_000_000 // 40, 3))
    pts[:, 2] = 0.0                                               # 2-D manifold
    cols = np.full((len(pts), 3), 128, np.uint8)
    p, _ = voxel_downsample(pts, cols, v)
    assert len(p) <= 1.3 * 2_500_000   # ~cap (density is not perfectly uniform)


def test_voxel_downsample_uniform_grid():
    rng = np.random.default_rng(2)
    pts = rng.uniform(0, 1, (50_000, 3))
    cols = rng.integers(0, 255, (50_000, 3), dtype=np.uint8)
    p, c = voxel_downsample(pts, cols, 0.1)
    assert len(p) <= 1000 and len(p) == len(c)
    # one representative per occupied cell, positions inside the hull
    assert p.dtype == np.float64 and c.dtype == np.uint8


# ---------- T1.3: pair-selection geometry gate ----------

K = np.array([[800.0, 0, 320], [0, 800.0, 240], [0, 0, 1]])
DIST = np.zeros(5)


def _view_looking_at(name, iid, center, look_at):
    f = np.asarray(look_at, np.float64) - np.asarray(center, np.float64)
    f /= np.linalg.norm(f)
    ref = np.array([0.0, 0.0, 1.0]) if abs(f[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    right = np.cross(f, ref)
    right /= np.linalg.norm(right)
    down = np.cross(f, right)
    R = np.vstack([right, down, f])
    t = -R @ np.asarray(center, np.float64)
    return ImageView(name=name, image_id=iid, camera_id=1, R=R, t=t, K=K,
                     dist=DIST, width=640, height=480, path=None)


def test_pair_geometry_gate_rejects_near_duplicate_and_too_wide_baseline():
    scene = np.array([0.0, 0.0, 0.0])
    med = 8.0
    cfg = StereoConfig()
    good_a = _view_looking_at("a", 1, (8.0, -1.0, 0.0), scene)
    good_b = _view_looking_at("b", 2, (8.0, 1.0, 0.0), scene)     # ~14° convergence
    dup = _view_looking_at("dup", 3, (8.0, -0.98, 0.0), scene)    # ~same spot as a
    wide = _view_looking_at("wide", 4, (-8.0, 0.0, 0.0), scene)   # opposite side

    base_ab = float(np.linalg.norm(good_a.center - good_b.center))
    assert _pair_geometry_ok(good_a, good_b, base_ab, med, scene, cfg, True)

    base_dup = float(np.linalg.norm(good_a.center - dup.center))
    assert not _pair_geometry_ok(good_a, dup, base_dup, med, scene, cfg, True)

    base_wide = float(np.linalg.norm(good_a.center - wide.center))
    assert not _pair_geometry_ok(good_a, wide, base_wide, med, scene, cfg, True)
    # relaxed (baseline/depth-ratio only) gate is more permissive: the
    # near-duplicate pair fails on baseline alone regardless, but flipping
    # off the angle checks must not make it MORE restrictive
    assert _pair_geometry_ok(good_a, good_b, base_ab, med, scene, cfg, False)


# ---------- T1.4: multi-view depth-consensus fusion ----------

def test_fuse_depth_candidates_picks_largest_agreeing_cluster():
    # 3 candidates at one pixel: two agree near z=10, one outlier at z=14
    Z = np.array([[[10.0]], [[10.05]], [[14.0]]])
    STEP = np.full_like(Z, 0.01)
    fused, which, count = _fuse_depth_candidates(Z, STEP)
    assert count[0, 0] == 2
    assert which[0, 0] in (0, 1)
    assert abs(fused[0, 0] - 10.025) < 1e-9


def test_fuse_depth_candidates_all_nan_gives_zero_count():
    Z = np.full((3, 2, 2), np.nan)
    STEP = np.full_like(Z, 0.01)
    _, _, count = _fuse_depth_candidates(Z, STEP)
    assert (count == 0).all()


def test_fuse_depth_candidates_single_candidate_self_agrees():
    Z = np.array([[[5.0]]])
    STEP = np.array([[[0.01]]])
    fused, which, count = _fuse_depth_candidates(Z, STEP)
    assert count[0, 0] == 1
    assert which[0, 0] == 0
    assert fused[0, 0] == 5.0

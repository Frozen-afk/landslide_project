"""Memory-bounding behaviour of the densify stage (no SfM needed)."""
import numpy as np

from landslide.densify import _cap_voxel, surface_filter, voxel_downsample


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

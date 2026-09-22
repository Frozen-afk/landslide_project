"""Ground-frame ray-casting geometry (T1.2), no SfM needed."""
import numpy as np

from landslide.ground import build_dsm, cast_polygon_to_ground, estimate_cell_size
from landslide.ortho import ground_basis
from landslide.sfm import ImageView

K = np.array([[800.0, 0, 320], [0, 800.0, 240], [0, 0, 1]])
DIST = np.zeros(5)


def _oblique_view(center, look_at):
    """A camera at `center` looking toward `look_at`, world-up-ish."""
    f = np.asarray(look_at, np.float64) - np.asarray(center, np.float64)
    f /= np.linalg.norm(f)
    world_up_ref = np.array([0.0, 0.0, 1.0])
    right = np.cross(f, world_up_ref)
    right /= np.linalg.norm(right)
    down = np.cross(f, right)
    R = np.vstack([right, down, f])
    t = -R @ np.asarray(center, np.float64)
    return ImageView(name="v", image_id=1, camera_id=1, R=R, t=t, K=K,
                     dist=DIST, width=640, height=480, path=None)


def _flat_ground_cloud(n=20000, half=5.0, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.uniform(-half, half, n)
    y = rng.uniform(-half, half, n)
    return np.column_stack([x, y, np.zeros(n)])


def test_cast_polygon_recovers_known_ground_square():
    view = _oblique_view(center=(0.0, -8.0, 4.0), look_at=(0.0, 0.0, 0.0))
    corners_world = np.array([[-1.0, -1.0, 0.0], [1.0, -1.0, 0.0],
                              [1.0, 1.0, 0.0], [-1.0, 1.0, 0.0]])
    uv, depth = view.project(corners_world)
    assert (depth > 0).all()

    pts = _flat_ground_cloud()
    up = np.array([0.0, 0.0, 1.0])
    e1, e2 = ground_basis(up)
    cell = estimate_cell_size(pts, e1, e2)
    dsm = build_dsm(pts, up, e1, e2, cell)

    ground_poly, hit_frac, max_miss_run = cast_polygon_to_ground(
        view, uv, dsm, up, e1, e2, scale=1.0)
    assert hit_frac > 0.8
    assert max_miss_run <= 2
    # every original corner is a densified-polygon vertex (k=0 for each edge)
    # and should ray-cast back to (x, y, 0) almost exactly on a flat ground
    for i, corner in enumerate(corners_world):
        # find the closest recovered ground point to this corner
        d = np.linalg.norm(ground_poly - corner[:2], axis=1)
        assert d.min() < 1e-6


def test_cast_polygon_reports_low_hit_fraction_off_footprint():
    """A polygon pointing well outside the DSM footprint should mostly miss."""
    view = _oblique_view(center=(0.0, -8.0, 4.0), look_at=(0.0, 0.0, 0.0))
    far_corners = np.array([[-1.0, -1.0, 0.0], [1.0, -1.0, 0.0],
                            [1.0, 1.0, 0.0], [-1.0, 1.0, 0.0]]) + [50.0, 0.0, 0.0]
    uv, depth = view.project(far_corners)
    assert (depth > 0).all()

    pts = _flat_ground_cloud(half=5.0)     # DSM only covers [-5, 5]^2
    up = np.array([0.0, 0.0, 1.0])
    e1, e2 = ground_basis(up)
    cell = estimate_cell_size(pts, e1, e2)
    dsm = build_dsm(pts, up, e1, e2, cell)

    _, hit_frac, _ = cast_polygon_to_ground(view, uv, dsm, up, e1, e2, scale=1.0)
    assert hit_frac < 0.3


def test_cast_polygon_respects_scale():
    """Doubling `scale` should recover a ground polygon scaled by the same factor."""
    view = _oblique_view(center=(0.0, -8.0, 4.0), look_at=(0.0, 0.0, 0.0))
    corners_world = np.array([[-1.0, -1.0, 0.0], [1.0, -1.0, 0.0],
                              [1.0, 1.0, 0.0], [-1.0, 1.0, 0.0]])
    uv, _ = view.project(corners_world)

    up = np.array([0.0, 0.0, 1.0])
    e1, e2 = ground_basis(up)

    scale = 2.0
    # match the DSM point density of the other tests despite the scaled-up
    # footprint, so this isolates the scale handling, not DSM sparsity
    pts_metric = _flat_ground_cloud(n=80_000, half=5.0) * scale
    cell = estimate_cell_size(pts_metric, e1, e2)
    dsm = build_dsm(pts_metric, up, e1, e2, cell)
    ground_poly, hit_frac, _ = cast_polygon_to_ground(view, uv, dsm, up, e1, e2, scale=scale)
    assert hit_frac > 0.8
    np.testing.assert_allclose(ground_poly[0], corners_world[0, :2] * scale, atol=1e-6)

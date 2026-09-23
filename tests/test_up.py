"""Scene-based up-vector estimation (T1.1), no SfM needed."""
import numpy as np

from landslide.densify import estimate_up
from landslide.sfm import ImageView

K = np.eye(3)
DIST = np.zeros(5)


def make_view(name, center):
    R = np.eye(3)
    t = -np.asarray(center, np.float64)
    return ImageView(name=name, image_id=abs(hash(name)) % 100000, camera_id=1,
                     R=R, t=t, K=K, dist=DIST, width=100, height=100, path=None)


def _true_normal(pts: np.ndarray) -> np.ndarray:
    c = pts.mean(axis=0)
    _, _, Vt = np.linalg.svd(pts - c, full_matrices=False)
    return Vt[2] / np.linalg.norm(Vt[2])


def _angle_deg(a, b) -> float:
    return float(np.degrees(np.arccos(np.clip(abs(a @ b), -1, 1))))


def test_collinear_camera_path_uses_scene_plane():
    """A straight-line camera walk gives the camera-plane fit an arbitrary
    (degenerate) in-plane axis for its normal; the scene's own dominant
    plane must win instead."""
    rng = np.random.default_rng(0)
    # cameras walk in a straight line along x, above a plane tilted in x
    xs = np.linspace(-5, 5, 12)
    views = {f"v{i}": make_view(f"v{i}", (x, 0.0, 5.0)) for i, x in enumerate(xs)}

    gx = rng.uniform(-6, 6, 4000)
    gy = rng.uniform(-6, 6, 4000)
    gz = 0.4 * gx + rng.normal(0, 0.002, 4000)      # tilted ground plane
    sparse = np.column_stack([gx, gy, gz])
    truth = _true_normal(sparse)
    # orient truth "up" (cameras are above the plane, at z=5 vs ground z~0)
    if truth[2] < 0:
        truth = -truth

    up = estimate_up(views, sparse)
    assert _angle_deg(up, truth) < 5.0


def test_horizontal_scene_and_camera_arc_agree():
    """The ordinary case: cameras on an arc above a flat horizontal scene —
    both candidates should recover vertical."""
    rng = np.random.default_rng(1)
    ang = np.linspace(0, np.pi, 15)
    views = {f"v{i}": make_view(f"v{i}", (8 * np.cos(a), 8 * np.sin(a), 5.0))
             for i, a in enumerate(ang)}
    gx = rng.uniform(-6, 6, 4000)
    gy = rng.uniform(-6, 6, 4000)
    gz = rng.normal(0, 0.002, 4000)
    sparse = np.column_stack([gx, gy, gz])

    up = estimate_up(views, sparse)
    assert _angle_deg(up, np.array([0.0, 0.0, 1.0])) < 5.0


def test_info_reports_source_and_disagree_angle_on_genuine_disagreement():
    """P4: a tilted ground plane under an arc of cameras disagrees with the
    camera-plane normal by a known, constructed angle — `info["disagree_deg"]`
    must match it and `info["up_source"]` must name the branch actually taken
    (not just any string)."""
    rng = np.random.default_rng(3)
    tilt_deg = 25.0
    tilt = np.radians(tilt_deg)
    # arc of cameras roughly above the scene, tall enough that the
    # camera-plane normal stays close to true vertical, not the tilted one
    ang = np.linspace(0, np.pi, 15)
    views = {f"v{i}": make_view(f"v{i}", (8 * np.cos(a), 8 * np.sin(a), 12.0))
             for i, a in enumerate(ang)}

    gx = rng.uniform(-6, 6, 4000)
    gy = rng.uniform(-6, 6, 4000)
    gz = np.tan(tilt) * gx + rng.normal(0, 0.002, 4000)
    sparse = np.column_stack([gx, gy, gz])

    info: dict = {}
    up = estimate_up(views, sparse, info=info)

    assert info["up_source"] in ("scene_plane", "camera_plane")
    assert info["disagree_deg"] is not None
    assert abs(info["disagree_deg"] - tilt_deg) < 2.0
    # up_source must name the branch that actually produced `up`
    truth_scene = _true_normal(sparse)
    if truth_scene[2] < 0:
        truth_scene = -truth_scene
    chosen_truth = truth_scene if info["up_source"] == "scene_plane" \
        else np.array([0.0, 0.0, 1.0])
    assert _angle_deg(up, chosen_truth) < 5.0


def test_returns_camera_plane_when_scene_has_no_dominant_plane():
    """A scene with no clean dominant plane (fit_plane_ransac degenerate)
    must fall back to the camera-path estimate rather than erroring."""
    rng = np.random.default_rng(2)
    ang = np.linspace(0, np.pi, 15)
    views = {f"v{i}": make_view(f"v{i}", (8 * np.cos(a), 8 * np.sin(a), 5.0))
             for i, a in enumerate(ang)}
    sparse = rng.uniform(-6, 6, (100, 3))   # a noisy blob, no surface at all

    up = estimate_up(views, sparse)
    assert np.isfinite(up).all()
    assert abs(np.linalg.norm(up) - 1.0) < 1e-9

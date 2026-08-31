"""Prior-DEM differencing and two-epoch change monitoring."""
import numpy as np

from landslide.dem import DemSurface, _gravity_R, icp_rigid, load_dem
from landslide.volume import dem_volume


def _road_with_pile(r_half=2.0, pile_r=0.9, pile_h=0.4, step=0.15,
                    seed=3, rotate=False):
    """Road with a smooth hill (relief makes rotation observable) + cone pile;
    optionally the whole scene is rigidly moved."""
    rng = np.random.default_rng(seed)
    xs = np.arange(-r_half, r_half + step, step)
    X, Y = np.meshgrid(xs, xs)
    r = np.hypot(X, Y)
    z = 0.35 * np.exp(-(((X - 1.0) ** 2) + ((Y + 1.0) ** 2)) / 0.8)  # hill
    z += pile_h * np.clip(1 - r / max(pile_r, 1e-6), 0, 1)
    z += 0.01 * rng.standard_normal(z.shape)
    pts = np.column_stack([X.ravel(), Y.ravel(), z.ravel()])
    if rotate:
        R = _rotation(np.radians(25), np.radians(-10))
        pts = pts @ R.T + np.array([40.0, 120.0, 350.0])
    truth = np.pi * pile_r ** 2 * pile_h / 3.0
    return pts, truth


def _rotation(az, tilt):
    cz, sz = np.cos(az), np.sin(az)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1.0]])
    ct, st = np.cos(tilt), np.sin(tilt)
    Rx = np.array([[1, 0, 0], [0, ct, -st], [0, st, ct]])
    return Rz @ Rx


def test_dem_volume_recovers_pile():
    pts, truth = _road_with_pile()
    surf = DemSurface(pts + np.array([0, 0, 0]))      # DEM = road itself
    # remove the pile from the DEM: use points with the pile subtracted
    dem_pts = pts.copy()
    r = np.hypot(dem_pts[:, 0], dem_pts[:, 1])
    inside = r <= 0.9
    dem_pts[inside, 2] -= 0.4 * np.clip(1 - r[inside] / 0.9, 0, 1)
    surf = DemSurface(dem_pts)
    res = dem_volume(pts, surf, log=lambda *_: None)
    assert res["datum"] == "dem"
    rel = abs(res["fill_volume_m3"] - truth) / truth
    assert rel < 0.15, f"fill {res['fill_volume_m3']:.3f} vs {truth:.3f}"
    assert res["cut_volume_m3"] < 0.15 * truth


def test_dem_volume_aligned_after_rigid_move():
    # same scene expressed in a rotated/translated world frame: the surface
    # and the DEM move together, differencing must be invariant
    pts, truth = _road_with_pile(rotate=True)
    dem_pts, _ = _road_with_pile(rotate=True, seed=3)
    r = np.hypot(dem_pts[:, 0] - 40.0, dem_pts[:, 1] - 120.0)  # undo offset
    dem_local = (dem_pts - np.array([40.0, 120.0, 350.0])) @ _rotation(
        np.radians(25), np.radians(-10))
    rr = np.hypot(dem_local[:, 0], dem_local[:, 1])
    m = rr <= 0.9
    dem_local[m, 2] -= 0.4 * np.clip(1 - rr[m] / 0.9, 0, 1)
    dem_world = dem_local @ _rotation(np.radians(25), np.radians(-10)).T \
        + np.array([40.0, 120.0, 350.0])
    surf = DemSurface(dem_world)
    res = dem_volume(pts, surf, log=lambda *_: None)
    rel = abs(res["fill_volume_m3"] - truth) / truth
    assert rel < 0.20, f"fill {res['fill_volume_m3']:.3f} vs {truth:.3f}"


def test_load_dem_xyz_text(tmp_path):
    pts, _ = _road_with_pile(r_half=1.5, step=0.2, seed=5)
    p = tmp_path / "dem.xyz"
    with open(p, "w") as f:
        f.write("# x y z\n")
        for x, y, z in pts:
            f.write(f"{x:.4f} {y:.4f} {z:.4f}\n")
    loaded = load_dem(p, log=lambda *_: None)
    assert len(loaded["pts"]) == len(pts)
    # rejects too-sparse input
    p2 = tmp_path / "tiny.xyz"
    p2.write_text("0 0 0\n1 1 1\n2 2 0\n")
    try:
        load_dem(p2, log=lambda *_: None)
        assert False, "sparse DEM accepted"
    except RuntimeError:
        pass


def test_icp_rigid_rejects_contamination():
    rng = np.random.default_rng(0)
    base, _ = _road_with_pile(r_half=2.0, pile_r=0.9, pile_h=0.0, step=0.2)
    from scipy.spatial import cKDTree
    tree = cKDTree(base)
    # "new" cloud: road + 30% junk points floating 1 m up (the debris)
    moved = base + np.array([0.3, -0.2, 0.05])
    junk = rng.uniform([-2, -2, 1.0], [2, 2, 1.4], (int(0.3 * len(base)), 3))
    src = np.vstack([moved, junk])
    R, t, rms = icp_rigid(src, tree, log=lambda *_: None)
    aligned = src @ R.T + t
    d = np.linalg.norm(aligned[:len(base)] - base, axis=1)
    assert d.mean() < 0.05, f"ground misaligned by {d.mean():.3f} m"


def test_change_volume_between_epochs():
    """Road, then the same road with a pile added: net change = pile."""
    from landslide.change import change_volume

    road_a, _ = _road_with_pile(r_half=2.0, pile_r=0.01, pile_h=0.0,
                                step=0.2, seed=1)
    road_b, truth = _road_with_pile(r_half=2.0, pile_r=0.9, pile_h=0.4,
                                    step=0.2, seed=2)

    class FakeCtx:
        scale = 1.0
        scale_info = {"applied": True, "scale": 1.0}

        def cloud(self, dense=True):
            return (self._pts, None)

    a, b = FakeCtx(), FakeCtx()
    a._pts, b._pts = road_a, road_b
    res = change_volume(a, b, log=lambda *_: None, up=np.array([0, 0, 1.0]))
    rel = abs(res["fill_volume_m3"] - truth) / truth
    assert rel < 0.25, f"change fill {res['fill_volume_m3']:.3f} vs {truth:.3f}"
    assert res["datum"] == "prior_epoch"
    assert res["icp_rms_m"] < 0.05


def test_change_marker_anchored_registration():
    """Two epochs of a planar road (ICP would slide) sharing a physical
    marker: the marker corners must give the exact transform instead."""
    from landslide.change import change_volume
    from tests.test_dem import _rotation  # noqa: F401 (self-import ok)

    road_a, _ = _road_with_pile(r_half=2.0, pile_r=0.01, pile_h=0.0,
                                step=0.25, seed=1)
    # epoch B: same physical road, different arbitrary model frame; a pile
    # sits where none was
    R_mov = _rotation(np.radians(140), np.radians(35))
    off = np.array([17.0, -42.0, 9.0])
    road_b_local, truth = _road_with_pile(r_half=2.0, pile_r=0.9, pile_h=0.4,
                                          step=0.25, seed=2)
    road_b = road_b_local @ R_mov.T + off

    # the physical marker (0.25 m square at the origin area) seen metric in
    # each model's own frame
    corner_l = np.array([[0.0, 0.0, 0.0], [0.25, 0.0, 0.0],
                         [0.25, 0.25, 0.0], [0.0, 0.25, 0.0]])
    corners_a = corner_l
    corners_b = corner_l @ R_mov.T + off

    class FakeCtx:
        scale = 1.0

        def cloud(self, dense=True):
            return (self._pts, None)

    a, b = FakeCtx(), FakeCtx()
    a._pts, b._pts = road_a, road_b
    a.scale_info = {"applied": True, "marker_corners_m": corners_a.tolist()}
    b.scale_info = {"applied": True, "marker_corners_m": corners_b.tolist()}

    lines = []
    res = change_volume(a, b, log=lines.append)   # no up given, no ICP needed
    assert res["registration"] == "marker", lines
    rel = abs(res["fill_volume_m3"] - truth) / truth
    assert rel < 0.25, f"change fill {res['fill_volume_m3']:.3f} vs {truth:.3f}"
    assert res["icp_rms_m"] < 0.02   # corner residual, millimetres
    # and the marker path wins even though the scene is a pure plane where
    # ICP translation is unobservable
    assert any("marker-anchored" in l for l in lines)

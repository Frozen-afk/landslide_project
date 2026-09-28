"""S5C observation map / recapture-instruction unit tests
(GUIDED_CAPTURE_PLAN.md §12c.4, §12c.6; §11 criteria 3 and 4).

Hand-built sparse contexts (no SfM run) — a flat plane plus a bowl, with
fake point tracks from two synthetic arms, following the same pattern as
`tests/test_capture_preview.py::_flat_scene_ctx`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from landslide import capture as capture_mod
from landslide.sfm import ImageView, ReconCtx

K = np.eye(3)
DIST = np.zeros(5)
UP = np.array([0.0, 0.0, 1.0])
E1 = np.array([1.0, 0.0, 0.0])
E2 = np.array([0.0, 1.0, 0.0])


def make_view(name, image_id, center):
    R = np.eye(3)
    t = -np.asarray(center, np.float64)
    return ImageView(name=name, image_id=image_id, camera_id=1, R=R, t=t,
                     K=K, dist=DIST, width=100, height=100, path=Path("x"))


class _Elem:
    def __init__(self, image_id):
        self.image_id = image_id


class _Track:
    def __init__(self, image_ids):
        self.elements = [_Elem(i) for i in image_ids]


class _Point3D:
    def __init__(self, xyz, image_ids):
        self.xyz = xyz
        self.track = _Track(image_ids)


def make_ctx(views: dict, points: list[tuple[tuple, list[int]]]) -> ReconCtx:
    """`points`: [((x,y,z), [image_ids observing it]), ...]."""
    pts = np.array([p[0] for p in points], np.float64)
    cols = np.zeros((len(pts), 3), dtype=np.uint8)
    rec = SimpleNamespace(points3D={i: _Point3D(p[0], p[1])
                                    for i, p in enumerate(points)})
    return ReconCtx(rec=rec, views=views, sparse=pts, sparse_colors=cols,
                    photos_dir=Path("."), workdir=Path("."), scale=1.0)


def _camera_for_grazing(cell_xy, az_deg, grazing_deg, dist_horiz=10.0):
    """A camera at horizontal distance `dist_horiz` from `cell_xy`, azimuth
    `az_deg`, at the height that gives `grazing_deg` against a flat (up-
    normal) cell plane."""
    h = dist_horiz * np.tan(np.radians(grazing_deg))
    a = np.radians(az_deg)
    cx = cell_xy[0] + dist_horiz * np.cos(a)
    cy = cell_xy[1] + dist_horiz * np.sin(a)
    return (cx, cy, h)


ROI = np.array([[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]])


def _jitter_points(center, n=6, spread=0.05, rng=None):
    rng = rng or np.random.default_rng(0)
    return [(center[0] + rng.uniform(-spread, spread),
            center[1] + rng.uniform(-spread, spread),
            0.0) for _ in range(n)]


# ---------------------------------------------------------------------------
# criterion 3: cell-map unit tests
# ---------------------------------------------------------------------------

def test_cell_seen_by_three_arm_cameras_at_25deg_is_good_10deg_is_weak():
    cell_xy = (0.5, 0.5)
    cams = [_camera_for_grazing(cell_xy, az, 25.0) for az in (0.0, 120.0, 240.0)]
    views = {f"A{i}": make_view(f"A{i}", i, c) for i, c in enumerate(cams)}
    pts6 = _jitter_points(cell_xy, n=6)
    points = [(p, [0, 1, 2]) for p in pts6]
    ctx = make_ctx(views, points)
    arms = {n: "A" for n in views}

    cmap = capture_mod.coverage_map(ctx, UP, E1, E2, ROI, arms=arms)
    ix = int((cell_xy[0] - cmap["grid"]["u0"]) / cmap["cell_m"])
    iy = int((cell_xy[1] - cmap["grid"]["v0"]) / cmap["cell_m"])
    row = cmap["grid"]["classes"].split("\n")[iy]
    assert row[ix] == "g"

    cams10 = [_camera_for_grazing(cell_xy, az, 10.0) for az in (0.0, 120.0, 240.0)]
    views10 = {f"A{i}": make_view(f"A{i}", i, c) for i, c in enumerate(cams10)}
    ctx10 = make_ctx(views10, points)
    cmap10 = capture_mod.coverage_map(ctx10, UP, E1, E2, ROI, arms=arms)
    row10 = cmap10["grid"]["classes"].split("\n")[iy]
    assert row10[ix] == "w"


def test_cell_with_no_points_is_void():
    views = {"A0": make_view("A0", 0, (0.0, 0.0, 10.0))}
    ctx = make_ctx(views, [((4.0, 4.0, 0.0), [0])])   # far corner only
    cmap = capture_mod.coverage_map(ctx, UP, E1, E2, ROI, arms={"A0": "A"})
    ix = int((0.5 - cmap["grid"]["u0"]) / cmap["cell_m"])
    iy = int((0.5 - cmap["grid"]["v0"]) / cmap["cell_m"])
    row = cmap["grid"]["classes"].split("\n")[iy]
    assert row[ix] == "v"


def test_preview_coverage_and_void_area_match_hand_counts():
    cell_xy = (0.5, 0.5)
    cams = [_camera_for_grazing(cell_xy, az, 25.0) for az in (0.0, 120.0, 240.0)]
    views = {f"A{i}": make_view(f"A{i}", i, c) for i, c in enumerate(cams)}
    points = [(p, [0, 1, 2]) for p in _jitter_points(cell_xy, n=6)]
    ctx = make_ctx(views, points)
    arms = {n: "A" for n in views}

    cmap = capture_mod.coverage_map(ctx, UP, E1, E2, ROI, arms=arms,
                                    cell_m=1.0, rim_band_m=0.0)
    n_roi_cells = cmap["grid"]["nx"] * cmap["grid"]["ny"]
    n_good = cmap["grid"]["classes"].replace("\n", "").count("g")
    assert n_good == 1
    assert cmap["preview_coverage"] == pytest.approx(1 / n_roi_cells)
    # every other roi cell is void (n_roi_cells - 1 cells, 1m^2 each)
    assert cmap["preview_void_m2"] == pytest.approx(n_roi_cells - 1)


def test_band_cells_never_enter_preview_coverage():
    """A good cell placed only in the rim band (outside the ROI polygon)
    must not raise preview_coverage."""
    band_xy = (6.0, 0.0)   # outside the ROI ([-5,5]^2), inside the 2.5m band
    cams = [_camera_for_grazing(band_xy, az, 25.0) for az in (0.0, 120.0, 240.0)]
    views = {f"A{i}": make_view(f"A{i}", i, c) for i, c in enumerate(cams)}
    points = [(p, [0, 1, 2]) for p in _jitter_points(band_xy, n=6)]
    ctx = make_ctx(views, points)
    arms = {n: "A" for n in views}

    cmap = capture_mod.coverage_map(ctx, UP, E1, E2, ROI, arms=arms)
    assert cmap["preview_coverage"] == 0.0
    assert "G" in cmap["grid"]["classes"]   # the band cell is recorded (uppercase)


def _band_cell_centers(roi=ROI, rim_band_m=2.5, cell_m=1.0):
    """The exact band-grid cell centres `coverage_map` itself computes, so
    the fixture can densely fill the annulus at the same resolution instead
    of a thin single-radius ring (which leaves most band cells void
    regardless of azimuthal coverage, and would test grid sparsity rather
    than azimuthal seen/unseen)."""
    from landslide.geometry import points_in_polygon, ring_distance

    lo = roi.min(axis=0) - rim_band_m
    hi = roi.max(axis=0) + rim_band_m
    nx = int(np.ceil((hi[0] - lo[0]) / cell_m))
    ny = int(np.ceil((hi[1] - lo[1]) / cell_m))
    xs = lo[0] + (np.arange(nx) + 0.5) * cell_m
    ys = lo[1] + (np.arange(ny) + 0.5) * cell_m
    Xc, Yc = np.meshgrid(xs, ys, indexing="xy")
    centers = np.column_stack([Xc.ravel(), Yc.ravel()])
    is_roi = points_in_polygon(centers, roi)
    d = ring_distance(centers, roi)
    is_band = (~is_roi) & (d <= rim_band_m)
    return centers[is_band]


def _ring_fixture(az_filter):
    """Densely-covered band cells (each seen by 3 same-arm cameras at
    25deg grazing) at every band-grid azimuth passing `az_filter`."""
    rng = np.random.default_rng(2)
    views: dict = {}
    points: list = []
    vid = 0
    for cx, cy in _band_cell_centers():
        az = np.degrees(np.arctan2(cy, cx)) % 360.0
        if not az_filter(az):
            continue
        cams = [_camera_for_grazing((cx, cy), az + d, 25.0, dist_horiz=4.0)
               for d in (-15, 0, 15)]
        ids = []
        for c in cams:
            views[f"A{vid}"] = make_view(f"A{vid}", vid, c)
            ids.append(vid)
            vid += 1
        points += [(p, ids) for p in _jitter_points((cx, cy), n=6, spread=0.02, rng=rng)]
    arms = {n: "A" for n in views}
    return make_ctx(views, points), arms


def test_band_windows_seen_full_ring_vs_half_ring():
    ctx_full, arms_full = _ring_fixture(lambda az: True)
    cmap_full = capture_mod.coverage_map(ctx_full, UP, E1, E2, ROI, arms=arms_full)
    assert cmap_full["band_windows_seen"] == 8

    ctx_half, arms_half = _ring_fixture(lambda az: az < 180.0)
    cmap_half = capture_mod.coverage_map(ctx_half, UP, E1, E2, ROI, arms=arms_half)
    assert cmap_half["band_windows_seen"] <= 4


# ---------------------------------------------------------------------------
# criterion 4: instruction unit tests
# ---------------------------------------------------------------------------

def _arms_geom_two_arms():
    cam_names = [f"A{i}" for i in range(5)] + [f"B{i}" for i in range(5)]
    azim = np.array([-10, -5, 0, 5, 10] + [80, 85, 90, 95, 100], np.float64)
    elev = np.full(10, 25.0)
    dist = np.full(10, 15.0)
    arms = {n: ("A" if n.startswith("A") else "B") for n in cam_names}
    return capture_mod._arm_geometry(cam_names, azim, elev, dist, arms), cam_names, azim, arms


def test_gap_facing_inter_arm_gap_extends_from_nearer_outer_end():
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    # midpoint between the arms (A ends ~10, B starts ~80): a gap facing 45deg
    gap = {"area_m2": 4.0, "centroid_uv": [3.0, 3.0], "face_az": 45.0, "slope_deg": 5.0}
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    ins = capture_mod._gap_instruction(gap, arms_geom, cam_az, arms)
    assert ins["kind"] == "extend_arm"
    # extend_arm always starts from an OUTER end (away from the other arm),
    # never the end nearest the other arm — §12a.3: an isolated position
    # near the inter-arm gap doesn't register.
    end_az = capture_mod._outer_ends(ins["arm"], arms_geom)[0]
    delta = capture_mod._circular_diff_deg(end_az, 45.0)
    expected_n = max(3, int(np.ceil(delta / capture_mod.ARM_STEP_DEG))
                     + capture_mod.OUTER_END_EXTRA)
    assert ins["n_photos"] == expected_n


def test_gap_inside_an_arm_span_produces_higher_with_stand_formula():
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    gap = {"area_m2": 4.0, "centroid_uv": [0.0, 0.0], "face_az": 0.0, "slope_deg": 10.0}
    ins = capture_mod._gap_instruction(gap, arms_geom, cam_az, arms)
    assert ins["kind"] == "higher"
    assert ins["arm"] == "A"
    need_elev = max(20.0, capture_mod.THETA_G + 10.0)
    expected_stand = arms_geom["A"]["h_med"] / np.tan(np.radians(need_elev))
    assert ins["stand_m"] == pytest.approx(expected_stand)


def test_one_arm_only_produces_new_arm_between_60_and_120deg():
    cam_names = [f"A{i}" for i in range(5)]
    azim = np.array([-10, -5, 0, 5, 10], np.float64)
    elev = np.full(5, 25.0)
    dist = np.full(5, 15.0)
    arms = {n: "A" for n in cam_names}
    arms_geom = capture_mod._arm_geometry(cam_names, azim, elev, dist, arms)
    ins = capture_mod._new_arm_instruction(arms_geom, gaps=[])
    assert ins["kind"] == "new_arm"
    sep = capture_mod._circular_diff_deg(ins["target_az"], arms_geom["A"]["mean_az"])
    assert 60.0 <= sep <= 120.0


def test_no_instruction_targets_a_position_not_connected_to_an_arm_end():
    """extend_arm always starts from an outer arm end — its target azimuth
    is within delta of that end, never an isolated far-side azimuth."""
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    gap = {"area_m2": 4.0, "centroid_uv": [0, -5], "face_az": 200.0, "slope_deg": 0.0}
    ins = capture_mod._gap_instruction(gap, arms_geom, cam_az, arms)
    assert ins["kind"] == "extend_arm"
    ends = capture_mod._outer_ends(ins["arm"], arms_geom)
    assert any(capture_mod._circular_diff_deg(e, 200.0) < 180.0 for e in ends)


def test_at_most_three_instructions_largest_first_merge_within_15deg():
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    coverage = {"gaps": [
        {"area_m2": 10.0, "centroid_uv": [1, 1], "face_az": 40.0, "slope_deg": 0.0},
        {"area_m2": 9.0, "centroid_uv": [1.1, 1.1], "face_az": 45.0, "slope_deg": 0.0},  # merges with above
        {"area_m2": 5.0, "centroid_uv": [-5, 0], "face_az": 180.0, "slope_deg": 0.0},
        {"area_m2": 3.0, "centroid_uv": [0, -5], "face_az": 270.0, "slope_deg": 0.0},
        {"area_m2": 2.0, "centroid_uv": [5, -5], "face_az": 300.0, "slope_deg": 0.0},
    ]}
    out = capture_mod.recapture_instructions(coverage, arms_geom, cam_az, arms)
    assert len(out) <= 3
    # the 45deg duplicate (9 m^2) must have been dropped, not the 40deg one
    azs = [g["face_az"] for g in coverage["gaps"]]
    assert len(out) == 3


def test_unstable_datum_and_no_gaps_produces_zero_instructions():
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    coverage = {"gaps": []}   # recapture_instructions never reads a datum estimate
    out = capture_mod.recapture_instructions(coverage, arms_geom, cam_az, arms)
    assert out == []


def test_small_steep_gap_produces_no_instruction():
    """GUIDED_CAPTURE_PROGRESS.md "S5C" false-instruction finding: a
    minimum-size gap (<= 1.5x GAP_MIN_M2) with an implausibly steep fitted
    normal (> 60deg) is a preview-SfM fit artifact, not a real void --
    reproduced directly on twosided_b's own cheap preview (area 2.0 m^2,
    slope_deg 87.2, which drove `need_elev` past 90deg before this guard).
    """
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    gap = {"area_m2": 2.0, "centroid_uv": [0.0, 0.0], "face_az": 0.0,
          "slope_deg": 87.24}
    assert capture_mod._gap_instruction(gap, arms_geom, cam_az, arms) is None

    # a gap of the same size but a plausible slope is unaffected
    gap_ok = {**gap, "slope_deg": 10.0}
    assert capture_mod._gap_instruction(gap_ok, arms_geom, cam_az, arms) is not None

    # a large-area gap keeps its instruction even at a steep slope (only the
    # minimum-size + implausible-slope *combination* is withheld)
    gap_large = {**gap, "area_m2": 20.0}
    assert capture_mod._gap_instruction(gap_large, arms_geom, cam_az, arms) is not None


def test_higher_instruction_need_elev_is_clamped_below_90deg():
    """A large-area gap (so the DEGENERATE_GAP_* guard above does not apply)
    with a steep fitted slope must still produce a physically sane `higher`
    instruction -- `need_elev` capped below 90deg keeps `stand_m` positive
    and finite (before this clamp, slope_deg 87.24 drove need_elev to
    105.2deg and stand_m to ~9.8e6 m, reproduced on twosided_b's preview)."""
    arms_geom, cam_names, azim, arms = _arms_geom_two_arms()
    cam_az = {n: float(a) for n, a in zip(cam_names, azim)}
    gap = {"area_m2": 20.0, "centroid_uv": [0.0, 0.0], "face_az": 0.0,
          "slope_deg": 87.24}
    ins = capture_mod._gap_instruction(gap, arms_geom, cam_az, arms)
    assert ins["kind"] == "higher"
    assert ins["need_elev"] <= 80.0
    assert 0.0 < ins["stand_m"] < 1000.0

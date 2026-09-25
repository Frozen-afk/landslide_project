"""T3 preview-reconstruction checks (GUIDED_CAPTURE_PLAN.md §4.3, S4).

Two layers: pure-logic tests for the small helpers `preview_report` is built
from (no reconstruction needed), and integration tests of `preview_report`
itself. The integration tests use two kinds of `ReconCtx`:

- A hand-built one (`_flat_scene_ctx`, the `tests/test_up.py` pattern): real
  `estimate_up`/ground-basis/elevation/azimuth math on a flat synthetic
  scene, with `_camera_center_collinearity`/`_focal_spread`/`select_pairs`/
  `aruco_scale` monkeypatched (they have their own dedicated tests in
  `tests/test_sfm.py` / `tests/test_scaling.py`) so each threshold in
  `preview_report` can be pinned in isolation without a real SfM run.
- The real, already-cached reconstructions under `data/bench/{twosided,
  twosided_b,twosided_cal}/work` (skipped if absent) for the §12b.6
  calibration finding: see `GUIDED_CAPTURE_PROGRESS.md`'s S4 section for why
  the datum-instability check is computed but never surfaced.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from landslide import capture as capture_mod
from landslide.sfm import ImageView, ReconCtx, reconstruct

ROOT = Path(__file__).resolve().parents[1]

K = np.eye(3)
DIST = np.zeros(5)


def make_view(name, center):
    R = np.eye(3)
    t = -np.asarray(center, np.float64)
    return ImageView(name=name, image_id=abs(hash(name)) % 100000, camera_id=1,
                     R=R, t=t, K=K, dist=DIST, width=100, height=100, path=Path("x"))


def _flat_scene_ctx(view_specs) -> ReconCtx:
    """Cameras at `view_specs` above a flat, noisy horizontal ground plane
    (matches `tests/test_up.py`'s own fixture) — `estimate_up` recovers
    +z regardless of the camera layout. `ctx.rec` is a stand-in never
    touched by real code once `_camera_center_collinearity`/`_focal_spread`/
    `select_pairs` are monkeypatched."""
    rng = np.random.default_rng(0)
    views = {name: make_view(name, center) for name, center in view_specs}
    gx = rng.uniform(-6, 6, 4000)
    gy = rng.uniform(-6, 6, 4000)
    gz = rng.normal(0, 0.002, 4000)
    sparse = np.column_stack([gx, gy, gz])
    colors = np.zeros((len(sparse), 3), dtype=np.uint8)
    return ReconCtx(rec=SimpleNamespace(), views=views, sparse=sparse,
                    sparse_colors=colors, photos_dir=Path("."), workdir=Path("."))


def _patch_geometry_checks(monkeypatch, collinearity=0.5, focal_ratio=1.0, pairs=None):
    monkeypatch.setattr(capture_mod, "_camera_center_collinearity", lambda rec: collinearity)
    monkeypatch.setattr(capture_mod, "_focal_spread", lambda rec: (1.0, 1.0, focal_ratio))
    monkeypatch.setattr(capture_mod, "select_pairs", lambda ctx, log=None: pairs or [])


def _arc_views(prefix, center_deg, span_deg, n, radius=8.0, height=5.0):
    angs = np.radians(center_deg + np.linspace(-span_deg / 2, span_deg / 2, n))
    return [(f"{prefix}{i}", (radius * np.cos(a), radius * np.sin(a), height))
           for i, a in enumerate(angs)]


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_circular_mean_deg_wraps_correctly():
    m = capture_mod._circular_mean_deg([350.0, 10.0])
    assert min(m, 360.0 - m) < 1e-6


def test_circular_diff_deg_handles_wraparound():
    assert capture_mod._circular_diff_deg(350.0, 10.0) == pytest.approx(20.0)
    assert capture_mod._circular_diff_deg(10.0, 350.0) == pytest.approx(20.0)


def test_default_roi_polygon_excludes_far_outliers():
    rng = np.random.default_rng(0)
    core = rng.normal(0, 1, (200, 2))
    far = rng.normal(0, 1, (50, 2)) + np.array([80.0, 80.0])
    uv = np.vstack([core, far])
    poly = capture_mod._default_roi_polygon(uv)
    assert poly[:, 0].max() < 10.0 and poly[:, 1].max() < 10.0


# ---------------------------------------------------------------------------
# preview_report: threshold/level wiring, on a hand-built flat scene
# ---------------------------------------------------------------------------

def test_registration_blocks_when_a_whole_arm_never_registered(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=8)
    ctx = _flat_scene_ctx(view_specs)
    arms = {name: "A" for name, _ in view_specs}
    arms.update({f"B{i}": "B" for i in range(5)})   # never in ctx.views

    report = capture_mod.preview_report(ctx, arms=arms, marker_side_m=None,
                                        log=lambda *_a: None)
    assert "T3_registration" in report["blocking"]
    assert report["missing_arms"] == ["B"]


def test_registration_ok_when_all_submitted_photos_registered(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = (_arc_views("A", center_deg=0, span_deg=40, n=8)
                 + _arc_views("B", center_deg=90, span_deg=40, n=8))
    ctx = _flat_scene_ctx(view_specs)
    arms = {name: name[0] for name, _ in view_specs}

    report = capture_mod.preview_report(ctx, arms=arms, marker_side_m=None,
                                        log=lambda *_a: None)
    assert "T3_registration" not in report["blocking"]
    assert report["missing_arms"] == []


def test_azimuth_separation_blocks_when_arms_overlap(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = (_arc_views("A", center_deg=0, span_deg=20, n=8)
                 + _arc_views("B", center_deg=15, span_deg=20, n=8))   # ~15deg apart
    ctx = _flat_scene_ctx(view_specs)
    arms = {name: name[0] for name, _ in view_specs}

    report = capture_mod.preview_report(ctx, arms=arms, marker_side_m=None,
                                        log=lambda *_a: None)
    assert "T3_azimuth" in report["blocking"]
    assert report["arm_separation_deg"] < 60.0


def test_azimuth_separation_ok_when_arms_well_separated(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = (_arc_views("A", center_deg=0, span_deg=20, n=8, height=15.0)
                 + _arc_views("B", center_deg=90, span_deg=20, n=8, height=15.0))
    ctx = _flat_scene_ctx(view_specs)
    arms = {name: name[0] for name, _ in view_specs}

    report = capture_mod.preview_report(ctx, arms=arms, marker_side_m=None,
                                        log=lambda *_a: None)
    assert "T3_azimuth" not in report["blocking"]
    assert report["arm_separation_deg"] == pytest.approx(90.0, abs=1.0)


def test_collinearity_blocks_below_threshold(monkeypatch):
    _patch_geometry_checks(monkeypatch, collinearity=0.05)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6)
    ctx = _flat_scene_ctx(view_specs)

    report = capture_mod.preview_report(ctx, marker_side_m=None, log=lambda *_a: None)
    assert "G4_collinear" in report["blocking"]


def test_focal_spread_warns_above_threshold(monkeypatch):
    _patch_geometry_checks(monkeypatch, focal_ratio=1.3)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6)
    ctx = _flat_scene_ctx(view_specs)

    report = capture_mod.preview_report(ctx, marker_side_m=None, log=lambda *_a: None)
    warns = {c["id"]: c["level"] for c in report["checks"]}
    assert warns.get("G3_focal") == "warn"
    assert "G3_focal" not in report["blocking"]


def test_stereo_pair_share_warns_when_most_images_are_unpaired(monkeypatch):
    _patch_geometry_checks(monkeypatch, pairs=[])   # 0 covered / n views
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6, height=15.0)
    ctx = _flat_scene_ctx(view_specs)

    report = capture_mod.preview_report(ctx, marker_side_m=None, log=lambda *_a: None)
    assert report["stereo_pair_share"] == 0.0
    warns = {c["id"]: c["level"] for c in report["checks"]}
    assert warns.get("G1_thin_dense") == "warn"


def test_low_elevation_warns(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6, radius=8.0, height=1.5)
    ctx = _flat_scene_ctx(view_specs)   # atan(1.5/8) ~ 10.6 deg, well under 20

    report = capture_mod.preview_report(ctx, marker_side_m=None, log=lambda *_a: None)
    assert report["elevation_deg"]["median"] < capture_mod.MIN_ELEVATION_DEG
    warns = {c["id"]: c["level"] for c in report["checks"]}
    assert warns.get("G5_partial") == "warn"


def test_marker_raise_blocks_with_a_message(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6, height=15.0)
    ctx = _flat_scene_ctx(view_specs)
    monkeypatch.setattr(capture_mod, "aruco_scale",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("marker not seen")))

    report = capture_mod.preview_report(ctx, marker_side_m=0.5, log=lambda *_a: None)
    assert "T3_marker_scale" in report["blocking"]
    assert "marker not seen" in report["marker_error"]
    assert report["datum_estimate"] is None   # never attempted without a scale


def test_marker_warnings_warn_without_blocking(monkeypatch):
    _patch_geometry_checks(monkeypatch)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=6, height=15.0)
    ctx = _flat_scene_ctx(view_specs)
    monkeypatch.setattr(capture_mod, "aruco_scale", lambda *a, **kw: {
        "scale": 1.0, "warnings": ["marker side spread 7%"], "views_used": 3})

    report = capture_mod.preview_report(ctx, marker_side_m=0.5, log=lambda *_a: None)
    warns = {c["id"]: c["level"] for c in report["checks"]}
    assert warns.get("G2_scale_warn") == "warn"
    assert "T3_marker_scale" not in report["blocking"]


def test_datum_estimate_never_becomes_a_check(monkeypatch):
    """§12b.6: the ratio is diagnostic-only in this stage (see the module
    docstring / GUIDED_CAPTURE_PROGRESS.md's S4 section) — it must never
    turn into a `checks` entry, no matter how large it is."""
    _patch_geometry_checks(monkeypatch)
    view_specs = _arc_views("A", center_deg=0, span_deg=40, n=30, radius=8.0, height=15.0)
    ctx = _flat_scene_ctx(view_specs)
    monkeypatch.setattr(capture_mod, "aruco_scale",
                        lambda *a, **kw: {"scale": 1.0, "warnings": []})

    report = capture_mod.preview_report(ctx, marker_side_m=0.5, log=lambda *_a: None)
    ids = {c["id"] for c in report["checks"]}
    assert "T3_datum_unstable" not in ids
    assert "T3_datum_unstable" not in report["blocking"]


# ---------------------------------------------------------------------------
# preview_report on the real, cached calibration reconstructions (§12b.6)
# ---------------------------------------------------------------------------

def _cached_ctx(preset: str) -> ReconCtx | None:
    work = ROOT / "data" / "bench" / preset / "work"
    if not (work / "sparse").is_dir():
        return None
    return reconstruct(ROOT / "data" / "bench" / preset / "images", work,
                       reuse=True, log=lambda *_a: None)


@pytest.mark.parametrize("preset", ["twosided_cal", "twosided_b", "twosided"])
def test_preview_report_on_calibration_scenes_never_blocks(preset):
    """§11 criterion 3: none of these three (a clean two-arm capture in every
    case) may finalize blocked. `twosided`/`twosided_b` are the *negative*
    datum controls (G9 fires on the final result) and `twosided_cal` is the
    path-1 reference (G9 silent) — S4 predicts none of that (§12b.6, see the
    module docstring), but must still never falsely block any of the three."""
    ctx = _cached_ctx(preset)
    if ctx is None:
        pytest.skip(f"needs the cached {preset} benchmark reconstruction")
    names = sorted(ctx.views.keys())
    half = len(names) // 2
    arms = {n: ("A" if i < half else "B") for i, n in enumerate(names)}

    report = capture_mod.preview_report(ctx, arms=arms, marker_side_m=2.0,
                                        marker_dict="auto", log=lambda *_a: None)
    assert report["blocking"] == []
    assert report["n_registered"] == len(names)
    assert report["marker_error"] is None
    assert report["arm_separation_deg"] == pytest.approx(90.0, abs=5.0)

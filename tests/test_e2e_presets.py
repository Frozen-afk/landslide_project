"""T3.3 validation harness: end-to-end checks for tools/synth.py's non-`arc`
camera-path presets (implementation_plan.md Part B). `arc` itself stays
covered by tests/test_e2e_synth.py — this file is the other 7.

Slow (SfM + dense stereo per preset): excluded from the fast run the same
way test_e2e_synth.py is, via `pytest -q -k "not e2e"`.

Thresholds below are pinned from real runs of `python -m tools.benchmark
--presets <preset>` (see IMPLEMENTATION_PROGRESS.md's Tier 3 section for the
exact numbers), not guessed — per the task's own "don't weaken tests merely
to make them pass" rule, a bad number is recorded as a bad number.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "bench"


def _gen(preset: str):
    d = DATA / preset
    if not (d / "ground_truth.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"),
                        "--out", str(d), "--preset", preset], check=True)
    return d, json.loads((d / "ground_truth.json").read_text())


def _ortho_polygon_px(ctx, gt, meta):
    """Ground-truth polygon (world circle) -> model frame -> ortho pixels.

    Same construction as `test_e2e_synth.py`/`tools/benchmark.py`, using the
    pose-aware (F13) alignment so a (near-)collinear preset's polygon lands
    in the right place instead of off by the free rotation about the line.
    """
    from tools.benchmark import _umeyama_pose_aware

    from landslide.densify import estimate_up
    from landslide.geometry import camera_center
    from landslide.ortho import ground_basis

    e1, e2 = ground_basis(estimate_up(ctx.views, ctx.sparse))
    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t) for n in names]) * ctx.scale
    Q = np.array([gt["poses"][n]["eye"] for n in names])
    fwd_p = np.array([ctx.views[n].R[2, :] for n in names])
    fwd_q = np.array([np.asarray(gt["poses"][n]["R"])[2, :] for n in names])
    s, Rm, t = _umeyama_pose_aware(P, Q, P + fwd_p, Q + fwd_q)
    cx, cy, r = gt["bowl"]["x"], gt["bowl"]["y"], gt["polygon_radius_m"]
    ang = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    circle_world = np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang),
                                    np.zeros(72)])
    circle_model = ((circle_world - t) @ Rm) / s
    return np.column_stack([(circle_model @ e1 - meta["u0"]) / meta["res"],
                            (circle_model @ e2 - meta["v0"]) / meta["res"]])


# ---------------------------------------------------------------- sparse8 --
@pytest.fixture(scope="module")
def sparse8():
    return _gen("sparse8")


def test_sparse8_registers_all_views(sparse8):
    from landslide.sfm import reconstruct
    d, gt = sparse8
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 8, \
        f"only {len(ctx.views)}/8 registered — sparse8's yaw_span may need retuning"


def test_sparse8_volume(sparse8):
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = sparse8
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    info = aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    assert info["dict"] == "DICT_6X6_250"
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                  rim_px=14.0, artifacts_dir=d / "artifacts", log=print)
    print(f"\nSPARSE8 VOLUME: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}, coverage {res.get('coverage_frac')}")
    # RC1/A1: 8 views + a partial ray-cast hit fraction leaves ~45% coverage
    # of the traced polygon (measured, not the old bridged/interpolated
    # number) — G6 correctly reads that as status=rejected, not a tight
    # point-error target. Acceptance criterion is §7.1's honest range.
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")


# ------------------------------------------------------------------ nadir --
@pytest.fixture(scope="module")
def nadir():
    return _gen("nadir")


def test_nadir_registers_all_views(nadir):
    """Drone-like straight-down flight — full SfM registration is expected
    even though the vertical ArUco board (see MARKER in tools/synth.py) is
    geometrically near-invisible edge-on from directly overhead."""
    from landslide.sfm import reconstruct
    d, gt = nadir
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 21


def test_nadir_scale_rejected(nadir):
    """G2 (RC4/A3): aruco_scale now REFUSES a marker whose 4 triangulated
    sides disagree by >10% after triangulation, instead of silently applying
    a badly-wrong scale. Nadir's vertical ArUco board is exactly this case —
    camera line offset 7.5 m in y from the board, altitude 20 m -> ~21°
    grazing angle, so few views see it well and the triangulated square is
    far from square (measured 33-43% side spread across runs). A real
    nadir/drone survey needs a horizontal ground marker, not this scene's
    vertical board — that's a synth.py scene-design change, out of scope
    here (T3.3 characterizes current behavior). Previously this silently
    produced a ~40-60% wrong scale that `measure()` would apply with no
    warning surfaced to the user; now the bad reference is refused outright.
    """
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = nadir
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    with pytest.raises(RuntimeError, match="disagree"):
        aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)


# -------------------------------------------------------------- oblique60 --
@pytest.fixture(scope="module")
def oblique60():
    return _gen("oblique60")


def test_oblique60_marker_is_known_undetected(oblique60):
    """Known limitation (F23), not a regression target: the 2m ArUco board
    at ~10m from a 60deg-oblique, close-in camera path is only visible
    (post-detection-threshold) in a single frame, so `aruco_scale` can't
    triangulate corners from a stereo pair of views and raises. Real field
    markers are smaller and closer, which is a different failure mode
    (H2, out of scope here); this test pins "registers fine, scale
    correctly refuses" so a future scale-path change must notice this case
    explicitly rather than silently starting to "succeed" with a garbage
    scale.
    """
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = oblique60
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 21
    with pytest.raises(Exception):
        aruco_scale(ctx, side_m=gt["marker"]["side"], dict_name="auto", log=print)


# ------------------------------------------------------------- descending --
@pytest.fixture(scope="module")
def descending():
    return _gen("descending")


def test_descending_registers_most_views(descending):
    from landslide.sfm import reconstruct
    d, gt = descending
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) >= 20


def test_descending_focal_spread_is_tight(descending):
    """F12: a walking-downhill capture isn't collinear, but per-image
    self-calibration can still drift on a couple of cameras; the fix (an
    unrefined shared-focal retry when the spread is bad) must keep the
    registered set's focal lengths close together."""
    from landslide.sfm import _focal_spread, reconstruct
    d, gt = descending
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    lo, hi, ratio = _focal_spread(ctx.rec)
    assert ratio < 1.15, f"focal spread {ratio:.2f}x (lo {lo:.0f}, hi {hi:.0f})"


def test_descending_focal_spread_is_stable_across_clean_runs(descending):
    """Stabilization pass: `descending`'s per-camera focal self-calibration
    diverges on a fresh SfM run more often than not (COLMAP's own
    multi-threaded matching/BA is not bit-for-bit reproducible), so the
    focal-locked retry and its selection over the diverged attempt must
    both hold on repeated *clean* (non-cached) builds, not just once against
    whatever happens to be on disk."""
    from landslide.sfm import _focal_spread, reconstruct
    d, gt = descending
    for run in range(2):
        shutil.rmtree(d / "work", ignore_errors=True)
        ctx = reconstruct(d / "images", d / "work", reuse=False, log=print)
        lo, hi, ratio = _focal_spread(ctx.rec)
        assert ratio < 1.15, (
            f"run {run}: focal spread {ratio:.2f}x (lo {lo:.0f}, hi {hi:.0f})")


def test_descending_volume(descending):
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = descending
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                  rim_px=14.0, artifacts_dir=d / "artifacts", log=print)
    print(f"\nDESCENDING VOLUME: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}, reasons {res['reasons']}")
    # RC1+RC3 (A3): descending's genuine ~30% stereo coverage gap on a
    # walking-downhill path is a real, un-fixed limitation — the correct
    # pipeline response is `status=rejected` with named reasons, not a
    # confident number (see REMAINING_ACCURACY_PLAN.md §3's per-preset
    # verdict table: "explicit rejection today via G3/G5/G6"). No numeric
    # error threshold is the point: this preset should never read as ok.
    assert res["status"] == "rejected", res["reasons"]
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")


# --------------------------------------------------------------- collinear --
@pytest.fixture(scope="module")
def collinear():
    return _gen("collinear")


def test_collinear_registers_all_views(collinear):
    from landslide.sfm import reconstruct
    d, gt = collinear
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 21


def test_collinear_volume(collinear):
    """F13: the harness's own alignment (used only for cloud_rms/ortho-
    polygon placement, not by the pipeline) has a free rotation about a
    straight camera line unless pose-aware — see `_ortho_polygon_px`."""
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = collinear
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                  rim_px=14.0, artifacts_dir=d / "artifacts", log=print)
    print(f"\nCOLLINEAR PHOTO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}, reasons {res['reasons']}")
    # RC5 (A3, §7.4): single-azimuth capture — RC1's void is one solid block
    # instead of a fragmented crescent, and the camera path is collinear
    # (G4). Acceptance criterion is explicit: never `status=ok` at a
    # flattering point error; indicative or rejected are both correct
    # non-ok outcomes here (measured coverage sits right at the G6 boundary).
    assert res["status"] in ("indicative", "rejected"), res["reasons"]
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")


def test_collinear_ortho_volume(collinear, tmp_path):
    from landslide.densify import dense_cloud
    from landslide.ortho import render_orthophoto
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = collinear
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    dense_cloud(ctx, log=print)
    _, meta = render_orthophoto(ctx, jpg_path=tmp_path / "ortho.jpg",
                                meta_path=tmp_path / "ortho.json", log=print)
    poly_px = _ortho_polygon_px(ctx, gt, meta)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, None, poly_px, dense=True, mode="ortho", ortho=meta,
                  artifacts_dir=tmp_path, log=print)
    print(f"\nCOLLINEAR ORTHO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}")
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")


# ----------------------------------------------------------------- lowtex --
@pytest.fixture(scope="module")
def lowtex():
    return _gen("lowtex")


def test_lowtex_registers_all_views(lowtex):
    from landslide.sfm import reconstruct
    d, gt = lowtex
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 21


def test_lowtex_volume(lowtex):
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = lowtex
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                  rim_px=14.0, artifacts_dir=d / "artifacts", log=print)
    print(f"\nLOWTEX PHOTO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}, coverage {res.get('coverage_frac')}")
    # RC1/A1 (§7.3): ~100% ray-cast hit, but the TIN only observes ~61% of
    # the traced polygon (a 30-40 m² void on the far, camera-unseen side of
    # the bowl) — the tight bridging cull now reports that honestly instead
    # of interpolating across it, so `status=indicative` with the truth
    # inside [cut_measured, cut_upper] is the correct outcome, not a tight
    # point error.
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")


def test_lowtex_ortho_volume(lowtex, tmp_path):
    from landslide.densify import dense_cloud
    from landslide.ortho import render_orthophoto
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = lowtex
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    dense_cloud(ctx, log=print)
    _, meta = render_orthophoto(ctx, jpg_path=tmp_path / "ortho.jpg",
                                meta_path=tmp_path / "ortho.json", log=print)
    poly_px = _ortho_polygon_px(ctx, gt, meta)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, None, poly_px, dense=True, mode="ortho", ortho=meta,
                  artifacts_dir=tmp_path, log=print)
    print(f"\nLOWTEX ORTHO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}")
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")


# --------------------------------------------------------------- distorted --
@pytest.fixture(scope="module")
def distorted():
    return _gen("distorted")


def test_distorted_registers_all_views(distorted):
    from landslide.sfm import reconstruct
    d, gt = distorted
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    assert len(ctx.views) == 21


def test_distorted_volume(distorted):
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = distorted
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                  rim_px=14.0, artifacts_dir=d / "artifacts", log=print)
    print(f"\nDISTORTED PHOTO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}, coverage {res.get('coverage_frac')}")
    # RC1/A1 — see test_lowtex_volume's comment; ~62% coverage here.
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")


def test_distorted_ortho_volume(distorted, tmp_path):
    from landslide.densify import dense_cloud
    from landslide.ortho import render_orthophoto
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = distorted
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    dense_cloud(ctx, log=print)
    _, meta = render_orthophoto(ctx, jpg_path=tmp_path / "ortho.jpg",
                                meta_path=tmp_path / "ortho.json", log=print)
    poly_px = _ortho_polygon_px(ctx, gt, meta)
    truth = gt["volume_true_polygon_m3"]
    res = measure(ctx, None, poly_px, dense=True, mode="ortho", ortho=meta,
                  artifacts_dir=tmp_path, log=print)
    print(f"\nDISTORTED ORTHO: cut_measured {res['cut_measured_m3']:.1f}, "
          f"cut_upper {res['cut_upper_m3']:.1f}, truth {truth:.1f}, "
          f"status {res['status']}")
    assert res["cut_measured_m3"] <= truth <= res["cut_upper_m3"], (
        f"truth {truth:.1f} outside [{res['cut_measured_m3']:.1f}, "
        f"{res['cut_upper_m3']:.1f}]")
    assert res["status"] in ("indicative", "rejected")

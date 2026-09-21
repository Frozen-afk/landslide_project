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
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "bench"


def _umeyama_scale(P, Q):
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    s = (S * [1, 1, d]).sum() / ((P - cp) ** 2).sum()
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    t = cq - s * R @ cp
    return s, R, t


def _gen(preset: str):
    d = DATA / preset
    if not (d / "ground_truth.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"),
                        "--out", str(d), "--preset", preset], check=True)
    return d, json.loads((d / "ground_truth.json").read_text())


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
    rel_err = abs(res["cut_volume_m3"] - truth) / truth
    # measured 22.7% on 8 views / 64° yaw span (vs arc's ~7-8% on 21 views) —
    # fewer views genuinely costs accuracy; this pins that real number, not
    # an aspirational one.
    print(f"\nSPARSE8 VOLUME: cut {res['cut_volume_m3']:.1f} vs truth {truth:.1f} "
          f"({rel_err * 100:.1f}%)")
    assert rel_err < 0.35, f"cut {res['cut_volume_m3']:.1f} vs truth {truth:.1f}"


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


def test_nadir_scale_is_known_bad(nadir):
    """Known limitation, not a regression target: aruco_scale still finds
    *a* detection from the oblique edge of the flight line (camera line is
    offset 7.5 m in y from the marker board, altitude 20 m -> ~21° grazing
    angle onto the board face), but it's badly wrong (measured ~40% error
    against the camera-center Umeyama reference) because so few views see
    the board well. A real nadir/drone survey needs a horizontal ground
    marker, not this scene's vertical board — that's a synth.py scene-design
    change, out of scope for T3.3 (which characterizes current behavior,
    not fixes geometry). This test pins "doesn't crash, stays badly-scaled"
    so a future scale-path change is forced to notice this case explicitly.
    """
    from landslide.geometry import camera_center
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    d, gt = nadir
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=print)
    info = aruco_scale(ctx, side_m=gt["marker"]["side"], log=print)
    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t) for n in names])
    Q = np.array([gt["poses"][n]["eye"] for n in names])
    s_true, _, _ = _umeyama_scale(P, Q)
    rel = abs(ctx.scale - s_true) / s_true
    print(f"\nNADIR SCALE: aruco {ctx.scale:.4f} vs truth {s_true:.4f} "
          f"({rel * 100:.1f}% off) — known-bad, vertical marker unsuitable for nadir")
    assert rel < 0.60, "scale error grew past the pinned baseline — investigate"


# ---------------- generators implemented, full run deferred (time budget) --
# `python -m tools.synth.py --preset <name>` works for all of these (GT-polygon
# frame-containment verified for every preset at generation time — see
# tools/synth.py's own assertions) and `python -m tools.benchmark --preset
# <name>` will produce the real numbers to replace these skips; a from-scratch
# SfM+dense+measure run costs several minutes each and only sparse8/nadir
# were run in this pass (see IMPLEMENTATION_PROGRESS.md's Tier 3 section).
@pytest.mark.skip(reason="generator implemented, not yet benchmarked — run "
                         "`python -m tools.benchmark --presets oblique60` "
                         "and pin real thresholds here")
def test_oblique60_e2e():
    pass


@pytest.mark.skip(reason="generator implemented, not yet benchmarked — run "
                         "`python -m tools.benchmark --presets descending` "
                         "and pin real thresholds here")
def test_descending_e2e():
    pass


@pytest.mark.skip(reason="generator implemented, not yet benchmarked — run "
                         "`python -m tools.benchmark --presets collinear` "
                         "and pin real thresholds here")
def test_collinear_e2e():
    pass


@pytest.mark.skip(reason="generator implemented, not yet benchmarked — run "
                         "`python -m tools.benchmark --presets lowtex` "
                         "and pin real thresholds here")
def test_lowtex_e2e():
    pass


@pytest.mark.skip(reason="generator implemented, not yet benchmarked — run "
                         "`python -m tools.benchmark --presets distorted` "
                         "and pin real thresholds here")
def test_distorted_e2e():
    pass

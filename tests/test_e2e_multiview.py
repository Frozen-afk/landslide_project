"""End-to-end multi-view marking on a real reconstruction: the `twosided_cal`
preset (SfM + ArUco scale + dense stereo, the one synthetic scene whose
single-view result is `status="ok"`), outlined with the synthetic
ground-truth masks from tools/mv_benchmark.py.

Slow (builds the scene/reconstruction/dense cloud when not cached, then runs
the full measurement): excluded from the fast run like the other e2e files,
`pytest -q -k "not e2e"`. Thresholds are pinned from tools/mv_benchmark.py
runs (MULTIVIEW_REGION_PROGRESS.md §5), not guessed.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "bench"
QUIET = lambda *_: None  # noqa: E731

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def scene():
    from landslide import multiview as mv
    from landslide.densify import dense_cloud
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct
    from tools.mv_benchmark import gt_masks

    d = DATA / "twosided_cal"
    if not (d / "ground_truth.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"),
                        "--out", str(d), "--preset", "twosided_cal"], check=True)
    gt = json.loads((d / "ground_truth.json").read_text())
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=QUIET)
    aruco_scale(ctx, side_m=gt["marker"]["side"], dict_name="auto", log=QUIET)
    dense_cloud(ctx, log=QUIET)
    masks = {n: m for n, m in gt_masks(d, gt).items() if n in ctx.views}
    pov = mv.pov_groups(ctx)
    order = [s["image"] for s in mv.suggest_images(ctx, pov, {}) if s["image"] in masks]
    return gt, ctx, masks, order


def test_suggested_outlines_reach_four_by_two(scene):
    from landslide import multiview as mv
    _, ctx, _, order = scene
    assert mv.requirement(mv.pov_groups(ctx), order[:8])["met"]


def test_multiview_measure_is_high_confidence_and_accurate(scene):
    from landslide.pipeline import measure
    gt, ctx, masks, order = scene
    anns = [{"image": n, "polygon": masks[n]} for n in order[:8]]
    res = measure(ctx, None, None, annotations=anns, log=QUIET)
    truth = gt["volume_true_polygon_m3"]
    assert res["region_method"] == "multiview_fusion"
    assert res["status"] == "ok" and res["confidence"] == "high", res["reasons"]
    assert abs(res["cut_volume_m3"] - truth) / truth < 0.05
    assert res["coverage_frac"] > 0.85
    mvr = res["multiview"]
    assert mvr["requirement"]["met"] and mvr["n_used"] == 8 and mvr["components"] == 1
    assert mvr["area_best_m2"] == pytest.approx(np.pi * gt["polygon_radius_m"] ** 2, rel=0.03)
    assert mvr["uncertain_area_frac"] < 0.15 and mvr["weak_support_frac"] < 0.15
    b = res["volume_bounds_m3"]
    assert b["lower"]["area_m2"] < b["best"]["area_m2"] < b["upper"]["area_m2"]
    assert b["lower"]["cut_volume_m3"] <= res["cut_volume_m3"] <= b["upper"]["cut_volume_m3"]


def test_one_bad_outline_is_rejected_on_a_real_cloud(scene):
    from landslide import multiview as mv
    gt, ctx, masks, order = scene
    bad = order[2]
    shifted = (np.asarray(masks[bad]) * 1.3 + [0.22 * gt["image_size"][0], -40.0]).tolist()
    anns = [{"image": n, "polygon": shifted if n == bad else masks[n]} for n in order[:8]]
    clean = mv.fuse(ctx, [{"image": n, "polygon": masks[n]} for n in order[:8]], log=QUIET)
    st = mv.fuse(ctx, anns, log=QUIET)["stats"]
    assert [a["image"] for a in st["annotations"] if a["rejected"]] == [bad]
    assert st["area_best_m2"] == pytest.approx(clean["stats"]["area_best_m2"], rel=0.02)
    assert not st["requirement"]["met"]

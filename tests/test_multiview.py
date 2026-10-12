"""Multi-view region fusion (landslide/multiview.py) on an analytic scene:
a 1 m-deep cosine bowl (radius 4 m) in flat ground, eight cameras in four
POV groups of two around it. No SfM, no stereo — exact cameras, exact
points — so every assertion is about the fusion logic itself."""
from pathlib import Path

import numpy as np
import pytest

from landslide import multiview as mv
from landslide.gates import classify_measurement_path, evaluate_gates
from landslide.sfm import ImageView, ReconCtx

UP = np.array([0.0, 0.0, 1.0])
R_REGION = 4.0
W, H, F = 1000, 800, 1000.0
QUIET = lambda *_: None  # noqa: E731


def _look_at(eye, target=(0.0, 0.0, 0.0)):
    z = np.asarray(target, float) - eye
    z /= np.linalg.norm(z)
    x = np.cross(z, UP)
    x /= np.linalg.norm(x)
    R = np.stack([x, np.cross(z, x), z])
    return R, -R @ eye


def _view(name, i, az_deg, radius=20.0, height=10.0):
    a = np.radians(az_deg)
    eye = np.array([radius * np.cos(a), radius * np.sin(a), height])
    R, t = _look_at(eye)
    K = np.array([[F, 0, W / 2], [0, F, H / 2], [0, 0, 1.0]])
    return ImageView(name, i, 1, R, t, K, np.zeros(5), W, H, Path(name))


def _ground(extra=None):
    g = np.arange(-15, 15, 0.1)
    x, y = (a.ravel() for a in np.meshgrid(g, g))
    r = np.hypot(x, y)
    z = np.where(r < R_REGION, -0.5 * (1 + np.cos(np.pi * r / R_REGION)), 0.0)
    pts = np.column_stack([x, y, z])
    return pts if extra is None else np.vstack([pts, extra])


def _ctx(azimuths, pts=None):
    views = {f"v{i:02d}": _view(f"v{i:02d}", i, az) for i, az in enumerate(azimuths)}
    pts = _ground() if pts is None else pts
    return ReconCtx(rec=None, views=views, sparse=pts[::40], sparse_colors=None,
                    photos_dir=Path("."), workdir=Path("."), scale=1.0,
                    scale_info={"applied": True},
                    dense={"points": pts.astype(np.float32),
                           "colors": np.zeros((len(pts), 3), np.uint8)})


def _disc(view, radius=R_REGION, centre=(0.0, 0.0), arc=(0.0, 2 * np.pi), n=72):
    a = np.linspace(arc[0], arc[1], n, endpoint=arc[1] - arc[0] < 2 * np.pi - 1e-9)
    ring = np.column_stack([centre[0] + radius * np.cos(a), centre[1] + radius * np.sin(a),
                            np.zeros(len(a))])
    return view.project(ring)[0].tolist()


FOUR_BY_TWO = [-3, 3, 87, 93, 177, 183, 267, 273]


@pytest.fixture(scope="module")
def ctx():
    return _ctx(FOUR_BY_TWO)


def _anns(ctx, names=None, edit=None):
    names = names or sorted(ctx.views)
    return [{"image": n, "polygon": (edit or {}).get(n) or _disc(ctx.views[n])} for n in names]


def test_pov_groups_split_by_direction_not_by_photo_count(ctx):
    pov = mv.pov_groups(ctx, up=UP)
    assert len(pov["groups"]) == 4
    assert all(len(g["images"]) == 2 for g in pov["groups"].values())
    # eight photos panned from ONE standpoint are one POV group, not eight
    same = _ctx([0, 1, 2, 3, 4, 5, 6, 7])
    assert len(mv.pov_groups(same, up=UP)["groups"]) == 1
    req = mv.requirement(mv.pov_groups(same, up=UP), sorted(same.views))
    assert not req["met"] and not req["achievable"] and req["n_annotations"] == 8


def test_requirement_is_four_groups_times_two(ctx):
    pov = mv.pov_groups(ctx, up=UP)
    names = sorted(ctx.views)
    assert mv.requirement(pov, names)["met"]
    assert not mv.requirement(pov, names[:7])["met"]            # 7 outlines
    assert not mv.requirement(pov, names[:6])["met"]            # 3 groups x 2
    one_each = [pov["groups"][g]["images"][0] for g in pov["groups"]]
    assert not mv.requirement(pov, one_each)["met"]             # 4 groups x 1


def test_suggestions_reach_four_by_two_and_respect_skips(ctx):
    pov = mv.pov_groups(ctx, up=UP)
    sug = mv.suggest_images(ctx, pov, {})
    assert len({s["group"] for s in sug[:4]}) == 4, "first four must be four viewpoints"
    assert mv.requirement(pov, [s["image"] for s in sug[:8]])["met"]
    skipped = sug[0]["image"]
    sug2 = mv.suggest_images(ctx, pov, {skipped: {"status": "skipped"},
                                        sug[1]["image"]: {"status": "accepted"}})
    assert skipped not in [s["image"] for s in sug2]
    assert sug[1]["image"] not in [s["image"] for s in sug2]


def test_clean_masks_recover_the_region(ctx):
    f = mv.fuse(ctx, _anns(ctx), up=UP, log=QUIET)
    st = f["stats"]
    assert st["requirement"]["met"] and st["n_used"] == 8 and st["components"] == 1
    assert st["area_best_m2"] == pytest.approx(np.pi * R_REGION ** 2, rel=0.04)
    assert st["area_lower_m2"] <= st["area_best_m2"] <= st["area_upper_m2"]
    assert all(a["iou"] > 0.9 and not a["rejected"] for a in st["annotations"])
    pts = ctx.dense["points"]
    r = np.hypot(pts[:, 0], pts[:, 1])
    assert f["confident"][r < R_REGION - 0.3].mean() > 0.99
    assert not f["upper"][r > R_REGION + 0.3].any()
    # the uncertain class is a thin band on the boundary, nothing else
    unc = f["upper"] & ~f["confident"]
    assert unc.any() and np.abs(r[unc] - R_REGION).max() < 0.4


def test_boundary_noise_widens_the_uncertain_band_not_the_region(ctx):
    rng = np.random.default_rng(1)
    noisy = {n: (np.asarray(_disc(v)) + rng.normal(0, 6, (72, 2))).tolist()
             for n, v in ctx.views.items()}
    clean = mv.fuse(ctx, _anns(ctx), up=UP, log=QUIET)["stats"]
    st = mv.fuse(ctx, _anns(ctx, edit=noisy), up=UP, log=QUIET)["stats"]
    assert st["area_best_m2"] == pytest.approx(clean["area_best_m2"], rel=0.05)
    assert st["uncertain_area_frac"] > clean["uncertain_area_frac"]
    assert not any(a["rejected"] for a in st["annotations"])


def test_one_bad_mask_is_rejected_and_does_not_move_the_region(ctx):
    bad = (np.asarray(_disc(ctx.views["v02"])) + [260.0, 0.0]).tolist()
    clean = mv.fuse(ctx, _anns(ctx), up=UP, log=QUIET)["stats"]
    st = mv.fuse(ctx, _anns(ctx, edit={"v02": bad}), up=UP, log=QUIET)["stats"]
    by = {a["image"]: a for a in st["annotations"]}
    assert by["v02"]["rejected"] and by["v02"]["iou"] < mv.IOU_REJECT
    assert [a["image"] for a in st["annotations"] if a["rejected"]] == ["v02"]
    assert st["area_best_m2"] == pytest.approx(clean["area_best_m2"], rel=0.02)
    assert not st["requirement"]["met"], "a rejected outline no longer counts toward 4x2"
    res = {"cloud": "dense", "multiview": st}
    evaluate_gates(_GateCtx(), res, "multiview_fusion")
    assert "MV_mask_disagreement" in res["reason_ids"]
    assert "MV_insufficient_pov" in res["reason_ids"]


def test_two_disagreeing_outlines_are_flagged_not_arbitrarily_rejected(ctx):
    # with only two outlines there is no majority to say which one is wrong
    bad = (np.asarray(_disc(ctx.views["v02"])) + [260.0, 0.0]).tolist()
    st = mv.fuse(ctx, _anns(ctx, ["v00", "v02"], {"v02": bad}), up=UP, log=QUIET)["stats"]
    assert not any(a["rejected"] for a in st["annotations"])
    res = {"cloud": "dense", "multiview": st}
    evaluate_gates(_GateCtx(), res, "multiview_fusion")
    assert "MV_uncertain_boundary" in res["reason_ids"]


def test_occluded_views_do_not_vote_against_hidden_points():
    # a 4 m wall at x=8 hides the x>0 half of the region from the two
    # cameras at azimuth ~0; they outline only the half they can see
    wy, wz = (a.ravel() for a in np.meshgrid(np.arange(-9, 9, 0.05), np.arange(0, 4, 0.05)))
    wall = np.column_stack([np.full(len(wy), 8.0), wy, wz])
    ctx = _ctx(FOUR_BY_TWO, _ground(wall))
    half = {n: _disc(ctx.views[n], arc=(np.pi / 2, 3 * np.pi / 2), n=40) for n in ("v00", "v01")}
    f = mv.fuse(ctx, _anns(ctx, edit=half), up=UP, log=QUIET)
    pts = ctx.dense["points"]
    r = np.hypot(pts[:, 0], pts[:, 1])
    hidden = (r < R_REGION - 0.5) & (pts[:, 0] > 1.0) & (pts[:, 2] < 0.01)
    assert hidden.sum() > 500
    # exactly the three groups that can see those points voted on them
    assert (f["n_groups"][hidden] == 3).mean() > 0.97
    assert f["confident"][hidden].mean() > 0.97, \
        "an occluded view must abstain, not vote 'outside'"
    assert not any(a["rejected"] for a in f["stats"]["annotations"])


def test_missing_pov_groups_fails_the_requirement_and_the_gate():
    # eight outlines, but from two viewpoints only
    ctx = _ctx([-6, -2, 2, 6, 84, 88, 92, 96])
    st = mv.fuse(ctx, _anns(ctx), up=UP, log=QUIET)["stats"]
    assert st["n_annotations"] == 8 and st["n_groups"] == 2
    assert not st["requirement"]["met"]
    res = {"cloud": "dense", "multiview": st}
    status, _ = evaluate_gates(_GateCtx(), res, "multiview_fusion")
    assert status == "indicative" and res["reason_ids"] == ["MV_insufficient_pov"]
    assert classify_measurement_path(status, res["reason_ids"], "rim_plane") == "marking"


def test_disconnected_selection_is_flagged(ctx):
    # half the viewpoints outline one patch, the other half a different one
    a = {n: _disc(ctx.views[n], 2.0, (-6.0, 0.0)) for n in ("v00", "v01", "v02", "v03")}
    b = {n: _disc(ctx.views[n], 2.0, (6.0, 0.0)) for n in ("v04", "v05", "v06", "v07")}
    st = mv.fuse(ctx, _anns(ctx, edit={**a, **b}), up=UP, log=QUIET)["stats"]
    assert st["components"] == 2
    res = {"cloud": "dense", "multiview": st}
    evaluate_gates(_GateCtx(), res, "multiview_fusion")
    assert "MV_disconnected" in res["reason_ids"]
    assert "MV_uncertain_boundary" in res["reason_ids"]


def test_region_reprojects_into_an_unmarked_view_as_an_editable_polygon(ctx):
    f = mv.fuse(ctx, _anns(ctx, ["v00", "v02", "v04", "v06"]), up=UP, log=QUIET)
    poly = np.asarray(mv.project_ring(ctx.views["v03"], f["rings"]["best"]))
    truth = np.asarray(_disc(ctx.views["v03"], n=360))
    d = np.linalg.norm(poly[:, None, :] - truth[None, :, :], axis=2).min(axis=1)
    assert 3 <= len(poly) <= 100
    assert d.mean() < 6.0, f"reprojected outline is {d.mean():.1f} px off the true edge"
    # a view looking away from the region gets no suggestion
    away = _view("away", 99, 0.0)
    away.R, away.t = _look_at(away.center, target=(60.0, 0.0, 0.0))
    assert mv.project_ring(away, f["rings"]["best"]) is None


class _GateCtx:
    dense = {"points": np.zeros((30_000, 3))}
    scale_info = {}
    warnings = []
    rec = object()


def test_clean_four_by_two_fires_no_multiview_gate(ctx):
    st = mv.fuse(ctx, _anns(ctx), up=UP, log=QUIET)["stats"]
    res = {"cloud": "dense", "multiview": st}
    evaluate_gates(_GateCtx(), res, "multiview_fusion")
    assert [r for r in res["reason_ids"] if r.startswith("MV_")] == []


def test_measure_multiview_and_legacy_single_view(ctx, monkeypatch):
    """The fused region runs through the unchanged photo-mode pipeline; a
    single-photo call takes the old path, numerically untouched, and is
    only LABELLED lower-confidence."""
    from landslide import pipeline

    truth = 0.5 * np.pi * R_REGION ** 2 * (1 - 4 / np.pi ** 2)     # cosine bowl, depth 1
    res = pipeline.measure(ctx, None, None, annotations=_anns(ctx), log=QUIET)
    assert res["region_method"] == "multiview_fusion"
    assert res["annotation_mode"] == "multi_view"
    assert res["cut_volume_m3"] == pytest.approx(truth, rel=0.05)
    b = res["volume_bounds_m3"]
    assert b["lower"]["area_m2"] <= b["best"]["area_m2"] <= b["upper"]["area_m2"]
    assert b["lower"]["cut_volume_m3"] <= res["cut_volume_m3"] <= b["upper"]["cut_volume_m3"]
    assert res["multiview"]["requirement"]["met"]
    assert "net_volume_ci95_m3" in res, "the best selection keeps its bootstrap CI"
    assert not [r for r in res["reason_ids"] if r.startswith("MV_")]
    assert res["confidence"] == ("high" if res["status"] == "ok" else res["status"])

    one = _anns(ctx, ["v00"])[0]
    legacy = pipeline.measure(ctx, one["image"], one["polygon"], log=QUIET)
    assert legacy["region_method"] == "ground_frame"
    assert legacy["annotation_mode"] == "single_view" and legacy["confidence"] == "lower"
    assert "multiview" not in legacy and "volume_bounds_m3" not in legacy
    assert not [r for r in legacy["reason_ids"] if r.startswith("MV_")]
    # same call with the multi-view labelling stripped must be the pre-existing result
    from landslide.ground import select_region_ground
    from landslide.volume import prism_volume
    from landslide.densify import estimate_up
    up = estimate_up(ctx.views, ctx.sparse)
    interior, rim, g = select_region_ground(ctx, one["image"], one["polygon"], up=up, log=QUIET)
    pts = ctx.dense["points"]
    ref = prism_volume(pts[interior], pts[rim], log=QUIET, up=up,
                       polygon_ground=np.asarray(g["ground_polygon"]))
    for k in ("net_volume_m3", "cut_volume_m3", "fill_volume_m3", "area_m2", "coverage_frac"):
        assert legacy[k] == pytest.approx(ref[k], rel=1e-9), k

    few = pipeline.measure(ctx, None, None, annotations=_anns(ctx, ["v00", "v02"]), log=QUIET)
    assert few["status"] != "ok" and "MV_insufficient_pov" in few["reason_ids"]
    assert few["confidence"] != "high" and few["measurement_path"] == "marking"

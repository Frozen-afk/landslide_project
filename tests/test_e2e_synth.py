"""End-to-end validation on the synthetic scene (slow: SfM + stereo MVS).

Run:  .venv/bin/python -m pytest tests/test_e2e_synth.py -s -x
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synth"


@pytest.fixture(scope="module")
def synth():
    if not (DATA / "ground_truth.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"),
                        "--out", str(DATA)], check=True)
    return json.loads((DATA / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def ctx(synth):
    from landslide.sfm import reconstruct
    return reconstruct(DATA / "images", DATA / "work", reuse=True, log=print)


def test_reconstruction_quality(ctx, synth):
    n_views = len(ctx.views)
    assert n_views >= 15, f"only {n_views} views registered"


def _umeyama_scale(P, Q):
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    s = (S * [1, 1, d]).sum() / ((P - cp) ** 2).sum()
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    t = cq - s * R @ cp
    return s, R, t


def test_aruco_scale_accuracy(ctx, synth):
    from landslide.geometry import camera_center
    from landslide.scaling import aruco_scale
    info = aruco_scale(ctx, side_m=synth["marker"]["side"],
                       dict_name="auto", log=print)
    assert info["dict"] == "DICT_6X6_250"          # auto found the right one
    assert len(info["views_used"]) >= 4
    assert info["side_spread_rel"] < 0.03

    # ground-truth check via camera centers: similarity recon->world
    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t) for n in names])
    Q = np.array([synth["poses"][n]["eye"] for n in names])
    s, Rm, t = _umeyama_scale(P, Q)
    res = np.linalg.norm((s * Rm @ P.T).T + t - Q, axis=1)
    assert res.max() < 0.3, f"camera centers misaligned by {res.max():.2f} m"
    rel = abs(ctx.scale - s) / s
    print(f"\nSCALE: aruco {ctx.scale:.4f} vs centers {s:.4f} "
          f"({rel * 100:.2f}% off), center residual max {res.max():.3f} m")
    assert rel < 0.03, f"aruco scale {ctx.scale} vs center-based {s}"


def test_manual_scale_matches_aruco(ctx, synth):
    from landslide.scaling import aruco_scale, manual_scale
    if not ctx.scale_info.get("applied"):
        aruco_scale(ctx, side_m=synth["marker"]["side"], log=print)
    side = synth["marker"]["side"]
    corners = np.array(synth["marker"]["corners_world"])   # (4,3)
    K = np.array(synth["K"])
    specs = []
    for name in (f"IMG_02.png", f"IMG_18.png"):
        pose = synth["poses"][name]
        R, t = np.array(pose["R"]), np.array(pose["t"])
        Pc = corners @ R.T + t
        uv = np.stack([K[0, 0] * Pc[:, 0] / Pc[:, 2] + K[0, 2],
                       K[1, 1] * Pc[:, 1] / Pc[:, 2] + K[1, 2]], 1)
        specs.append({"image": name,
                      "p1": uv[0].tolist(), "p2": uv[1].tolist()})
    info = manual_scale(ctx, specs[0], specs[1], length_m=side, log=print)
    assert abs(info["scale"] - ctx.scale) / ctx.scale < 0.02


def test_volume_end_to_end(ctx, synth):
    from landslide.pipeline import measure
    if not ctx.scale_info.get("applied"):
        from landslide.scaling import aruco_scale
        aruco_scale(ctx, side_m=synth["marker"]["side"], log=print)
    res = measure(ctx, synth["polygon_image"], synth["polygon_px"],
                  dense=True, rim_px=14.0, artifacts_dir=DATA / "artifacts",
                  log=print)
    truth = synth["volume_true_polygon_m3"]
    print(f"\nVOLUME: measured cut {res['cut_volume_m3']:.1f} m^3, "
          f"truth {truth:.1f} m^3, net {res['net_volume_m3']:.1f}, "
          f"fill {res['fill_volume_m3']:.1f}, points {res['n_points']}, "
          f"dense pts {res['n_cloud_points']}")
    rel_err = abs(res["cut_volume_m3"] - truth) / truth
    assert rel_err < 0.30, f"cut {res['cut_volume_m3']:.1f} vs truth {truth:.1f}"
    assert res["fill_volume_m3"] < 0.35 * truth
    assert res["n_points"] > 5000


def test_volume_ortho_end_to_end(ctx, synth, tmp_path):
    """Same measurement traced on the top-down orthophoto (no parallax)."""
    import numpy as np
    from landslide.ortho import render_orthophoto, select_region_ortho
    from landslide.pipeline import measure
    if not ctx.scale_info.get("applied"):
        from landslide.scaling import aruco_scale
        aruco_scale(ctx, side_m=synth["marker"]["side"], log=print)

    _, meta = render_orthophoto(ctx, jpg_path=tmp_path / "ortho.jpg",
                                meta_path=tmp_path / "ortho.json", log=print)
    # ground-truth polygon (world circle) -> model frame -> ortho pixels
    from landslide.densify import estimate_up
    from landslide.geometry import camera_center
    from landslide.ortho import ground_basis
    e1, e2 = ground_basis(estimate_up(ctx.views, ctx.sparse))
    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t)
                  for n in names]) * ctx.scale      # metric frame: matches
    Q = np.array([synth["poses"][n]["eye"] for n in names])  # the ortho meta
    s, Rm, t = _umeyama_scale(P, Q)          # world ~= s * Rm @ model + t
    cx, cy, r = synth["bowl"]["x"], synth["bowl"]["y"], synth["polygon_radius_m"]
    ang = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    circle_world = np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang),
                                    np.zeros(72)])
    circle_model = ((circle_world - t) @ Rm) / s   # row-vector inverse of
    # world = s * Rm @ model + t  =>  model = (world - t) @ Rm / s
    poly_px = np.column_stack([(circle_model @ e1 - meta["u0"]) / meta["res"],
                               (circle_model @ e2 - meta["v0"]) / meta["res"]])

    res = measure(ctx, None, poly_px, dense=True, mode="ortho", ortho=meta,
                  artifacts_dir=tmp_path, log=print)
    truth = synth["volume_true_polygon_m3"]
    print(f"\nORTHO VOLUME: cut {res['cut_volume_m3']:.1f} m^3, truth {truth:.1f}, "
          f"datum {res['datum']}, rim band {res['rim_band_m']}")
    assert res["mode"] == "ortho"
    rel_err = abs(res["cut_volume_m3"] - truth) / truth
    assert rel_err < 0.30, f"cut {res['cut_volume_m3']:.1f} vs truth {truth:.1f}"
    assert res["n_points"] > 5000

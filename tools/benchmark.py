"""T3.3 validation harness: run SfM -> scale -> dense -> measure for each
synthetic camera-path preset (tools/synth.py --preset) and print a Markdown
regression table.

Usage:  .venv/bin/python -m tools.benchmark --presets arc,sparse8,oblique60
        (default: all presets in tools.synth.PRESETS)

This is a reporting tool, not a test — tests/test_e2e_presets.py pins real
thresholds derived from runs of this harness. A preset stage that fails
(e.g. `nadir`'s vertical ArUco board is geometrically invisible to a
straight-down camera) is reported as "n/a: <reason>" in that column rather
than aborting the whole table — one bad preset shouldn't hide the rest.
"""
from __future__ import annotations

import argparse
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.synth import PRESETS, terrain_height, build_terrain  # noqa: E402


def _umeyama_scale(P, Q):
    """Least-squares similarity P -> Q. Returns (scale, R, t)."""
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    s = (S * [1, 1, d]).sum() / ((P - cp) ** 2).sum()
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    t = cq - s * R @ cp
    return s, R, t


def run_preset(preset: str, work_root: Path, seed: int = 7) -> dict:
    import json

    from landslide.geometry import camera_center
    from landslide.pipeline import measure
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct

    out = {"preset": preset}
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()

    data_dir = work_root / preset
    if not (data_dir / "ground_truth.json").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "synth.py"),
                        "--out", str(data_dir), "--preset", preset,
                        "--seed", str(seed)], check=True)
    gt = json.loads((data_dir / "ground_truth.json").read_text())

    try:
        ctx = reconstruct(data_dir / "images", data_dir / "work",
                          reuse=True, log=lambda *_: None)
    except Exception as e:
        out["registered"] = f"FAILED: {e}"
        out["runtime_s"] = time.time() - t0
        return out
    n_total = len(gt["poses"])
    out["registered"] = f"{len(ctx.views)}/{n_total}"

    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t) for n in names])
    Q = np.array([gt["poses"][n]["eye"] for n in names])
    s_true, Rm, tm = _umeyama_scale(P, Q)

    try:
        aruco_scale(ctx, side_m=gt["marker"]["side"], dict_name="auto",
                   log=lambda *_: None)
        out["scale_err_pct"] = abs(ctx.scale - s_true) / s_true * 100
    except Exception as e:
        out["scale_err_pct"] = f"n/a: {e}"
        out["photo_vol_err_pct"] = "n/a: no scale"
        out["ortho_vol_err_pct"] = "n/a: no scale"
        out["cloud_rms_m"] = "n/a: no scale"
        out["runtime_s"] = time.time() - t0
        out["peak_rss_mb"] = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024
        return out

    truth = gt["volume_true_polygon_m3"]
    try:
        res = measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True,
                     rim_px=14.0, artifacts_dir=data_dir / "artifacts",
                     log=lambda *_: None)
        out["photo_vol_err_pct"] = abs(res["cut_volume_m3"] - truth) / truth * 100
    except Exception as e:
        out["photo_vol_err_pct"] = f"n/a: {e}"

    try:
        from landslide.densify import estimate_up
        from landslide.ortho import ground_basis, render_orthophoto
        _, meta = render_orthophoto(ctx, jpg_path=data_dir / "artifacts" / "ortho.jpg",
                                    meta_path=data_dir / "artifacts" / "ortho.json",
                                    log=lambda *_: None)
        e1, e2 = ground_basis(estimate_up(ctx.views, ctx.sparse))
        Pw = P * ctx.scale
        s2, Rm2, t2 = _umeyama_scale(Pw, Q)   # s2 ~= 1: Pw already metric
        cx, cy, r = gt["bowl"]["x"], gt["bowl"]["y"], gt["polygon_radius_m"]
        ang = np.linspace(0, 2 * np.pi, 72, endpoint=False)
        circ_w = np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang),
                                  np.zeros(72)])
        circ_model = ((circ_w - t2) @ Rm2) / s2
        poly_px = np.column_stack([(circ_model @ e1 - meta["u0"]) / meta["res"],
                                   (circ_model @ e2 - meta["v0"]) / meta["res"]])
        res_o = measure(ctx, None, poly_px, dense=True, mode="ortho", ortho=meta,
                        artifacts_dir=data_dir / "artifacts", log=lambda *_: None)
        out["ortho_vol_err_pct"] = abs(res_o["cut_volume_m3"] - truth) / truth * 100
    except Exception as e:
        out["ortho_vol_err_pct"] = f"n/a: {e}"

    try:
        pts, _ = ctx.cloud(dense=True)
        if len(pts) > 5000:
            pts = pts[np.random.default_rng(0).choice(len(pts), 5000, replace=False)]
        world = (s_true * Rm @ pts.T).T + tm
        _, _, z_grid, _, _ = build_terrain(seed, tex_scale=PRESETS[preset]["tex_scale"])
        # X,Y grid discarded above (not needed: terrain_height re-derives fx/fy from x,y)
        from tools.synth import L, N
        Xg, Yg = np.meshgrid(np.linspace(0, L, N), np.linspace(0, L, N), indexing="ij")
        in_bounds = (world[:, 0] > 0) & (world[:, 0] < L) & (world[:, 1] > 0) & (world[:, 1] < L)
        world = world[in_bounds]
        z_true = np.array([terrain_height(Xg, Yg, z_grid, x, y) for x, y in world[:, :2]])
        out["cloud_rms_m"] = float(np.sqrt(np.mean((world[:, 2] - z_true) ** 2)))
    except Exception as e:
        out["cloud_rms_m"] = f"n/a: {e}"

    out["runtime_s"] = time.time() - t0
    out["peak_rss_mb"] = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0) / 1024
    return out


def _fmt(v, suffix=""):
    if isinstance(v, float):
        return f"{v:.2f}{suffix}"
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--presets", default=",".join(sorted(PRESETS)))
    ap.add_argument("--out", default=str(ROOT / "data" / "bench"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--md-out", default=None, help="optional path to also write the table")
    args = ap.parse_args()
    presets = args.presets.split(",")
    work_root = Path(args.out)
    work_root.mkdir(parents=True, exist_ok=True)

    rows = [run_preset(p, work_root, args.seed) for p in presets]

    cols = ["preset", "registered", "scale_err_pct", "photo_vol_err_pct",
            "ortho_vol_err_pct", "cloud_rms_m", "runtime_s", "peak_rss_mb"]
    header = ["preset", "registered", "scale err %", "photo vol err %",
              "ortho vol err %", "cloud RMS (m)", "runtime (s)", "peak RSS (MB)"]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * len(header)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(c, "n/a")) for c in cols) + " |")
    table = "\n".join(lines)
    print(table)
    if args.md_out:
        Path(args.md_out).write_text(table + "\n")


if __name__ == "__main__":
    main()

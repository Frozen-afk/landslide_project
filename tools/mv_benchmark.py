"""Multi-view region-marking validation (MULTIVIEW_REGION_PROGRESS.md §5).

For each cached synthetic benchmark scene (tools/synth.py presets under
data/bench/, as left by tools/benchmark.py) this writes synthetic
ground-truth masks — the true region ring projected into EVERY photo with
the ground-truth cameras — and scores 1-view (legacy) versus 2/4/8/16-outline
fusion under clean masks, boundary noise, one bad mask, an occluder, and
missing POV groups.

Usage:  .venv/bin/python -m tools.mv_benchmark --presets twosided_cal,arc
        --no-volume   region metrics only (fast; used for calibration)
        --only "clean x8,legacy"   run only the scenarios whose name matches

Never runs SfM or stereo: a preset without a cached reconstruction + dense
cloud is skipped.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.benchmark import _umeyama_pose_aware  # noqa: E402
from tools.synth import (BOWL, DEFAULT_TERRAIN_AMPS, POLY_R, PRESETS,  # noqa: E402
                         build_terrain, project, terrain_height)

QUIET = lambda *_: None  # noqa: E731


def gt_ring_world(preset: str, seed: int = 7, n: int = 72) -> np.ndarray:
    p = PRESETS[preset]
    X, Y, z, _, _ = build_terrain(seed, tex_scale=p["tex_scale"],
                                  octave_amps=p.get("terrain_amps", DEFAULT_TERRAIN_AMPS))
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    xy = np.column_stack([BOWL["x"] + POLY_R * np.cos(ang), BOWL["y"] + POLY_R * np.sin(ang)])
    return np.column_stack([xy, [terrain_height(X, Y, z, x, y) for x, y in xy]])


def gt_masks(data_dir: Path, gt: dict, seed: int = 7) -> dict:
    """Ground-truth outline of the region in every photo (stored-photo px),
    written to <scene>/gt_masks.json. A photo where the ring leaves the
    frame gets no mask."""
    ring = gt_ring_world(gt["preset"], seed)
    K, (w, h) = np.array(gt["K"]), gt["image_size"]
    masks = {}
    for name, po in gt["poses"].items():
        px, depth = project(ring, np.array(po["R"]), np.array(po["t"]), K, k1=gt["k1"])
        if (depth > 0).all() and px[:, 0].min() >= 0 and px[:, 0].max() < w \
                and px[:, 1].min() >= 0 and px[:, 1].max() < h:
            masks[name] = np.round(px, 2).tolist()
    (data_dir / "gt_masks.json").write_text(json.dumps(
        {"preset": gt["preset"], "ring": "circle r=polygon_radius_m about bowl, on terrain",
         "masks": masks}))
    return masks


def load_scene(preset: str, root: Path, seed: int = 7):
    from landslide.densify import load_cached_dense
    from landslide.geometry import camera_center
    from landslide.scaling import aruco_scale
    from landslide.sfm import reconstruct

    d = root / preset
    gt = json.loads((d / "ground_truth.json").read_text())
    if not (d / "work" / "database.db").exists():
        raise FileNotFoundError("no cached reconstruction")
    ctx = reconstruct(d / "images", d / "work", reuse=True, log=QUIET)
    aruco_scale(ctx, side_m=gt["marker"]["side"], dict_name="auto", log=QUIET)
    ctx.dense = load_cached_dense(ctx, log=QUIET)
    if ctx.dense is None:
        raise FileNotFoundError("no cached dense cloud")
    names = sorted(ctx.views)
    P = np.array([camera_center(ctx.views[n].R, ctx.views[n].t) for n in names])
    Q = np.array([gt["poses"][n]["eye"] for n in names])
    fwd_p = np.array([ctx.views[n].R[2, :] for n in names])
    fwd_q = np.array([np.asarray(gt["poses"][n]["R"])[2, :] for n in names])
    s, R, t = _umeyama_pose_aware(P, Q, P + fwd_p, Q + fwd_q)   # world = s R p + t
    return d, gt, ctx, (s, R, t)


def _noisy(poly, sigma, rng):
    return (np.asarray(poly) + rng.normal(0, sigma, (len(poly), 2))).tolist()


def _occluder(ctx, to_world, masks, group_images, all_images, gt):
    """A wall of points between the cameras of `group_images` and the region,
    plus every outline in `all_images` clipped to what the wall leaves
    visible in that photo (a person outlines only what they can see).
    Returns (extra model points, {image: clipped polygon})."""
    s, R, t = to_world
    centre = np.array([BOWL["x"], BOWL["y"]])
    eye = np.mean([gt["poses"][n]["eye"] for n in group_images], axis=0)
    d = (eye[:2] - centre) / np.linalg.norm(eye[:2] - centre)
    perp = np.array([-d[1], d[0]])
    ring = gt_ring_world(gt["preset"])
    z0 = float(np.median(ring[:, 2]))
    a, b = np.meshgrid(np.linspace(-7, 7, 280), np.linspace(-0.5, 3.0, 70))
    wall_w = np.column_stack([(centre + 10.0 * d)[None, :] + a.ravel()[:, None] * perp[None, :],
                              z0 + b.ravel()])
    wall_model = ((wall_w - t) @ R) / s
    K, (w, h) = np.array(gt["K"]), gt["image_size"]
    clipped = {}
    for n in all_images:
        po = gt["poses"][n]
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(np.asarray(masks[n])).astype(np.int32)], 1)
        px, _ = project(wall_w, np.array(po["R"]), np.array(po["t"]), K, k1=gt["k1"])
        hull = cv2.convexHull(np.round(px).astype(np.int32))
        cv2.fillPoly(m, [hull], 0)
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cs:
            c = max(cs, key=cv2.contourArea).reshape(-1, 2)
            if len(c) >= 3:
                clipped[n] = c.astype(float).tolist()
    return wall_model, clipped


def region_metrics(ctx, to_world, sel_mask, polygon_ground, e1, e2, gt) -> dict:
    """3D precision/recall of a point selection and boundary error of its
    ground polygon, against the true circle."""
    from landslide.geometry import ring_distance

    s, R, t = to_world
    pts = np.asarray(ctx.dense["points"], np.float64)
    world = (s * (R @ pts.T)).T + t
    truth = np.hypot(world[:, 0] - BOWL["x"], world[:, 1] - BOWL["y"]) < POLY_R
    tp = int((sel_mask & truth).sum())
    n_sel, n_true = int(sel_mask.sum()), int(truth.sum())
    out = {"precision": tp / max(n_sel, 1), "recall": tp / max(n_true, 1),
           "background_inclusion": (n_sel - tp) / max(n_sel, 1),
           "true_point_loss": (n_true - tp) / max(n_true, 1)}
    if polygon_ground is not None and len(polygon_ground) >= 3:
        ring_model = ((gt_ring_world(gt["preset"], n=360) - t) @ R) / s
        ring_g = np.column_stack([ring_model @ e1, ring_model @ e2]) * ctx.scale
        d1 = ring_distance(np.asarray(polygon_ground), ring_g)   # selection -> truth
        d2 = ring_distance(ring_g, np.asarray(polygon_ground))   # truth -> selection
        out["boundary_err_m"] = float(0.5 * (d1.mean() + d2.mean()))
        out["boundary_max_m"] = float(max(d1.max(), d2.max()))
    return out


def run_scenario(ctx, to_world, gt, label, anns, volume, legacy=False) -> dict:
    from landslide.densify import estimate_up
    from landslide.ground import select_region_ground
    from landslide.multiview import select_region_multiview
    from landslide.ortho import ground_basis
    from landslide.pipeline import measure

    truth = gt["volume_true_polygon_m3"]
    row = {"scenario": label, "n_outlines": len(anns)}
    t0 = time.time()
    try:
        up = estimate_up(ctx.views, ctx.sparse)
        e1, e2 = ground_basis(up)
        if legacy:
            g = select_region_ground(ctx, anns[0]["image"], anns[0]["polygon"], up=up, log=QUIET)
            if g is None:
                raise RuntimeError("ray-cast fallback (image-plane)")
            sel_mask, poly = g[0], np.asarray(g[2]["ground_polygon"])
        else:
            mv = select_region_multiview(ctx, anns, up=up, log=QUIET)
            sel_mask, poly = mv["interior"]["best"], mv["polygons"]["best"]
            st = mv["fusion"]["stats"]
            row.update(n_groups=st["n_groups"], n_used=st["n_used"],
                       req_met=st["requirement"]["met"],
                       uncertain_frac=st["uncertain_area_frac"],
                       weak_frac=st["weak_support_frac"],
                       min_iou=min((a["iou"] for a in st["annotations"]
                                    if a["iou"] is not None), default=None),
                       rejected=[a["image"] for a in st["annotations"] if a["rejected"]],
                       components=st["components"])
        row["select_s"] = time.time() - t0
        row.update(region_metrics(ctx, to_world, sel_mask, poly, e1, e2, gt))
        if volume:
            t1 = time.time()
            if legacy:
                res = measure(ctx, anns[0]["image"], anns[0]["polygon"], log=QUIET)
            else:
                res = measure(ctx, None, None, annotations=anns, log=QUIET)
            row["measure_s"] = time.time() - t1
            cut = res["cut_volume_m3"]
            row.update(status=res["status"], reason_ids=res["reason_ids"],
                       confidence=res.get("confidence"), cut_m3=cut,
                       vol_err_pct=abs(cut - truth) / truth * 100,
                       coverage_frac=res.get("coverage_frac"),
                       err_covers_truth=abs(cut - truth) <= res["est_volume_error_m3"])
            # the pipeline's own honest range (RC1/A1): measured cut .. cut if
            # the unmeasured part of the footprint were as deep as the deepest
            # measured point. Region bounds below only widen it.
            lo, hi = cut, res.get("cut_upper_m3") or cut
            b = res.get("volume_bounds_m3")
            if b and b["lower"] and b["upper"]:
                row.update(cut_lower_m3=b["lower"]["cut_volume_m3"],
                           cut_upper_m3=b["upper"]["cut_volume_m3"])
                row["bounds_cover_truth"] = row["cut_lower_m3"] <= truth <= row["cut_upper_m3"]
                lo, hi = min(lo, row["cut_lower_m3"]), max(hi, row["cut_upper_m3"])
            row.update(range_lo_m3=lo, range_hi_m3=hi, range_covers_truth=lo <= truth <= hi)
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    row["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return row


def run_preset(preset: str, root: Path, volume: bool, seed: int = 7,
               only: list[str] | None = None) -> list[dict]:
    from landslide import multiview as mvm

    d, gt, ctx, to_world = load_scene(preset, root, seed)
    masks = gt_masks(d, gt, seed)
    masks = {n: m for n, m in masks.items() if n in ctx.views}
    pov = mvm.pov_groups(ctx)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    order = [s["image"] for s in mvm.suggest_images(ctx, pov, {}) if s["image"] in masks]
    rng = np.random.default_rng(0)

    def anns(names, edit=None):
        return [{"image": n, "polygon": (edit or {}).get(n, masks[n])} for n in names]

    rows = []

    def run(label, a, vol=volume, legacy=False):
        if only is None or any(o in label for o in only):
            rows.append(run_scenario(ctx, to_world, gt, label, a, vol, legacy=legacy))

    legacy_img = gt["polygon_image"]
    run("legacy 1-view (ray-cast)", [{"image": legacy_img, "polygon": gt["polygon_px"]}],
        legacy=True)
    for n in (1, 2, 4, 8, 16):
        if len(order) >= n:
            run(f"clean x{n}", anns(order[:n]))
    base = order[:8]
    if len(base) == 8:
        for sigma in (3.0, 8.0):
            run(f"boundary noise σ={sigma:g}px x8",
                anns(base, {n: _noisy(masks[n], sigma, rng) for n in base}))
        w = gt["image_size"][0]
        bad = (np.asarray(masks[base[2]]) * 1.3 + [0.22 * w, -40.0]).tolist()
        run("one bad mask x8", anns(base, {base[2]: bad}))
        if len(order) >= 9:
            b9 = order[8]
            bad9 = (np.asarray(masks[b9]) * 1.3 + [0.22 * w, -40.0]).tolist()
            run("one bad mask x9 (8 good + 1)", anns(base + [b9], {b9: bad9}))
        # occluder in front of the first two POV groups used
        g_used = []
        for n in base:
            g = pov["views"][n]["group"]
            if g not in g_used:
                g_used.append(g)
        occ_imgs = [n for n in base if pov["views"][n]["group"] in g_used[:2]]
        wall, clipped = _occluder(ctx, to_world, masks, occ_imgs, base, gt)
        keep = ctx.dense
        ctx.dense = {"points": np.vstack([keep["points"], wall.astype(np.float32)]),
                     "colors": np.vstack([keep["colors"],
                                          np.full((len(wall), 3), 128, np.uint8)])}
        run("occluder in front of 2 groups x8", anns(base, clipped), vol=False)
        ctx.dense = keep
    # missing POV groups: every mask of the two best-populated adjacent groups
    gids = sorted(pov["groups"])
    pair = max(zip(gids, gids[1:]),
               key=lambda p: sum(len([n for n in pov["groups"][g]["images"] if n in masks])
                                 for g in p), default=None)
    if pair:
        names = [n for g in pair for n in pov["groups"][g]["images"] if n in masks]
        run(f"missing POV groups (2 groups) x{len(names)}", anns(names))
    for r in rows:
        r["preset"] = preset
        r["baseline_rss_mb"] = rss0
        r["n_dense"] = int(len(ctx.dense["points"]))
        r["n_pov_groups"] = len(pov["groups"])
    return rows


COLS = [("preset", "preset"), ("scenario", "scenario"), ("n_groups", "groups"),
        ("n_used", "used"), ("req_met", "4×2"), ("precision", "prec"), ("recall", "recall"),
        ("background_inclusion", "bg incl"), ("true_point_loss", "pt loss"),
        ("boundary_err_m", "bnd err m"), ("uncertain_frac", "unc frac"),
        ("min_iou", "min IoU"), ("status", "status"), ("vol_err_pct", "vol err %"),
        ("cut_lower_m3", "cut lo"), ("cut_m3", "cut"), ("cut_upper_m3", "cut up"),
        ("bounds_cover_truth", "region bounds∋truth"), ("range_hi_m3", "G6 upper"),
        ("range_covers_truth", "range∋truth"), ("err_covers_truth", "±err∋truth"),
        ("select_s", "select s"), ("measure_s", "measure s"), ("peak_rss_mb", "peak RSS MB")]


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--presets", default="twosided_cal,twosided_b,arc,sparse8")
    ap.add_argument("--out", default=str(ROOT / "data" / "bench"))
    ap.add_argument("--no-volume", action="store_true")
    ap.add_argument("--only", default=None,
                    help="comma-separated substrings; run only matching scenarios")
    ap.add_argument("--md-out", default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    rows = []
    for p in args.presets.split(","):
        try:
            rows += run_preset(p, Path(args.out), not args.no_volume,
                               only=args.only.split(",") if args.only else None)
        except FileNotFoundError as e:
            print(f"skipping {p}: {e}", file=sys.stderr)
    lines = ["| " + " | ".join(h for _, h in COLS) + " |",
             "| " + " | ".join("---" for _ in COLS) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(k)) for k, _ in COLS) + " |")
        if r.get("error"):
            lines.append(f"| | ↳ error: {r['error']} |" + " |" * (len(COLS) - 2))
        if r.get("reason_ids") or r.get("rejected"):
            lines.append(f"| | ↳ reasons: {', '.join(r.get('reason_ids') or []) or '—'}"
                         f"; rejected: {', '.join(r.get('rejected') or []) or '—'} |"
                         + " |" * (len(COLS) - 2))
    table = "\n".join(lines)
    print(table)
    if args.md_out:
        Path(args.md_out).write_text(table + "\n")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=1, default=str))


if __name__ == "__main__":
    main()

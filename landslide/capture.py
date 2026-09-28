"""Server-side per-photo (T1) and per-sequence (T2) guided-capture checks
(GUIDED_CAPTURE_PLAN.md S2, §4.1/§4.2). No web dependency — unit-testable
in isolation from server/.

Reuses `pipeline._photo_metrics` (blur/exposure), `scaling.detect_marker_corners`
/ `_load_gray` / `_available_dicts` (marker) instead of duplicating their
thresholds. T1_MESSAGES is the §4.1 "instruction on fail" text — a different
vocabulary from `gates.RULES` (the *result-side* G-gate ids, §7/§7a), which
this module does not touch: a per-photo check fires long before any gate
runs, so it has no gate id to report yet.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .densify import estimate_up, select_pairs
from .gates import DATUM_HOLDOUT_REL, DATUM_MIN_WINDOWS, RULES
from .geometry import points_in_polygon, polygon_area, ring_distance
from .ortho import ground_basis, select_region_world
from .pipeline import _photo_metrics
from .scaling import _available_dicts, _load_gray, aruco_scale, detect_marker_corners
from .sfm import ReconCtx, _camera_center_collinearity, _focal_spread
from .volume import fit_plane_robust, prism_volume

# §4.1 thresholds
FEATURES_FAIL = 300
FEATURES_WARN = 800
OVERLAP_FAIL = 40
OVERLAP_WARN = 100
PARALLAX_H_RATIO_WARN = 0.95
MARKER_SIDE_OK_PX = 60
MARKER_SIDE_WARN_PX = 30
MARKER_RATIO_WARN = 0.6
MARKER_RATIO_FAIL = 0.4
MARKER_EDGE_MARGIN_PX = 10
BLUR_ABS_FLOOR = 6.0          # landslide/pipeline.py::_cull's own floor
EXPOSURE_FAIL = 0.5
EXPOSURE_WARN = 0.10

T1_MESSAGES = {
    "blur": "Blurry — hold still, tap to focus on the slope, retake.",
    "exposure": "Sky or shadow is blowing out the frame — tilt down or "
                "step so the sun is behind you.",
    "features": "Too little texture in frame (water, snow, sky) — include "
                "more of the slope and its edges.",
    "overlap": "Too far from the last photo — step back half the distance "
               "and retake.",
    "parallax": "You turned without moving. Walk sideways along the arc "
                "between shots.",
    "marker_small": "Marker too small — move closer or use a bigger print.",
    "marker_edge_on": "Marker seen edge-on — turn the board toward you.",
    "marker_clipped": "Marker cut off at the frame edge.",
    "arm_bridge": "Arm B isn't linking to arm A — take 3-5 photos while "
                  "walking back toward your first position.",
}

_SIFT = None


def _sift():
    global _SIFT
    if _SIFT is None:
        _SIFT = cv2.SIFT_create(4000)
    return _SIFT


def _load_1000(path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise IOError(f"cannot read {path}")
    s = min(1.0, 1000 / max(img.shape))
    if s < 1.0:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img


def extract_feats(path, cache: Path | None = None):
    """SIFT keypoints/descriptors at 1000 px, optionally cached to `cache`
    (an .npz, `capture/feats/<uuid>.npz` — §9.1) so a later photo's overlap
    check against this one doesn't re-decode and re-detect it."""
    if cache is not None and Path(cache).exists():
        d = np.load(cache, allow_pickle=False)
        kp = [cv2.KeyPoint(float(x), float(y), float(sz)) for x, y, sz in d["kp"]]
        desc = d["desc"] if d["desc"].size else None
        return kp, desc
    gray = _load_1000(path)
    kp, desc = _sift().detectAndCompute(gray, None)
    if cache is not None:
        cache = Path(cache)
        cache.parent.mkdir(parents=True, exist_ok=True)
        kp_arr = np.array([(k.pt[0], k.pt[1], k.size) for k in kp], dtype=np.float64) \
            if kp else np.zeros((0, 3))
        np.savez(cache, kp=kp_arr, desc=desc if desc is not None else np.zeros((0, 0)))
    return kp, desc


def _match_pair(kp1, desc1, kp2, desc2) -> dict:
    """Ratio-test SIFT matches, then fundamental- and homography-inlier
    counts against them (§4.1 overlap / parallax)."""
    if desc1 is None or desc2 is None or len(kp1) < 8 or len(kp2) < 8:
        return {"matches": 0, "f_inliers": 0, "h_inliers": 0, "h_ratio": 0.0}
    bf = cv2.BFMatcher(cv2.NORM_L2)
    raw = bf.knnMatch(desc1, desc2, k=2)
    good = [m for m, n in raw if n is not None and m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return {"matches": len(good), "f_inliers": 0, "h_inliers": 0, "h_ratio": 0.0}
    pts1 = np.float32([kp1[m.queryIdx].pt for m in good])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in good])
    _, f_mask = cv2.findFundamentalMat(pts1, pts2, cv2.FM_RANSAC, 3.0, 0.99)
    f_inliers = int(f_mask.sum()) if f_mask is not None else 0
    _, h_mask = cv2.findHomography(pts1, pts2, cv2.RANSAC, 3.0)
    h_inliers = int(h_mask.sum()) if h_mask is not None else 0
    h_ratio = h_inliers / f_inliers if f_inliers else 0.0
    return {"matches": len(good), "f_inliers": f_inliers, "h_inliers": h_inliers,
            "h_ratio": float(h_ratio)}


def _marker_check(path, marker_side_m: float | None, marker_dict: str = "auto") -> dict | None:
    """§4.1 marker present/size/angle. None when no marker is found at all
    (not itself a fail — the sequence-level marker-view count is what the
    §3 step-6 rule checks)."""
    if not marker_side_m:
        return None
    img, gray, s = _load_gray(path)
    full_h, full_w = round(img.shape[0] / s), round(img.shape[1] / s)
    for dname in _available_dicts(marker_dict):
        found = detect_marker_corners(gray, s, dname, path=path)
        if not found:
            continue
        mid, corners = next(iter(found.items()))
        sides = np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1)
        side_px = float(np.mean(sides))
        ratio = float(sides.min() / sides.max()) if sides.max() > 0 else 0.0
        clipped = bool(corners[:, 0].min() < MARKER_EDGE_MARGIN_PX or
                       corners[:, 1].min() < MARKER_EDGE_MARGIN_PX or
                       corners[:, 0].max() > full_w - MARKER_EDGE_MARGIN_PX or
                       corners[:, 1].max() > full_h - MARKER_EDGE_MARGIN_PX)
        size_v = ("ok" if side_px >= MARKER_SIDE_OK_PX else
                  "warn" if side_px >= MARKER_SIDE_WARN_PX else "fail")
        angle_v = ("ok" if ratio >= MARKER_RATIO_WARN else
                   "warn" if ratio >= MARKER_RATIO_FAIL else "fail")
        verdict = "fail" if "fail" in (size_v, angle_v) else \
            "warn" if clipped or "warn" in (size_v, angle_v) else "ok"
        # priority: a clipped marker is the most actionable fix, then angle,
        # then size — only one reason is shown at a time (§4.1's chip is a
        # single line)
        reason = None
        if size_v != "ok":
            reason = "marker_small"
        if angle_v != "ok":
            reason = "marker_edge_on"
        if clipped:
            reason = "marker_clipped"
        return {"id": int(mid), "side_px": side_px, "side_ratio": ratio,
                "clipped": clipped, "verdict": verdict, "reason": reason}
    return None


def image_checks(path, prev_path=None, marker_side_m: float | None = None,
                  marker_dict: str = "auto", blur_floor: float = BLUR_ABS_FLOOR,
                  blur_median: float | None = None, arm_bridge_paths=None,
                  prev_pivot: bool = False, feats_cache: Path | None = None,
                  prev_feats_cache: Path | None = None) -> dict:
    """T1 per-photo server checks (§4.1).

    `prev_path`/`prev_feats_cache`: the previous *kept* photo in the same
    arm, for the overlap and parallax(pivot) checks.
    `prev_pivot`: whether that previous photo itself fired a parallax
    warning — two in a row escalates this one to fail (§4.1).
    `arm_bridge_paths`: up to 3 tail photos of the other arm, for the
    arm-bridge overlap check (§4.1's "first 3 photos of arm B / transition").
    `blur_median`: the session/batch median Laplacian variance once >= 5
    photos exist (§4.1); None uses the flat floor only, matching S1's own
    "before 5 photos exist" reading.
    """
    from PIL import Image
    with Image.open(path) as im:
        lap, clip, _dhash = _photo_metrics(im)

    reasons: list[str] = []
    verdict = "ok"

    def bump(v: str) -> None:
        nonlocal verdict
        if v == "fail" or (v == "warn" and verdict == "ok"):
            verdict = v

    blur_cut = max(blur_floor, 0.25 * blur_median) if blur_median else blur_floor
    if lap < blur_cut:
        reasons.append("blur")
        bump("fail")
    elif blur_median and lap < 0.5 * blur_median:
        bump("warn")   # advisory only — no §4.1 reason id for the warn band

    if clip >= EXPOSURE_FAIL:
        reasons.append("exposure")
        bump("fail")
    elif clip >= EXPOSURE_WARN:
        bump("warn")

    kp, desc = extract_feats(path, cache=feats_cache)
    n_feat = len(kp)
    if n_feat < FEATURES_FAIL:
        reasons.append("features")
        bump("fail")
    elif n_feat < FEATURES_WARN:
        bump("warn")

    overlap = None
    parallax = None
    if prev_path is not None:
        pkp, pdesc = extract_feats(prev_path, cache=prev_feats_cache)
        overlap = _match_pair(pkp, pdesc, kp, desc)
        if overlap["f_inliers"] < OVERLAP_FAIL:
            reasons.append("overlap")
            bump("fail")
        elif overlap["f_inliers"] < OVERLAP_WARN:
            bump("warn")
        # pivot detection only makes sense once overlap itself is real —
        # an already-failed overlap pair has too few inliers for h_ratio
        # to mean anything (§4.1: "Warn if >= 0.95 ... Fail on two in a row")
        if overlap["f_inliers"] >= OVERLAP_FAIL:
            is_pivot = overlap["h_ratio"] >= PARALLAX_H_RATIO_WARN
            parallax = is_pivot
            if is_pivot and prev_pivot:
                reasons.append("parallax")
                bump("fail")
            elif is_pivot:
                bump("warn")   # first pivot warns; no reason id until the 2nd

    marker = _marker_check(path, marker_side_m, marker_dict)
    if marker is not None and marker["reason"]:
        reasons.append(marker["reason"])
        bump(marker["verdict"])

    bridge = None
    if arm_bridge_paths:
        best = max((_match_pair(*extract_feats(p), kp, desc)["f_inliers"]
                    for p in arm_bridge_paths), default=0)
        bridge = {"f_inliers": best}
        if best < OVERLAP_WARN:
            reasons.append("arm_bridge")
            bump("warn")

    return {
        "verdict": verdict,
        "reasons": [{"id": r, "text": T1_MESSAGES[r]} for r in reasons],
        "server": {"lap": lap, "clip": clip, "n_feat": n_feat,
                   "overlap_prev": overlap, "parallax_pivot": parallax,
                   "marker": marker, "arm_bridge": bridge},
    }


def sequence_checks(photos: list[dict]) -> dict:
    """T2 sequence-level checks over the manifest's photo list so far
    (§4.2's server-derivable subset — heading/elevation/arm-separation need
    client sensor data from S3 and are not computed here)."""
    kept = [p for p in photos if not p.get("excluded")]
    counts: dict[str, int] = {}
    marker_views: dict[str, int] = {}
    overlap_gaps: list[str] = []
    for p in kept:
        arm = p.get("arm", "?")
        counts[arm] = counts.get(arm, 0) + 1
        server = p.get("server") or {}
        marker = server.get("marker")
        if marker and marker.get("verdict") in ("ok", "warn"):
            marker_views[arm] = marker_views.get(arm, 0) + 1
        overlap = server.get("overlap_prev")
        if overlap is not None and overlap.get("f_inliers", 0) < OVERLAP_FAIL:
            overlap_gaps.append(p["uuid"])
    return {
        "counts": counts,
        "total": len(kept),
        "marker_views": marker_views,
        "marker_views_total": sum(marker_views.values()),
        "overlap_gaps": overlap_gaps,
    }


# ---------------------------------------------------------------------------
# S4 (GUIDED_CAPTURE_PLAN.md §4.3, §9.2, §12 stage table): T3 preview-
# reconstruction checks, run once on a cheap preview SfM attempt
# (`worker.run_preview`: `sfm.reconstruct(..., max_image_size=1000,
# max_attempts=1)`), never on the job's real reconstruction.
# ---------------------------------------------------------------------------

MIN_ELEVATION_DEG = 20.0          # §3 step 3 / §4.3
MIN_ARM_ELEVATION_DEG = 15.0
MIN_ARM_SEPARATION_DEG = 60.0     # §4.3 azimuth coverage
STEREO_PAIR_SHARE_WARN = 0.70     # §4.3 stereo pairs
REGISTRATION_FRAC_BLOCK = 0.90    # §4.3 registration and connectivity
DATUM_RIM_INNER_M = 0.5           # §3a.4 band
DATUM_RIM_OUTER_M = 2.5

# S5C (GUIDED_CAPTURE_PLAN.md §12c): coverage/ROI guidance constants.
CELL_M = 1.0                  # §12c.4 observation-map cell
THETA_G = 18.0                 # §12a.3's fitted two-arm good-cell grazing floor
RIM_BAND_M = 2.5               # §12c.4 band width around the lifted ROI
BAND_WINDOW_MIN_FRAC = 0.5     # §12c.4 band_windows_seen
GAP_MIN_M2 = 2.0               # §12c.6 (the G6 void limit)
GAP_MERGE_DEG = 15.0           # §12c.6 merge threshold
# S5C validation (GUIDED_CAPTURE_PROGRESS.md "S5C", the twosided_b/twosided_cal
# false-instruction finding): a gap at or near the GAP_MIN_M2 floor (1-3
# cells) whose cell-normal fit reports a near-vertical slope is a fit
# artifact, not a real landslide wall — reproduced directly on twosided_b's
# own cheap preview (a 2-cell gap, slope_deg 87.2, which without this guard
# sent `need_elev` past 90 deg and `stand_m` to ~9.8e6 m). This does not
# touch GAP_MIN_M2, THETA_G, or any calibrated cut point: it only withholds
# an instruction for a gap whose own geometry is too small and too steep to
# be a trustworthy signal, the same evidence-backed shape as the reproduced
# false extend_arm/higher instruction on this clean capture.
DEGENERATE_GAP_AREA_M2 = 1.5 * GAP_MIN_M2
DEGENERATE_GAP_SLOPE_DEG = 60.0
MAX_INSTRUCTIONS = 3
ARM_STEP_DEG = 7.0             # §12a.4's field step
OUTER_END_EXTRA = 2            # §3 step 3's 2 outer-end photos

# §12c.5/§12c.7.3 calibration freeze (GUIDED_CAPTURE_PROGRESS.md "S5C",
# calibration run on the 9 scale-able presets, seed 7). 9/9 matches (>= the
# 8/9 criterion-4 floor), 0 optimistic predictions, 0 false blocks.
C_REJ = 0.740             # feasible interval (0.7143, 0.7685], centre 0.741,
                          # rounded to the 0.005 search grid. NOT margin-safe
                          # (interval width 0.054 < the required 0.055) ->
                          # G6_rejected is a warn, never a block (see below).
C_OK = 0.890              # feasible-with-margin interval [0.8658, 0.9170]
COVERAGE_CALIBRATION = "s5c-v1"
G6_REJECTED_BLOCKS = False
DATUM_ESTIMATE_CALIBRATED = True   # §12c.7.3 recalibration passed with the
                                   # real ROI: ratio unstable (>0.05) on
                                   # twosided (0.076) and twosided_b (0.179),
                                   # silent (<=0.05) on twosided_cal (0.014)

# Preview-only ids with no final-gate counterpart (§4.3's "SfM < 50% hard
# fail" and "separation < 60deg" rows fire before/outside `evaluate_gates`,
# so `gates.RULES` has no id for them). Every other predicted check reuses
# the final-result id/text from `gates.RULES` verbatim (§7a.2: "the same ids
# are used with 'predicted' in front of the first line").
T3_MESSAGES = {
    "T3_registration": "Photos don't connect into one model yet. Retake the "
                       "gap with smaller steps (more overlap) between shots.",
    "T3_azimuth": "Walk to a second vantage point at least 60° around the "
                 "slide from the first, keeping 60-80% overlap.",
    "T3_marker_scale": "Marker scale could not be computed ({err}). Move "
                       "closer, flatten the board, and include it in more "
                       "photos, spread across both arms.",
    "T3_datum_unstable": "Early check: the ground around the slide looks "
                         "uneven. The result may be indicative only. This "
                         "does not depend on your photos — keep capturing.",
}


def _circular_mean_deg(degs) -> float:
    rad = np.radians(np.asarray(degs, np.float64))
    return float(np.degrees(np.arctan2(np.mean(np.sin(rad)), np.mean(np.cos(rad))))) % 360.0


def _circular_diff_deg(a: float, b: float) -> float:
    return abs((b - a + 180.0) % 360.0 - 180.0)


def _central_half_mask(uv: np.ndarray) -> np.ndarray:
    """Boolean mask of the central 50% of 2D points by distance to their
    own median."""
    center = np.median(uv, axis=0)
    d = np.linalg.norm(uv - center, axis=1)
    return d <= np.median(d)


def _default_roi_polygon(uv: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """§5.1 fallback ROI, used whenever no ROI tap exists or the tap can't be
    lifted (§12c.3): the convex hull of the central 50% of the sparse
    cloud's ground-plane projection."""
    from scipy.spatial import ConvexHull

    central = uv[mask if mask is not None else _central_half_mask(uv)]
    if len(central) < 3:
        return central
    hull = ConvexHull(central)
    return central[hull.vertices]


# ---------------------------------------------------------------------------
# S5C (GUIDED_CAPTURE_PLAN.md §12c): ROI lift, observation map, and
# directional recapture instructions.
# ---------------------------------------------------------------------------

def _lift_roi(ctx: ReconCtx, up: np.ndarray, e1: np.ndarray, e2: np.ndarray,
             roi: dict | None, capture_dir, log=print) -> dict:
    """§12c.3: lift a tapped outline (in original-photo px) into ground
    coordinates. Returns {"source": "tap"|"tap_hull"|"default", "note",
    "ground_polygon": (M,2) metric array or None, "hit_frac": float|None}.
    Needs `ctx.scale` already set by a successful marker check."""
    if not roi or not roi.get("photo_uuid") or not roi.get("polygon_px"):
        return {"source": "default", "note": None, "ground_polygon": None, "hit_frac": None}

    photo_uuid = roi["photo_uuid"]
    # a capture-session photo is registered as "{seq:03d}_{uuid}.jpg" (§9.1);
    # an exact match is also accepted so a caller can name a registered view
    # directly (the §12c.5 calibration script stands in the ground-truth
    # photo name for a user's tap, with no capture session behind it).
    name = photo_uuid if photo_uuid in ctx.views else next(
        (n for n in ctx.views if n.endswith(f"_{photo_uuid}.jpg")), None)
    if name is None:
        return {"source": "default", "note": "roi_photo_unregistered",
                "ground_polygon": None, "hit_frac": None}

    view = ctx.views[name]
    polygon_px = np.asarray(roi["polygon_px"], np.float64)
    if capture_dir is not None:
        orig_path = Path(capture_dir) / "originals" / f"{photo_uuid}.jpg"
        if orig_path.exists():
            try:
                from PIL import Image
                with Image.open(orig_path) as im:
                    orig_w, _orig_h = im.size
                if orig_w and orig_w != view.width:
                    polygon_px = polygon_px * (view.width / orig_w)
            except Exception as e:
                log(f"[capture] roi photo size read failed: {e}")

    from .ground import select_region_ground
    try:
        result = select_region_ground(ctx, name, polygon_px, up=up, log=log, dense=False)
    except Exception as e:
        log(f"[capture] roi ray-cast failed: {e}")
        result = None
    if result is not None:
        _interior, _rim, info = result
        ground = np.asarray(info["ground_polygon"], np.float64)
        if len(ground) >= 3:
            return {"source": "tap", "note": None, "ground_polygon": ground,
                    "hit_frac": info["hit_frac"]}

    # §12c.3.4 fallback: project the sparse cloud into the view, keep points
    # in front of the camera and inside the tapped polygon, take their hull.
    uv_px, depth = view.project(ctx.sparse)
    inside = points_in_polygon(uv_px, polygon_px) & (depth > 0)
    pts_in = ctx.sparse[inside] * ctx.scale
    if len(pts_in) < 3:
        return {"source": "default", "note": "roi_no_points",
                "ground_polygon": None, "hit_frac": None}
    from scipy.spatial import ConvexHull
    uv_in = np.column_stack([pts_in @ e1, pts_in @ e2])
    try:
        hull = ConvexHull(uv_in)
        ground = uv_in[hull.vertices]
    except Exception:
        return {"source": "default", "note": "roi_no_points",
                "ground_polygon": None, "hit_frac": None}
    return {"source": "tap_hull", "note": None, "ground_polygon": ground, "hit_frac": None}


def coverage_map(ctx: ReconCtx, up: np.ndarray, e1: np.ndarray, e2: np.ndarray,
                 roi_ground: np.ndarray, arms: dict[str, str] | None = None,
                 cell_m: float = CELL_M, theta_g: float = THETA_G,
                 rim_band_m: float = RIM_BAND_M) -> dict:
    """§12c.4 observation map: per-cell good/weak/void class over the ROI
    plus its rim band, on the preview sparse cloud and its point tracks."""
    from scipy.ndimage import label

    roi_ground = np.asarray(roi_ground, np.float64)
    pts = ctx.sparse * ctx.scale
    uv = np.column_stack([pts @ e1, pts @ e2])

    lo = roi_ground.min(axis=0) - rim_band_m
    hi = roi_ground.max(axis=0) + rim_band_m
    nx = max(1, int(np.ceil((hi[0] - lo[0]) / cell_m)))
    ny = max(1, int(np.ceil((hi[1] - lo[1]) / cell_m)))
    xs = lo[0] + (np.arange(nx) + 0.5) * cell_m
    ys = lo[1] + (np.arange(ny) + 0.5) * cell_m
    Xc, Yc = np.meshgrid(xs, ys, indexing="xy")
    centers = np.column_stack([Xc.ravel(), Yc.ravel()])
    is_roi = points_in_polygon(centers, roi_ground).reshape(ny, nx)
    dcell = ring_distance(centers, roi_ground).reshape(ny, nx)
    is_band = (~is_roi) & (dcell <= rim_band_m)

    in_bounds = ((uv[:, 0] >= lo[0]) & (uv[:, 0] < lo[0] + nx * cell_m) &
                (uv[:, 1] >= lo[1]) & (uv[:, 1] < lo[1] + ny * cell_m))
    ix_all = np.clip(((uv[:, 0] - lo[0]) / cell_m).astype(np.int64), 0, nx - 1)
    iy_all = np.clip(((uv[:, 1] - lo[1]) / cell_m).astype(np.int64), 0, ny - 1)

    name_by_id = {v.image_id: n for n, v in ctx.views.items()}
    point_names: list[list[str]] = []
    try:
        points3d = list(ctx.rec.points3D.values())
    except AttributeError:
        points3d = None   # a test double `rec` with no track data: no arm can
                          # be confirmed for any cell, so nothing classifies "good"
    if points3d is None or len(points3d) != len(pts):
        point_names = [[] for _ in range(len(pts))]
    else:
        for p3 in points3d:
            names = []
            for el in p3.track.elements:
                iid = getattr(el, "image_id", None)
                if iid is None and isinstance(el, tuple):
                    iid = el[0]
                nm = name_by_id.get(int(iid)) if iid is not None else None
                if nm is not None:
                    names.append(nm)
            point_names.append(names)

    cam_center = {n: ctx.views[n].center * ctx.scale for n in ctx.views}

    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(len(pts)):
        if in_bounds[i]:
            buckets.setdefault((int(iy_all[i]), int(ix_all[i])), []).append(i)

    def _neighbor_idx(iy, ix):
        out = list(buckets.get((iy, ix), []))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    out += buckets.get((iy + dy, ix + dx), [])
        return out

    classes = np.full((ny, nx), ".", dtype="<U1")
    gap_mask = np.zeros((ny, nx), dtype=bool)
    cell_normals: dict[tuple[int, int], tuple[np.ndarray, np.ndarray | None]] = {}
    band_cell_uv: list[np.ndarray] = []
    band_cell_ij: list[tuple[int, int]] = []

    # Visit every ROI-or-band grid cell, not just occupied ones: an empty
    # cell is a real void/gap, and an empty BAND cell must also count
    # against `band_windows_seen` (otherwise a window with no data at all
    # looks "seen" just because it has no cells to disagree with — this is
    # what made an early cut of this function mark most of a half-populated
    # rim ring "seen").
    for iy, ix in zip(*np.nonzero(is_roi | is_band)):
        iy, ix = int(iy), int(ix)
        idx = buckets.get((iy, ix), [])
        n_points = len(idx)
        if n_points == 0:
            cls = "v"
        else:
            obs_names: set[str] = set()
            for i in idx:
                obs_names.update(point_names[i])
            centroid = pts[idx].mean(axis=0)
            normal = None
            fit_idx = idx if n_points >= 6 else _neighbor_idx(iy, ix)
            if len(fit_idx) >= 6:
                try:
                    c, n_, _basis, _keep = fit_plane_robust(pts[fit_idx])
                    nrm = float(np.linalg.norm(n_))
                    if nrm > 1e-9:
                        normal = n_ / nrm
                        centroid = c
                except Exception:
                    normal = None
            cell_normals[(iy, ix)] = (centroid, normal)

            by_arm: dict[str, set[str]] = {}
            for nm in obs_names:
                arm = (arms or {}).get(nm)
                if arm is None:
                    continue
                grazing = 90.0
                if normal is not None:
                    C = cam_center.get(nm)
                    if C is not None:
                        ray = C - centroid
                        d = float(np.linalg.norm(ray))
                        if d > 1e-9:
                            ang = np.degrees(np.arccos(
                                np.clip(abs(float((ray / d) @ normal)), 0.0, 1.0)))
                            grazing = max(0.0, 90.0 - ang)
                if grazing >= theta_g:
                    by_arm.setdefault(arm, set()).add(nm)
            cls = "g" if any(len(v) >= 3 for v in by_arm.values()) else "w"

        if is_roi[iy, ix]:
            classes[iy, ix] = cls
            if cls != "g":
                gap_mask[iy, ix] = True
        else:
            classes[iy, ix] = cls.upper()
            band_cell_uv.append(centers[iy * nx + ix])
            band_cell_ij.append((iy, ix))

    n_roi = int(is_roi.sum())
    n_good = int(np.sum((classes == "g") & is_roi))
    preview_coverage = (n_good / n_roi) if n_roi else 0.0

    lbl, n_lbl = label(gap_mask, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    largest_void_m2 = 0.0
    gaps: list[dict] = []
    roi_centroid_uv = roi_ground.mean(axis=0)
    if n_lbl:
        counts = np.bincount(lbl.ravel())
        largest_void_m2 = float(counts[1:].max()) * cell_m * cell_m
        for gi in range(1, n_lbl + 1):
            area_m2 = float(counts[gi]) * cell_m * cell_m
            if area_m2 < GAP_MIN_M2:
                continue
            mask = lbl == gi
            gcenters = centers.reshape(ny, nx, 2)[mask]
            gaps.append(_gap_info(mask, gcenters, cell_normals, roi_centroid_uv,
                                  e1, e2, up, cell_m))

    band_window_seen: list[bool] = []
    band_window_center: list[float] = []
    if band_cell_uv:
        band_arr = np.asarray(band_cell_uv)
        ang = np.degrees(np.arctan2(band_arr[:, 1] - roi_centroid_uv[1],
                                    band_arr[:, 0] - roi_centroid_uv[0])) % 360.0
        seen_or_weak = np.array([classes[iy, ix] in ("W", "G") for iy, ix in band_cell_ij])
        for k in range(8):
            win_center = (k * 45.0 + 45.0) % 360.0
            te = ((ang - k * 45.0) % 360.0) < 90.0
            total = int(te.sum())
            band_window_center.append(win_center)
            # `bool(total) and <numpy bool_>` short-circuits to the numpy
            # scalar itself, not a Python bool, when total is truthy — found
            # by S5C's criterion-11 real HTTP replay (job.save_capture's
            # json.dumps rejects the resulting numpy.int64 band_windows_seen,
            # never hit by the direct-Python-dict calibration script).
            seen = bool(total) and bool(seen_or_weak[te].sum() / total
                                        >= BAND_WINDOW_MIN_FRAC)
            band_window_seen.append(seen)
    band_windows_seen = int(sum(band_window_seen))

    grid_str = "\n".join("".join(str(classes[iy, ix]) for ix in range(nx))
                         for iy in range(ny))

    return {
        "roi_area_m2": float(polygon_area(roi_ground)) if len(roi_ground) >= 3 else 0.0,
        "roi_centroid_uv": roi_centroid_uv.tolist(),
        "cell_m": cell_m,
        "grid": {"u0": float(lo[0]), "v0": float(lo[1]), "cell": cell_m,
                "nx": nx, "ny": ny, "classes": grid_str},
        "preview_coverage": preview_coverage,
        "preview_void_m2": largest_void_m2,
        "band_windows_seen": band_windows_seen,
        "band_window_seen": band_window_seen,
        "band_window_center_deg": band_window_center,
        "gaps": gaps,
    }


def _gap_info(mask: np.ndarray, gcenters: np.ndarray, cell_normals: dict,
             roi_centroid_uv: np.ndarray, e1: np.ndarray, e2: np.ndarray,
             up: np.ndarray, cell_m: float) -> dict:
    """§12c.6 per-gap `face_az`/`slope_deg` from the cells' own robust
    plane normals, falling back to "opposite the gap's own direction from
    the ROI centroid" when no cell in the gap has one."""
    area_m2 = float(mask.sum()) * cell_m * cell_m
    centroid_uv = gcenters.mean(axis=0)
    normals3 = [cell_normals[(int(iy), int(ix))][1]
               for iy, ix in zip(*np.nonzero(mask))
               if cell_normals.get((int(iy), int(ix)), (None, None))[1] is not None]
    if normals3:
        arr = np.asarray(normals3)
        nu, nv = float((arr @ e1).mean()), float((arr @ e2).mean())
        face_az = np.degrees(np.arctan2(nv, nu)) % 360.0
        slope_deg = float(np.degrees(np.mean(
            np.arccos(np.clip(np.abs(arr @ up), 0.0, 1.0)))))
    else:
        rel = centroid_uv - roi_centroid_uv
        face_az = (np.degrees(np.arctan2(rel[1], rel[0])) + 180.0) % 360.0
        slope_deg = 0.0
    return {"area_m2": area_m2, "centroid_uv": centroid_uv.tolist(),
           "face_az": float(face_az), "slope_deg": slope_deg}


def _seq_of(name: str):
    head = name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def _arm_geometry(cam_names: list[str], azim: np.ndarray, elev: np.ndarray,
                  dist: np.ndarray, arms: dict[str, str] | None) -> dict:
    """§12c.6 per-arm circular stats from the model: mean azimuth, the two
    end azimuths, median camera height/distance from the ROI centroid."""
    out: dict[str, dict] = {}
    if not arms:
        return out
    labels = sorted(set(arms.get(n) for n in cam_names if arms.get(n)))
    for arm in labels:
        idx = [i for i, n in enumerate(cam_names) if arms.get(n) == arm]
        if not idx:
            continue
        a = azim[idx]
        mean_az = _circular_mean_deg(a)
        rel = ((a - mean_az + 180.0) % 360.0) - 180.0
        out[arm] = {
            "mean_az": mean_az,
            "end_low": (mean_az + float(rel.min())) % 360.0,
            "end_high": (mean_az + float(rel.max())) % 360.0,
            "h_med": float(np.median(dist[idx] * np.sin(np.radians(elev[idx])))),
            "d_med": float(np.median(dist[idx])),
        }
    return out


def _outer_ends(arm: str, arms_geom: dict) -> list[float]:
    """§12c.6: the end farther (circularly) from the other arm's mean; both
    ends of a single arm count as outer."""
    g = arms_geom[arm]
    others = [a for a in arms_geom if a != arm]
    if not others:
        return [g["end_low"], g["end_high"]]
    other_mean = arms_geom[others[0]]["mean_az"]
    d_low = _circular_diff_deg(g["end_low"], other_mean)
    d_high = _circular_diff_deg(g["end_high"], other_mean)
    return [g["end_high"] if d_high >= d_low else g["end_low"]]


def _gap_instruction(gap: dict, arms_geom: dict, cam_azimuths: dict[str, float],
                     arms: dict[str, str] | None, theta_g: float = THETA_G,
                     step_deg: float = ARM_STEP_DEG) -> dict | None:
    if not arms_geom:
        return None
    face_az = gap["face_az"]
    if (gap.get("area_m2", 0.0) <= DEGENERATE_GAP_AREA_M2
            and gap.get("slope_deg", 0.0) > DEGENERATE_GAP_SLOPE_DEG):
        # Too small and too steep to trust (see DEGENERATE_GAP_* above) —
        # withhold the instruction rather than act on fit noise.
        return None
    elevation_arm = None
    seqs: list[int] = []
    for arm in arms_geom:
        names = [n for n, a in cam_azimuths.items()
                if (arms or {}).get(n) == arm and _circular_diff_deg(a, face_az) <= 20.0]
        if len(names) >= 3:
            elevation_arm = arm
            seqs = sorted(s for s in (_seq_of(n) for n in names) if s is not None)
            break

    if elevation_arm:
        g = arms_geom[elevation_arm]
        # Clamp below 90 deg: `need_elev` is a right-triangle angle
        # (`stand_m = h_med / tan(need_elev)`), which is only physical
        # short of vertical. 80 deg keeps `stand_m` finite and positive.
        need_elev = max(20.0, min(80.0, theta_g + gap.get("slope_deg", 0.0)))
        stand_m = g["h_med"] / max(np.tan(np.radians(need_elev)), 1e-6)
        lo_hi = [seqs[0], seqs[-1]] if seqs else None
        where = f"near photos {lo_hi[0]}-{lo_hi[1]} " if lo_hi else ""
        text = (f"Part of the slide {where}is hidden. Re-shoot that stretch of arm "
               f"{elevation_arm} from higher ground, or from about {stand_m:.0f} m "
               f"from the slide centre instead of {g['d_med']:.0f} m.")
        return {"kind": "higher", "arm": elevation_arm, "photo_range": lo_hi,
               "need_elev": need_elev, "stand_m": stand_m, "text": text}

    best = None
    for arm in arms_geom:
        for end_az in _outer_ends(arm, arms_geom):
            delta = _circular_diff_deg(end_az, face_az)
            if best is None or delta < best[2]:
                best = (arm, end_az, delta)
    if best is None:
        return None
    arm, _end_az, delta = best
    n = max(3, int(np.ceil(delta / step_deg)) + OUTER_END_EXTRA)
    g = arms_geom[arm]
    text = (f"Continue arm {arm} past its end: walk about {delta:.0f}° further "
           "around the slide, same distance, and take "
           f"{n} more photos, one every few steps.")
    if n > 15:
        text += (" This side needs a long walk. If it is unsafe or blocked, you "
                "can keep an indicative result.")
    return {"kind": "extend_arm", "arm": arm, "n_photos": n, "target_az": face_az,
           "stand_m": g["d_med"], "text": text}


def _new_arm_instruction(arms_geom: dict, gaps: list[dict]) -> dict | None:
    """§12c.6 `new_arm`: only when a single arm is registered."""
    if len(arms_geom) != 1:
        return None
    (arm, g), = arms_geom.items()
    default_az = gaps[0]["face_az"] if gaps else (g["mean_az"] + 90.0) % 360.0
    plus = (g["mean_az"] + 90.0) % 360.0
    minus = (g["mean_az"] - 90.0) % 360.0
    if _circular_diff_deg(plus, default_az) <= _circular_diff_deg(minus, default_az):
        target, side = plus, "right"
    else:
        target, side = minus, "left"
    text = ("Start arm B: keep shooting every few steps while you walk about "
           f"90° around the slide to the {side}, then take at least 12 photos "
           "plus 2 extra at the far end.")
    return {"kind": "new_arm", "arm": arm, "target_az": target, "text": text}


def _largest_missing_run_center(seen: list[bool], centers_deg: list[float]) -> float | None:
    n = len(seen)
    if n == 0 or all(seen):
        return None
    if not any(seen):
        return _circular_mean_deg(centers_deg)
    start = seen.index(True)
    seen_r = seen[start:] + seen[:start]
    centers_r = centers_deg[start:] + centers_deg[:start]
    best, cur = [], []
    for s, c in zip(seen_r, centers_r):
        if s:
            if len(cur) > len(best):
                best = cur
            cur = []
        else:
            cur.append(c)
    if len(cur) > len(best):
        best = cur
    return _circular_mean_deg(best) if best else None


def _rim_missing_instruction(cmap: dict, arms_geom: dict) -> dict | None:
    """§12c.6 rim-side prediction instruction: aimed at the centre of the
    largest run of unseen 90° rim windows, from the far side (+180°)."""
    seen = cmap.get("band_window_seen")
    centers = cmap.get("band_window_center_deg")
    if not seen or not centers or not arms_geom:
        return None
    missing_center = _largest_missing_run_center(seen, centers)
    if missing_center is None:
        return None
    face_az = (missing_center + 180.0) % 360.0
    return _gap_instruction({"face_az": face_az, "slope_deg": 0.0}, arms_geom, {}, {})


def _roi_touches_hull_edge(ctx: ReconCtx, e1: np.ndarray, e2: np.ndarray,
                           roi_ground: np.ndarray, frac: float = 0.30,
                           dist_m: float = 2.5) -> bool:
    """§12c.6 `G9_no_rim`: >=30% of the tapped polygon lies within 2.5 m of
    the sparse cloud's own convex-hull edge — a likely photo-edge trace."""
    from scipy.spatial import ConvexHull

    pts = ctx.sparse * ctx.scale
    uv = np.column_stack([pts @ e1, pts @ e2])
    if len(uv) < 4 or len(roi_ground) < 3:
        return False
    try:
        hull = ConvexHull(uv)
    except Exception:
        return False
    d = ring_distance(np.asarray(roi_ground, np.float64), uv[hull.vertices])
    return float((d <= dist_m).mean()) >= frac


def recapture_instructions(coverage: dict, arms_geom: dict,
                           cam_azimuths: dict[str, float] | None = None,
                           arms: dict[str, str] | None = None) -> list[dict]:
    """§12c.6, §12c.7.1: directional recapture instructions from the
    observation map's `gaps` alone — this function never reads a datum
    estimate, so an unstable-datum finding can never produce one."""
    gaps = sorted(coverage.get("gaps") or [], key=lambda g: -g["area_m2"])
    kept: list[dict] = []
    for g in gaps:
        if any(_circular_diff_deg(g["face_az"], k["face_az"]) <= GAP_MERGE_DEG
              for k in kept):
            continue
        kept.append(g)
    out = []
    for g in kept[:MAX_INSTRUCTIONS]:
        ins = _gap_instruction(g, arms_geom, cam_azimuths or {}, arms)
        if ins:
            out.append(ins)
    return out[:MAX_INSTRUCTIONS]


def preview_report(ctx: ReconCtx, arms: dict[str, str] | None = None,
                   marker_side_m: float | None = None, marker_dict: str = "auto",
                   roi=None, capture_dir=None, log=print) -> dict:
    """T3 preview checks (§4.3, §12c) on a preview `ReconCtx`.

    `arms` maps each image name actually registered by the preview attempt
    to its capture arm ("A"/"B"/"T"/"R<n>"), from the capture manifest —
    used for per-arm registration/elevation/azimuth and the S5C observation
    map. `roi` is the capture session's `{photo_uuid, polygon_px}` tap
    (§12c.3), or None/unregistered/excluded, in which case the §5.1 default
    ROI is used. `capture_dir` (the session's `capture/` directory) is used
    only to read the ROI photo's original pixel size for the tap lift.

    Returns a dict of raw numbers plus `"coverage"` (§12c.4-§12c.6, or
    `{"unavailable": reason}`), `"checks"` (a list of `{id, level, text}`,
    `level` in `{"warn", "block", "info"}`) and `"blocking"` (the ids with
    `level == "block"` — what `finalize` refuses on unless `override=true`).
    Every check reuses an existing diagnostic (`sfm`/`densify`/`scaling`/
    `volume`) rather than recomputing it; ids reuse `gates.RULES` wherever a
    final-gate counterpart exists, `T3_MESSAGES` otherwise.
    """
    checks: list[dict] = []
    seen_ids: set[str] = set()

    def add(level: str, rid: str, text: str) -> None:
        if rid in seen_ids:
            return
        seen_ids.add(rid)
        checks.append({"id": rid, "level": level, "text": text})

    report: dict = {}

    # --- registration and connectivity ---
    n_submitted = len(arms) if arms else len(ctx.views)
    n_reg = len(ctx.views)
    reg_frac = n_reg / n_submitted if n_submitted else 1.0
    missing_arms: list[str] = []
    if arms:
        by_arm: dict[str, list[str]] = {}
        for name, arm in arms.items():
            by_arm.setdefault(arm, []).append(name)
        missing_arms = [arm for arm, names in by_arm.items()
                        if not any(n in ctx.views for n in names)]
    report.update(n_submitted=n_submitted, n_registered=n_reg,
                 registered_frac=reg_frac, missing_arms=missing_arms)
    if reg_frac < REGISTRATION_FRAC_BLOCK or missing_arms:
        add("block", "T3_registration", "Predicted: " + T3_MESSAGES["T3_registration"])

    # --- collinearity (G4) ---
    collin = _camera_center_collinearity(ctx.rec)
    report["collinearity"] = collin
    if collin < 0.10:
        add("block", "G4_collinear", "Predicted: " + RULES["G4_collinear"]["action"])

    # --- focal spread (G3) ---
    _, _, focal_ratio = _focal_spread(ctx.rec)
    report["focal_spread"] = focal_ratio
    if focal_ratio > 1.15:
        add("warn", "G3_focal", "Predicted: " + RULES["G3_focal"]["action"])

    # --- up-vector agreement ---
    up_info: dict = {}
    up = estimate_up(ctx.views, ctx.sparse, info=up_info)
    report["up"] = up_info
    disagree = up_info.get("disagree_deg")
    if disagree is not None and disagree > 20.0:
        add("warn", "UP_disagree", "Predicted: " + RULES["UP_disagree"]["action"])
    e1, e2 = ground_basis(up)

    # --- marker scale (G2/G7) — moved ahead of elevation/ROI/coverage
    # (§12c) since those all need a real metric scale, not just the raw
    # model-unit angles the earlier S4 ordering was scale-invariant for.
    marker_report = None
    marker_error = None
    if marker_side_m:
        try:
            info = aruco_scale(ctx, side_m=marker_side_m, dict_name=marker_dict,
                               log=lambda *_a: None)
            marker_report = {k: v for k, v in info.items() if k != "marker_px"}
            ctx.scale = info["scale"]
            ctx.scale_info = info
            if info.get("warnings"):
                add("warn", "G2_scale_warn", "Predicted: " + RULES["G2_scale_warn"]["action"])
        except Exception as e:
            marker_error = str(e)
            add("block", "T3_marker_scale", "Predicted: "
                + T3_MESSAGES["T3_marker_scale"].format(err=marker_error))
    report["marker"] = marker_report
    report["marker_error"] = marker_error

    # --- ROI lift (§12c.3), when a scale is known ---
    roi_lift = (_lift_roi(ctx, up, e1, e2, roi, capture_dir, log=log)
               if marker_report is not None else
               {"source": "unavailable", "note": "marker scale failed",
                "ground_polygon": None, "hit_frac": None})

    # --- camera elevation and azimuth coverage, referenced to the ROI
    # centroid when one was lifted (§12c.3.5), else the §5.1 default ---
    scale = ctx.scale if marker_report is not None else 1.0
    pts_metric = ctx.sparse * scale
    uv_metric = np.column_stack([pts_metric @ e1, pts_metric @ e2])
    roi_ground_poly = roi_lift["ground_polygon"]
    if roi_ground_poly is None and marker_report is not None:
        roi_ground_poly = _default_roi_polygon(uv_metric)
    if roi_ground_poly is not None and len(roi_ground_poly) >= 3:
        # §3 step 3: "the ROI centroid, not the lowest debris point" — the
        # interior mean height is the excavated crater floor, which drags a
        # sparse-point-weighted centroid well below the true reference and
        # was exactly the S4-era bug (GUIDED_CAPTURE_PROGRESS.md's S4
        # section: "the default-ROI centroid is the cause" of a 17.7-18.0deg
        # proxy against a 22.9-23.1deg ground truth). Use the polygon's own
        # planar centroid (matching coverage_map's `roi_centroid_uv`) with a
        # height from the nearby undisturbed rim band, not the interior.
        poly_centroid_uv = roi_ground_poly.mean(axis=0)
        d_to_poly = ring_distance(uv_metric, roi_ground_poly)
        inside_mask = points_in_polygon(uv_metric, roi_ground_poly)
        rim_mask = (~inside_mask) & (d_to_poly <= RIM_BAND_M)
        if rim_mask.sum() >= 10:
            # the rim band also holds the (vertical, ~2m tall) marker board
            # (§6, DATUM_STABILITY_PROGRESS.md's own later finding on this
            # exact band) — a plain median over it is not "ground height".
            # Reuse the datum fit's own sigma-clip to drop the board before
            # taking the height, the same rejection the full pipeline trusts.
            try:
                _c, _n, _basis, keep = fit_plane_robust(pts_metric[rim_mask])
                kept = pts_metric[rim_mask][keep]
                h_ref = float(np.median(kept @ up)) if len(kept) else \
                    float(np.median(pts_metric[rim_mask] @ up))
            except Exception:
                h_ref = float(np.median(pts_metric[rim_mask] @ up))
        elif inside_mask.sum() >= 3:
            h_ref = float(np.median(pts_metric[inside_mask] @ up))
        else:
            h_ref = float(np.median(pts_metric @ up))
        roi_centroid = poly_centroid_uv[0] * e1 + poly_centroid_uv[1] * e2 + h_ref * up
    else:
        central_mask = _central_half_mask(uv_metric)
        roi_centroid = (pts_metric[central_mask].mean(axis=0) if central_mask.any()
                        else pts_metric.mean(axis=0))

    cam_names = list(ctx.views.keys())
    centers = np.array([ctx.views[n].center for n in cam_names]) * scale
    rel = centers - roi_centroid
    dist = np.maximum(np.linalg.norm(rel, axis=1), 1e-9)
    elev = np.degrees(np.arcsin(np.clip((rel @ up) / dist, -1.0, 1.0)))
    azim = np.degrees(np.arctan2(rel @ e2, rel @ e1)) % 360.0

    med_elev = float(np.median(elev)) if len(elev) else 0.0
    report["elevation_deg"] = {"median": med_elev}
    if med_elev < MIN_ELEVATION_DEG:
        add("warn", "G5_partial", "Predicted: " + RULES["G5_partial"]["action"])

    arms_geom: dict = {}
    if arms:
        per_arm_elev: dict[str, float] = {}
        per_arm_azim: dict[str, float] = {}
        for arm in sorted(set(arms.get(n) for n in cam_names if arms.get(n))):
            idx = [i for i, n in enumerate(cam_names) if arms.get(n) == arm]
            if not idx:
                continue
            per_arm_elev[arm] = float(np.median(elev[idx]))
            per_arm_azim[arm] = _circular_mean_deg(azim[idx])
        report["elevation_deg"]["per_arm"] = per_arm_elev
        if any(v < MIN_ARM_ELEVATION_DEG for v in per_arm_elev.values()):
            add("warn", "G5_partial", "Predicted: " + RULES["G5_partial"]["action"])
        arm_names = sorted(per_arm_azim)
        if len(arm_names) >= 2:
            sep = _circular_diff_deg(per_arm_azim[arm_names[0]], per_arm_azim[arm_names[1]])
            report["arm_separation_deg"] = sep
            if sep < MIN_ARM_SEPARATION_DEG:
                add("block", "T3_azimuth", "Predicted: " + T3_MESSAGES["T3_azimuth"])
        else:
            report["arm_separation_deg"] = None
        arms_geom = _arm_geometry(cam_names, azim, elev, dist, arms)

    # --- stereo pairs (G1 proxy) ---
    try:
        pairs = select_pairs(ctx, log=lambda *_a: None)
        covered = {i for pr in pairs for i in (pr[0].image_id, pr[1].image_id)}
        share = len(covered) / max(len(ctx.views), 1)
    except Exception:
        share = 1.0   # too few points to even try — G1 below will already have fired
    report["stereo_pair_share"] = share
    if share < STEREO_PAIR_SHARE_WARN:
        add("warn", "G1_thin_dense", "Predicted: " + RULES["G1_thin_dense"]["action"])

    # --- coverage map, predicted G6, and directional instructions (§12c) ---
    coverage_report: dict = {"unavailable": "marker scale failed"}
    if marker_report is not None:
        if roi_ground_poly is None or len(roi_ground_poly) < 3:
            coverage_report = {"unavailable": "roi too small"}
        else:
            cmap = coverage_map(ctx, up, e1, e2, roi_ground_poly, arms=arms)
            predicted = None
            if C_REJ is not None and C_OK is not None:
                if cmap["preview_coverage"] < C_REJ:
                    predicted = "rejected"
                elif (cmap["preview_coverage"] >= C_OK
                      and roi_lift["source"] == "tap"):
                    predicted = "ok"
                else:
                    predicted = "indicative"
            coverage_report = {
                "roi_source": roi_lift["source"], "roi_note": roi_lift.get("note"),
                "roi_ground": roi_ground_poly.tolist(),
                "roi_area_m2": cmap["roi_area_m2"], "roi_hit_frac": roi_lift.get("hit_frac"),
                "roi_centroid_uv": cmap["roi_centroid_uv"], "cell_m": cmap["cell_m"],
                "grid": cmap["grid"], "preview_coverage": cmap["preview_coverage"],
                "preview_void_m2": cmap["preview_void_m2"],
                "band_windows_seen": cmap["band_windows_seen"],
                "predicted_g6": predicted, "calibration": COVERAGE_CALIBRATION,
                "gaps": cmap["gaps"],
                # §12c.8/§12c.9: camera dots for the top-down sketch.
                # `compass_offset_deg` stays null — model-to-compass
                # alignment (§5.4) is S3/sensor scope, not built here.
                "cameras": [{"name": n, "arm": (arms or {}).get(n), "seq": _seq_of(n),
                            "u": float(centers[i] @ e1), "v": float(centers[i] @ e2)}
                           for i, n in enumerate(cam_names)],
                "compass_offset_deg": None,
            }
            if predicted == "rejected":
                add("block" if G6_REJECTED_BLOCKS else "warn", "G6_rejected",
                    "Predicted: " + RULES["G6_rejected"]["action"])
            elif predicted == "indicative":
                add("warn", "G6_indicative", "Predicted: " + RULES["G6_indicative"]["action"])

            cam_azimuths = {cam_names[i]: float(azim[i]) for i in range(len(cam_names))}
            instructions = recapture_instructions(cmap, arms_geom, cam_azimuths, arms)
            if (len(arms_geom) == 1 and predicted in ("indicative", "rejected", None)
                    and not any(i["kind"] == "new_arm" for i in instructions)):
                na = _new_arm_instruction(arms_geom, cmap["gaps"])
                if na:
                    instructions = ([na] + instructions)[:MAX_INSTRUCTIONS]

            if cmap["band_windows_seen"] < DATUM_MIN_WINDOWS:
                add("warn", "G9_rim_partial", "Predicted: " + RULES["G9_rim_partial"]["action"])
                rim_ins = _rim_missing_instruction(cmap, arms_geom)
                if rim_ins and len(instructions) < MAX_INSTRUCTIONS:
                    instructions.append(rim_ins)
            coverage_report["instructions"] = instructions[:MAX_INSTRUCTIONS]

            if (roi_lift["source"] == "tap"
                    and _roi_touches_hull_edge(ctx, e1, e2, roi_ground_poly)):
                add("warn", "G9_no_rim", "Predicted: " + RULES["G9_no_rim"]["action"])
    report["coverage"] = coverage_report

    # --- datum estimate (§3a.4, §12c.7.3): information only, never blocks,
    # never an input to recapture_instructions. Uses the same lifted ROI as
    # coverage above. Surfaced as a `checks` entry (level "info") only once
    # the §12c.7.3 recalibration with a real ROI has passed
    # (DATUM_ESTIMATE_CALIBRATED) and only with a tapped ROI — see
    # GUIDED_CAPTURE_PROGRESS.md's S4 section for why the S4-era default-ROI
    # ratio could not discriminate the three calibration scenes.
    datum_report: dict | None = None
    if marker_report is not None and roi_ground_poly is not None and len(roi_ground_poly) >= 3:
        pts = pts_metric
        roi_area_m2 = polygon_area(roi_ground_poly) if len(roi_ground_poly) >= 3 else 0.0
        try:
            interior, rim, _band = select_region_world(
                ctx, e1, e2, roi_ground_poly, rim_inner_m=DATUM_RIM_INNER_M,
                rim_outer_m=DATUM_RIM_OUTER_M, log=lambda *_a: None)
            interior_pts, rim_pts = pts[interior], pts[rim]
            if len(interior_pts) >= 30 and len(rim_pts) >= 15:
                res = prism_volume(interior_pts, rim_pts, up=up, log=lambda *_a: None)
                dh = res.get("datum_holdout_m")
                nwin = res.get("datum_holdout_windows", 0)
                net = abs(res.get("net_volume_m3") or 0.0)
                datum_report = {"datum": res.get("datum"), "datum_holdout_m": dh,
                                "datum_holdout_windows": nwin,
                                "net_volume_m3": res.get("net_volume_m3"),
                                "roi_area_m2": roi_area_m2, "ratio": None}
                if dh is not None and nwin >= DATUM_MIN_WINDOWS and net > 1e-9:
                    ratio = dh * roi_area_m2 / net
                    datum_report["ratio"] = ratio
                    if (DATUM_ESTIMATE_CALIBRATED and roi_lift["source"] == "tap"
                            and ratio > DATUM_HOLDOUT_REL):
                        add("info", "T3_datum_unstable", "Predicted: "
                            + T3_MESSAGES["T3_datum_unstable"])
            else:
                datum_report = {"pending": "not enough rim points to estimate "
                                          "the datum yet"}
        except Exception as e:
            datum_report = {"error": str(e)}
    report["datum_estimate"] = datum_report

    report["checks"] = checks
    report["blocking"] = [c["id"] for c in checks if c["level"] == "block"]
    return report

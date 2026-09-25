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
from .gates import DATUM_MIN_WINDOWS, RULES
from .geometry import polygon_area
from .ortho import ground_basis, select_region_world
from .pipeline import _photo_metrics
from .scaling import _available_dicts, _load_gray, aruco_scale, detect_marker_corners
from .sfm import ReconCtx, _camera_center_collinearity, _focal_spread
from .volume import prism_volume

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
    """§5.1 fallback ROI, used whenever no ROI tap exists (the S5 endpoint
    is not built yet, so S4 always takes this path): the convex hull of the
    central 50% of the sparse cloud's ground-plane projection."""
    from scipy.spatial import ConvexHull

    central = uv[mask if mask is not None else _central_half_mask(uv)]
    if len(central) < 3:
        return central
    hull = ConvexHull(central)
    return central[hull.vertices]


def preview_report(ctx: ReconCtx, arms: dict[str, str] | None = None,
                   marker_side_m: float | None = None, marker_dict: str = "auto",
                   roi=None, log=print) -> dict:
    """T3 preview checks (§4.3) on a preview `ReconCtx`.

    `arms` maps each image name actually registered by the preview attempt
    to its capture arm ("A"/"B"/"T"/"R<n>"), from the capture manifest —
    used for per-arm registration/elevation/azimuth. `roi` is accepted for
    forward compatibility with the (not yet built, S5) ROI-tap endpoint;
    S4 always falls back to the §5.1 default (convex hull of the central
    50% of the sparse cloud) regardless of what is passed, since lifting a
    traced polygon into ground coordinates is S5 scope.

    Returns a dict of raw numbers plus `"checks"` (a list of
    `{id, level, text}`, `level` in `{"warn", "block", "info"}`) and
    `"blocking"` (the ids with `level == "block"` — what `finalize` refuses
    on, per §4.3: "Block means finalize is refused unless override=true").
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

    # --- camera elevation and azimuth coverage (angles: scale-invariant,
    # computed on the raw model-unit cloud so they don't depend on whether
    # the marker check below finds a scale) ---
    e1, e2 = ground_basis(up)
    uv_model = np.column_stack([ctx.sparse @ e1, ctx.sparse @ e2])
    central_mask = _central_half_mask(uv_model)
    roi_centroid = (ctx.sparse[central_mask].mean(axis=0)
                   if central_mask.any() else ctx.sparse.mean(axis=0))

    cam_names = list(ctx.views.keys())
    centers = np.array([ctx.views[n].center for n in cam_names])
    rel = centers - roi_centroid
    dist = np.maximum(np.linalg.norm(rel, axis=1), 1e-9)
    elev = np.degrees(np.arcsin(np.clip((rel @ up) / dist, -1.0, 1.0)))
    azim = np.degrees(np.arctan2(rel @ e2, rel @ e1)) % 360.0

    med_elev = float(np.median(elev)) if len(elev) else 0.0
    report["elevation_deg"] = {"median": med_elev}
    if med_elev < MIN_ELEVATION_DEG:
        add("warn", "G5_partial", "Predicted: " + RULES["G5_partial"]["action"])

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

    # --- marker scale (G2/G7) ---
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

    # --- datum estimate (§3a.4): information only, never blocks. Needs a
    # real metric scale (the rim band is 0.5-2.5 *meters*), so it only runs
    # once the marker check above actually applied one.
    #
    # §12b.6 calibration (run against the cached `twosided`/`twosided_b`/
    # `twosided_cal` reconstructions in data/bench, GUIDED_CAPTURE_PROGRESS.md
    # S4): with no ROI tap (S5) to work from, the only default this stage can
    # use is §5.1's fallback (convex hull of the central 50% of the sparse
    # cloud). That default's centroid sits close enough to the true bowl to
    # be usable for elevation/azimuth, but the ROI polygon it produces is
    # ~3x the true polygon's area (it picks up surrounding terrain, not just
    # the slide) — enough to break the ratio's own discrimination: all three
    # calibration scenes predicted "unstable" (ratios 0.45-0.67), including
    # `twosided_cal`, which must stay silent. Per §12b.6's own fixed rule
    # ("If either fails, the T3 datum estimate is dropped from the UI, not
    # re-tuned"), the ratio is still computed and reported below for future
    # work once S5's real ROI tap exists, but it is never turned into a
    # `checks` entry — `T3_MESSAGES["T3_datum_unstable"]` is unused until a
    # real ROI makes this discriminate correctly.
    datum_report: dict | None = None
    if marker_report is not None:
        pts = ctx.sparse * ctx.scale
        uv = np.column_stack([pts @ e1, pts @ e2])
        roi_polygon = _default_roi_polygon(uv)
        roi_area_m2 = polygon_area(roi_polygon) if len(roi_polygon) >= 3 else 0.0
        try:
            interior, rim, _band = select_region_world(
                ctx, e1, e2, roi_polygon, rim_inner_m=DATUM_RIM_INNER_M,
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
                    # not surfaced as a check — see the calibration note above
                    datum_report["ratio"] = dh * roi_area_m2 / net
            else:
                datum_report = {"pending": "not enough rim points to estimate "
                                          "the datum yet"}
        except Exception as e:
            datum_report = {"error": str(e)}
    report["datum_estimate"] = datum_report

    report["checks"] = checks
    report["blocking"] = [c["id"] for c in checks if c["level"] == "block"]
    return report

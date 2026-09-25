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

from .pipeline import _photo_metrics
from .scaling import _available_dicts, _load_gray, detect_marker_corners

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

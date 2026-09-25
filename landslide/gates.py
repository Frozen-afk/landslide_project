"""Mandatory quality gates (A3): turn the pipeline's own diagnostics into a
single `status` + `reasons` instead of a confident number with no context.

See REMAINING_ACCURACY_PLAN.md section 5 for the source gate table (G1-G7;
G8 is the UI side, server/static/js/steps/result.js). Gates are detectors,
not mitigations — G3/G4 flag a risk the plan's own A6/A7 algorithmic fixes
would otherwise reduce; those are deferred (see REMAINING_ACCURACY_PROGRESS.md)
because the plan itself treats their target cases (descending, nadir) as
explicit-rejection captures, not fix targets.
"""
from __future__ import annotations

from .sfm import FOCAL_SPREAD_RATIO_BAD, ReconCtx, _camera_center_collinearity, _focal_spread

_RANK = {"ok": 0, "indicative": 1, "rejected": 2}

# G9 (DATUM_STABILITY_PLAN.md §6.2)
DATUM_HOLDOUT_REL = 0.05   # the ±5% volume accuracy an "ok" result is held to
DATUM_MIN_WINDOWS = 6      # of 8 overlapping 90° rim arcs that must be usable

# S6a (GUIDED_CAPTURE_PLAN.md §7, §7a, §9.3): one stable id per reason, its
# class (can recapture fix it, or is it the datum/informational?), and the
# "what to do next time" text the result panel shows next to the reason.
# This is the single source for that text — the capture UI (not built yet)
# and the result panel both key off these ids, not the free-text reasons.
RULES: dict[str, dict[str, str]] = {
    "G1_thin_dense": {"class": "capture", "action":
        "Not enough overlapping texture for 3-D. Shoot from an angle (not "
        "straight down), keep 70%+ overlap between photos, and include "
        "textured ground, not just sky or water."},
    "G2_scale_warn": {"class": "capture", "action":
        "Marker scale is uncertain. Move closer, turn the board to face the "
        "camera, flatten it if bent, and include it in more photos."},
    "G3_focal": {"class": "capture", "action":
        "Camera zoom or lens changed mid-capture. Stay on the 1x camera for "
        "every photo."},
    "G4_collinear": {"class": "capture", "action":
        "You walked a straight line. Walk a curve around the slide, and add "
        "a second vantage point well away from the first."},
    "G5_fallback": {"class": "capture", "action":
        "Get higher (at least 20° above the debris). Keep the whole rim "
        "plus a margin in every photo. Never shoot from below the crown."},
    "G5_partial": {"class": "capture", "action":
        "Get higher (at least 20° above the debris). Keep the whole rim "
        "plus a margin in every photo. Never shoot from below the crown."},
    "G5_rejected": {"class": "capture", "action":
        "Get higher (at least 20° above the debris). Keep the whole rim "
        "plus a margin in every photo. Never shoot from below the crown."},
    "UP_disagree": {"class": "capture", "action":
        "Include more flat, stable ground around the slide in frame."},
    "G6_indicative": {"class": "capture", "action":
        "Walk to a second vantage point at least 60° around the slide from "
        "the first, keeping 60-80% overlap, so more of the interior is seen "
        "from at least two angles."},
    "G6_rejected": {"class": "capture", "action":
        "Walk to a second vantage point at least 60° around the slide from "
        "the first, keeping 60-80% overlap, so more of the interior is seen "
        "from at least two angles."},
    "G9_no_rim": {"class": "capture", "action":
        "No undisturbed ground was measured around the outline. First check "
        "the outline traces the slide edge, not the photo edge. If it's "
        "right, recapture with 2-3 m of undisturbed ground around the slide "
        "in every photo."},
    "G9_rim_partial": {"class": "capture", "action":
        "Recapture so the missing side of the slide is in frame. If that "
        "side cannot be seen (cliff, river, no access), this result can be "
        "kept as indicative."},
    "G9_datum_unstable": {"class": "datum", "action":
        "Not a photo problem — more photos will not help. The ground around "
        "the slide varies, so its edge does not predict the surface the "
        "slide removed. For a measured result, upload a pre-event elevation "
        "model (survey, LiDAR, earlier scan) and re-measure."},
    "DEM_no_coverage_gate": {"class": "info", "action":
        "Measured against your pre-event model. Indicative until SlopeLens "
        "can check how much of the slide the model covers."},
}


def classify_measurement_path(status: str, reason_ids: list[str],
                              datum: str | None) -> str:
    """§7a.1: rim / dem / indicative_only / capture_failure. First match
    wins. Additive only — never changes status or any volume number."""
    if any(RULES.get(rid, {}).get("class") == "capture" for rid in reason_ids):
        return "capture_failure"
    if datum == "dem":
        return "dem"
    if "G9_datum_unstable" in reason_ids:
        return "indicative_only"
    if status == "ok":
        return "rim"
    return "capture_failure"          # unmapped reason: never shown as rim/indicative_only


def evaluate_gates(ctx: ReconCtx, res: dict, region_method: str) -> tuple[str, list[str]]:
    """Returns (status, reasons) — status is the worst of every gate fired.
    Also sets res["reason_ids"] (parallel to `reasons`, stable ids from
    RULES) and res["measurement_path"] (§7a.1)."""
    status = "ok"
    reasons: list[str] = []
    reason_ids: list[str] = []

    def flag(level: str, msg: str, reason_id: str) -> None:
        nonlocal status
        if _RANK[level] > _RANK[status]:
            status = level
        reasons.append(msg)
        reason_ids.append(reason_id)

    # G1 — dense cloud actually used
    n_dense = len(ctx.dense["points"]) if ctx.dense is not None else 0
    if res.get("cloud") != "dense" or n_dense < 20_000:
        flag("rejected", f"dense stereo cloud too thin ({n_dense} points) for a "
             "reliable volume — check photo overlap/texture", "G1_thin_dense")

    # G2 — marker / scale quality: aruco_scale already refuses past 10%
    # spread (see scaling.py); this covers the softer 5-10% band it only
    # warns about, and manual_scale's own warnings.
    if ctx.scale_info.get("warnings"):
        flag("indicative", "scale reference quality warning: "
             + "; ".join(ctx.scale_info["warnings"]), "G2_scale_warn")

    # G3 — per-camera sanity (detection only; sfm.reconstruct's F12
    # shared-focal retry is the existing mitigation, A6's per-camera
    # deregistration was not implemented — see progress notes)
    if any("focal length varies" in w for w in (ctx.warnings or [])):
        flag("indicative", "unstable per-camera calibration across the "
             "reconstruction — geometry may be systematically distorted", "G3_focal")

    # G4 — focal constraint on a (near-)collinear capture path: per-camera
    # focal self-calibration is geometrically underconstrained there
    # (F12's focal/depth ambiguity) regardless of how this particular run
    # converged; A7's EXIF-focal-lock mitigation is not implemented.
    try:
        collin = _camera_center_collinearity(ctx.rec)
    except Exception:
        collin = None
    if collin is not None and collin < 0.10:
        flag("indicative", "camera path is (near-)collinear — per-camera "
             "focal length is not geometrically constrained on this path", "G4_collinear")

    # G5 — ray-cast integrity (photo mode only)
    if region_method == "image_projection":
        flag("indicative", "used the image-plane fallback (parallax-sensitive) "
             "— the ground-frame ray-cast could not resolve the boundary", "G5_fallback")
    elif region_method == "ground_frame":
        hit_frac = res.get("hit_frac")
        max_run = res.get("max_miss_run")
        if hit_frac is not None:
            if hit_frac < 0.5:
                flag("rejected", f"ray-cast hit only {hit_frac:.0%} of the "
                     "traced boundary", "G5_rejected")
            elif hit_frac < 0.85 or (max_run or 0) > 3:
                flag("indicative", f"ray-cast hit {hit_frac:.0%} of the traced "
                     f"boundary (longest consecutive miss run {max_run})", "G5_partial")

    # up-vector quality (P4/H1): the scene-plane/camera-plane candidates
    # disagreed by more than 20 deg and a ground-like vote (not agreement)
    # decided which one is "up" — everything referenced to it (slope stats,
    # hazard map, top-down view, DEM gravity seed) inherits that uncertainty.
    disagree = res.get("up_disagree_deg")
    if disagree is not None and disagree > 20.0:
        flag("indicative", f"up-vector candidates disagreed by {disagree:.0f}° "
             f"— chose {res.get('up_source')} by a ground-like vote, not agreement", "UP_disagree")

    # G6 — coverage of the selected interior (RC1/A1)
    cov = res.get("coverage_frac")
    if cov is not None:
        void = res.get("largest_void_m2") or 0.0
        if cov < 0.6:
            flag("rejected", f"only {cov:.0%} of the traced region has nearby "
                 "data", "G6_rejected")
        elif cov < 0.85 or void > 2.0:
            flag("indicative", f"{cov:.0%} coverage, largest unmeasured patch "
                 f"{void:.1f} m² — the volume interpolates across it", "G6_indicative")
    elif res.get("datum") == "dem":
        # dem_volume has no G6 coverage gate: its interior points live in the
        # DEM-aligned world frame, not the model's own (up, polygon_ground)
        # frame G6 needs, so wiring a real coverage_frac would need a second
        # coordinate transform this fix doesn't touch (B3 scope). Forcing
        # "indicative" is the audit's documented fallback (FINAL_RELEASE_
        # AUDIT.md §5, B3) so DEM mode can no longer report "ok" un-gated.
        flag("indicative", "DEM mode has no coverage gate (G6) — treat the "
             "bridged/unmeasured area as unverified", "DEM_no_coverage_gate")

    # G9 — datum stability (DATUM_STABILITY_PLAN.md): the rim datum is
    # extrapolated across the whole interior; G6 proves the interior was seen,
    # not that the datum under it is right.
    datum = res.get("datum")
    if datum == "surface_plane":
        flag("indicative", "no usable rim around the region — the datum was fitted "
             "to the region itself and the volume is biased toward zero", "G9_no_rim")
    elif datum in ("rim_plane", "rim_quad", "rim_tps"):
        dh, nwin = res.get("datum_holdout_m"), res.get("datum_holdout_windows", 0)
        area = res.get("polygon_area_m2") or res.get("area_m2") or 0.0
        net = abs(res.get("net_volume_m3") or 0.0)
        if dh is None or nwin < DATUM_MIN_WINDOWS:
            flag("indicative", f"undisturbed ground seen on only {nwin} of 8 sides "
                 "of the region — the datum cannot be cross-checked", "G9_rim_partial")
        elif dh * area > DATUM_HOLDOUT_REL * net:
            flag("indicative", f"datum uncertain: the ground around the region "
                 f"departs ±{dh:.2f} m from the fitted surface, about "
                 f"{dh * area:.1f} m³ ({dh * area / max(net, 1e-9):.0%}) of the volume",
                 "G9_datum_unstable")

    res["reason_ids"] = reason_ids
    return status, reasons

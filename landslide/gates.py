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


def evaluate_gates(ctx: ReconCtx, res: dict, region_method: str) -> tuple[str, list[str]]:
    """Returns (status, reasons) — status is the worst of every gate fired."""
    status = "ok"
    reasons: list[str] = []

    def flag(level: str, msg: str) -> None:
        nonlocal status
        if _RANK[level] > _RANK[status]:
            status = level
        reasons.append(msg)

    # G1 — dense cloud actually used
    n_dense = len(ctx.dense["points"]) if ctx.dense is not None else 0
    if res.get("cloud") != "dense" or n_dense < 20_000:
        flag("rejected", f"dense stereo cloud too thin ({n_dense} points) for a "
             "reliable volume — check photo overlap/texture")

    # G2 — marker / scale quality: aruco_scale already refuses past 10%
    # spread (see scaling.py); this covers the softer 5-10% band it only
    # warns about, and manual_scale's own warnings.
    if ctx.scale_info.get("warnings"):
        flag("indicative", "scale reference quality warning: "
             + "; ".join(ctx.scale_info["warnings"]))

    # G3 — per-camera sanity (detection only; sfm.reconstruct's F12
    # shared-focal retry is the existing mitigation, A6's per-camera
    # deregistration was not implemented — see progress notes)
    if any("focal length varies" in w for w in (ctx.warnings or [])):
        flag("indicative", "unstable per-camera calibration across the "
             "reconstruction — geometry may be systematically distorted")

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
             "focal length is not geometrically constrained on this path")

    # G5 — ray-cast integrity (photo mode only)
    if region_method == "image_projection":
        flag("indicative", "used the image-plane fallback (parallax-sensitive) "
             "— the ground-frame ray-cast could not resolve the boundary")
    elif region_method == "ground_frame":
        hit_frac = res.get("hit_frac")
        max_run = res.get("max_miss_run")
        if hit_frac is not None:
            if hit_frac < 0.5:
                flag("rejected", f"ray-cast hit only {hit_frac:.0%} of the "
                     "traced boundary")
            elif hit_frac < 0.85 or (max_run or 0) > 3:
                flag("indicative", f"ray-cast hit {hit_frac:.0%} of the traced "
                     f"boundary (longest consecutive miss run {max_run})")

    # up-vector quality (P4/H1): the scene-plane/camera-plane candidates
    # disagreed by more than 20 deg and a ground-like vote (not agreement)
    # decided which one is "up" — everything referenced to it (slope stats,
    # hazard map, top-down view, DEM gravity seed) inherits that uncertainty.
    disagree = res.get("up_disagree_deg")
    if disagree is not None and disagree > 20.0:
        flag("indicative", f"up-vector candidates disagreed by {disagree:.0f}° "
             f"— chose {res.get('up_source')} by a ground-like vote, not agreement")

    # G6 — coverage of the selected interior (RC1/A1)
    cov = res.get("coverage_frac")
    if cov is not None:
        void = res.get("largest_void_m2") or 0.0
        if cov < 0.6:
            flag("rejected", f"only {cov:.0%} of the traced region has nearby "
                 "data")
        elif cov < 0.85 or void > 2.0:
            flag("indicative", f"{cov:.0%} coverage, largest unmeasured patch "
                 f"{void:.1f} m² — the volume interpolates across it")
    elif res.get("datum") == "dem":
        # dem_volume has no G6 coverage gate: its interior points live in the
        # DEM-aligned world frame, not the model's own (up, polygon_ground)
        # frame G6 needs, so wiring a real coverage_frac would need a second
        # coordinate transform this fix doesn't touch (B3 scope). Forcing
        # "indicative" is the audit's documented fallback (FINAL_RELEASE_
        # AUDIT.md §5, B3) so DEM mode can no longer report "ok" un-gated.
        flag("indicative", "DEM mode has no coverage gate (G6) — treat the "
             "bridged/unmeasured area as unverified")

    return status, reasons

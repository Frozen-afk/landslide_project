"""Volume between the terrain surface inside a user polygon and a datum plane.

The datum is fitted to "rim" points — 3D points that project near the polygon
boundary in the selected photo, i.e. undisturbed ground around the landslide.
Volume is then the prism integral of signed point heights over a Delaunay
triangulation in the datum plane (classic 2.5D cut/fill).
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import Delaunay, cKDTree

from .geometry import points_in_polygon, ring_distance
from .sfm import Log, ReconCtx


def fit_plane(pts: np.ndarray):
    """Total-least-squares plane. Returns (centroid, normal, in-plane basis).

    The normal sign is arbitrary; orient it at the call site.
    """
    c = pts.mean(axis=0)
    _, _, Vt = np.linalg.svd(pts - c, full_matrices=False)
    return c, Vt[2], Vt[:2]


def fit_plane_robust(pts: np.ndarray, iters: int = 3, clip: float = 2.5,
                     min_keep_frac: float = 0.5):
    """Sigma-clipped plane fit: (centroid, normal, in-plane basis, inlier_mask).

    The rim band around a real polygon contains vegetation and stereo floaters;
    a plain TLS plane would tilt toward them and bias every height. Iteratively
    refit and drop points farther than `clip`*sigma until stable, but never
    keep fewer than `min_keep_frac` of the points.
    """
    keep = np.ones(len(pts), dtype=bool)
    if len(pts) >= 30:
        for _ in range(iters):
            c, n, _ = fit_plane(pts[keep])
            d = np.abs((pts - c) @ n)
            sigma = float(np.sqrt((d[keep] ** 2).mean()))
            if sigma <= 0:
                break
            new = d <= clip * sigma
            if new.sum() < max(min_keep_frac * len(pts), 15) or (new == keep).all():
                break
            keep = new
    c, n, basis = fit_plane(pts[keep])
    return c, n, basis, keep


def _quad_features(uv: np.ndarray, s: float) -> np.ndarray:
    x, y = uv[:, 0] / s, uv[:, 1] / s
    return np.column_stack([np.ones_like(x), x, y, x * x, y * y, x * y])


def fit_quadratic(uv: np.ndarray, h: np.ndarray, min_pts: int = 40):
    """Least-squares paraboloid h = q(u,v); returns ((coef, scale), rms) or (None, inf).

    Features are normalized by the rim extent so the normal equations stay
    conditioned; a mild ridge keeps degenerate (nearly collinear) rim rings
    from producing wild curvature. A paraboloid is the highest order that
    extrapolates tamely across the polygon interior, where the rim — a thin
    ring — has no data at all (this is why splines/RBFs are not used).
    """
    if len(uv) < min_pts:
        return None, float("inf")
    s = max(float(np.ptp(uv[:, 0])), float(np.ptp(uv[:, 1])), 1e-9)
    A = _quad_features(uv, s)
    ridge = 1e-9 * len(uv)
    ATA = A.T @ A + ridge * np.eye(6)
    try:
        coef = np.linalg.solve(ATA, A.T @ h)
    except np.linalg.LinAlgError:
        return None, float("inf")
    resid = A @ coef - h
    return (coef, s), float(np.sqrt((resid ** 2).mean()))


def eval_quadratic(quad, uv: np.ndarray) -> np.ndarray:
    coef, s = quad
    return _quad_features(np.asarray(uv, np.float64), s) @ coef


def _get_covis(ctx: ReconCtx):
    if getattr(ctx, "_covis", None) is None:
        from .sfm import covisibility_pairs
        ctx._covis = covisibility_pairs(ctx.rec)
    return ctx._covis


def _neighbor_views(ctx: ReconCtx, image_name: str, k: int = 1):
    """The k views sharing the most 3D points with the marked view."""
    view = ctx.views[image_name]
    by_id = {v.image_id: v for v in ctx.views.values()}
    scored = []
    for (a, b), c in _get_covis(ctx).items():
        if a == view.image_id and b in by_id:
            scored.append((c, b))
        elif b == view.image_id and a in by_id:
            scored.append((c, a))
    scored.sort(reverse=True)
    return [by_id[i] for _, i in scored[:k]]


def select_region(ctx: ReconCtx, image_name: str, polygon, rim_px: float = 12.0,
                  rim_inner_px: float | None = None, extra_views: int = 0):
    """Split the cloud into interior/rim by projecting into the marked photo.

    The rim band is an annulus *outside* the polygon line: users tend to click
    slightly inside the debris edge, so the band starts `rim_inner_px` out
    (default half the band width) to avoid sampling fallen material as
    "undisturbed ground".

    With extra_views > 0 the polygon mask is ANDed across the marked view and
    its most-covisible neighbours (poor-man's space carving): background
    objects that merely project into the polygon in one photo — e.g. a marker
    board behind the landslide — are carved away, real surface points are not.
    Returns (view, uv_in_marked_view, interior_mask, rim_mask).
    """
    view = ctx.views[image_name]
    views = [view] + _neighbor_views(ctx, image_name, extra_views)
    inner = rim_px * 0.5 if rim_inner_px is None else float(rim_inner_px)
    pts, _ = ctx.cloud(dense=True)
    interior = np.ones(len(pts), dtype=bool)
    ring = np.ones(len(pts), dtype=bool)
    uv = None
    for i, v in enumerate(views):
        u, _ = v.project(pts)
        if i == 0:
            uv = u
        finite = np.isfinite(u).all(axis=1)
        inside = np.zeros(len(pts), dtype=bool)
        near = np.zeros(len(pts), dtype=bool)
        if finite.any():
            inside[finite] = points_in_polygon(u[finite], polygon)
            # looser ring band in the secondary views: parallax shifts edge
            # points between views, the band must not shave the rim itself
            m = 1.0 if i == 0 else 2.0
            d = ring_distance(u[finite], polygon)
            near[finite] = (d >= inner * m) & (d <= (inner + rim_px) * m)
        interior &= inside
        ring &= near
    rim = ring & ~interior
    return view, uv, interior, rim


def prism_volume(interior: np.ndarray, rim: np.ndarray | None,
                 log: Log = print, max_above_datum: float | None = None,
                 up: np.ndarray | None = None,
                 max_edge_factor: float = 20.0,
                 max_edge_region_frac: float = 0.5) -> dict:
    """Cut/fill volume of interior points above the rim-fitted datum surface.

    All units: whatever the points are in (caller passes metric-scaled pts).
    The datum is a robustly-fitted plane, upgraded to a paraboloid when the
    rim residual shows the ground is curved (road crowns, hillsides). The
    datum normal is oriented by `up` (scene vertical from camera layout)
    when given, otherwise so that most interior points sit below it. Points
    floating implausibly high above the datum (stereo outliers, objects
    behind the surface) are trimmed before integration.

    Delaunay interpolates across small data holes, which is desirable (the
    surface is smooth there); only triangles bridging a gap longer than
    max(max_edge_factor × point spacing, max_edge_region_frac × region
    diameter) are excluded, so a region marked over unreconstructed
    background doesn't invent area/volume.
    """
    if len(interior) < 30:
        raise RuntimeError(
            f"only {len(interior)} points inside the polygon — mark a larger "
            "area, use more photos, or enable the dense cloud")
    warnings: list[str] = []
    datum_pts = rim if (rim is not None and len(rim) >= 15) else interior
    if datum_pts is interior:
        warnings.append("too few rim points — datum fitted to the region surface "
                        "itself, volume will be biased toward zero")

    # rim steepness filter: the rim must be undisturbed GROUND. Points on
    # steep surfaces — a retaining wall the polygon edge climbs, boulders,
    # the debris face itself — sit tens of cm above the road and tilt the
    # datum so far that a pile reads as a depression (inverted cut/fill).
    # Drop rim points whose local surface normal is >~55° off vertical.
    if datum_pts is rim and up is not None and len(rim) >= 20:
        local = np.vstack([rim, interior])
        k = min(10, len(local) - 1)
        _, idx = cKDTree(local).query(rim, k=k, workers=-1)
        nb = local[idx].astype(np.float64)
        nb -= nb.mean(axis=1, keepdims=True)
        _, _, Vt = np.linalg.svd(nb, full_matrices=False)
        steep = np.abs(Vt[:, 2, :] @ up) < 0.57
        if steep.any():
            keep = ~steep
            if keep.sum() >= 15:
                log(f"[volume] rim steepness filter: dropped {int(steep.sum())} "
                    f"of {len(rim)} rim points on steep surfaces (walls / debris "
                    f"face) before the datum fit")
                warnings.append(
                    f"{int(steep.sum())} rim points on steep surfaces (wall? "
                    "debris face?) were excluded from the datum — trace the "
                    "boundary where the debris meets flat ground for best "
                    "accuracy")
                rim = rim[keep]
                datum_pts = rim
            else:
                warnings.append("most of the rim is on steep surfaces — the "
                                "datum may be unreliable; re-trace the boundary "
                                "on flat ground around the debris")

    # rim elevation sanity: a rim band spanning large heights means the
    # boundary runs over structure, not around the region on one surface
    if datum_pts is rim and up is not None:
        rh = datum_pts @ up
        if np.ptp(rh) > 0.6:
            warnings.append(f"rim heights span {np.ptp(rh):.1f} m — the boundary "
                            "seems to climb a slope/wall; the datum averages "
                            "over that and volumes can invert (pile read as "
                            "depression). Trace on one surface")

    c, n, basis, inliers = fit_plane_robust(datum_pts)
    if datum_pts is rim and not inliers.all():
        frac = float((~inliers).mean())
        warnings.append(f"{frac:.0%} of rim points were outliers and were "
                        "excluded from the datum plane fit")
        log(f"[volume] robust datum: clipped {int((~inliers).sum())} rim outliers "
            f"({frac:.0%})")
    if up is not None:
        if n @ up < 0:
            n = -n
    elif float(((interior - c) @ n).sum()) > 0:   # majority-below convention
        n = -n

    # curved-slope datum: a plane cuts through crowned roads and hillsides;
    # upgrade to a paraboloid when it clearly explains more rim residual
    datum = "rim_plane" if datum_pts is rim else "surface_plane"
    quad = None
    resid = (datum_pts[inliers] - c) @ n
    sigma = float(np.sqrt((resid ** 2).mean()))
    if datum_pts is rim:
        uv_r = (datum_pts[inliers] - c) @ basis.T
        h_r = (datum_pts[inliers] - c) @ n
        quad, sigma_q = fit_quadratic(uv_r, h_r)
        # adopt curvature only when it matters in absolute terms: with tens of
        # thousands of rim points even noise-level curvature is statistically
        # "significant", and extrapolating it across the region would bias
        # the volume more than the flat plane it replaces
        if quad is not None and (sigma - sigma_q) > max(0.02, 0.25 * sigma):
            log(f"[volume] curved datum (paraboloid): rim rms {sigma:.3f} -> "
                f"{sigma_q:.3f} m")
            sigma = sigma_q
            datum = "rim_quad"

    cap = max_above_datum if max_above_datum is not None else max(1.5, 8.0 * sigma)
    h = (interior - c) @ n                    # signed heights above datum
    uv2 = (interior - c) @ basis.T            # in-plane 2D coords
    if quad is not None and datum == "rim_quad":
        h = h - eval_quadratic(quad, uv2)     # heights above the curved datum
    dropped = h > cap
    if dropped.any():
        log(f"[volume] dropping {int(dropped.sum())} points floating >{cap:.2f} m "
            f"above the datum (stereo outliers / background objects)")
        interior = interior[~dropped]
        h = h[~dropped]
    uv2 = (interior - c) @ basis.T            # in-plane 2D coords

    tri = Delaunay(uv2)
    simp = tri.simplices
    p0, p1, p2 = uv2[simp[:, 0]], uv2[simp[:, 1]], uv2[simp[:, 2]]
    # cross product z-component in 2D = parallelogram area
    area_tri = 0.5 * np.abs((p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) -
                            (p2[:, 0] - p0[:, 0]) * (p1[:, 1] - p0[:, 1]))
    h_tri = h[simp].mean(axis=1)
    v_tri = area_tri * h_tri

    # drop catastrophic "bridging" triangles: Delaunay happily spans data
    # holes and the convex-hull rim with long triangles whose heights
    # interpolate across the gap. Small holes are fine (smooth surface), so
    # the threshold is anchored to the region's own diameter and only kills
    # bridges over genuinely-missing data.
    d_self, _ = cKDTree(uv2).query(uv2, k=2, workers=-1)
    spacing = float(np.median(d_self[:, 1]))
    lo, hi = np.percentile(uv2, [1, 99], axis=0)
    diam = float(np.linalg.norm(hi - lo))
    max_edge = max(max_edge_factor * spacing, max_edge_region_frac * diam)
    edges = np.stack([np.linalg.norm(p1 - p0, axis=1),
                      np.linalg.norm(p2 - p1, axis=1),
                      np.linalg.norm(p0 - p2, axis=1)])
    keep_tri = edges.max(axis=0) <= max_edge
    if not keep_tri.all():
        bridged = float(area_tri[~keep_tri].sum())
        log(f"[volume] dropping {int((~keep_tri).sum())} bridging triangles "
            f"(edge > {max_edge:.2f} m), {bridged:.1f} m^2 of unsupported area")
        if bridged > 0.05 * float(area_tri.sum()):
            warnings.append(f"{bridged:.0f} m² of the marked region could not be "
                            "reconstructed and was excluded — the volume covers "
                            "only the measured part")
        area_tri, v_tri, h_tri = area_tri[keep_tri], v_tri[keep_tri], h_tri[keep_tri]

    fill = float(v_tri[h_tri > 0].sum())      # material above datum
    cut = float(-v_tri[h_tri < 0].sum())      # depression below datum
    net = fill - cut                          # depression -> negative net
    area = float(area_tri.sum())

    if sigma > 0.5 and area > 0:
        warnings.append(f"datum plane residual is high (rms {sigma:.2f} m) — the "
                        "ground around the polygon is rough or curved; treat the "
                        "absolute volumes with caution")
    return {
        "net_volume_m3": net,
        "cut_volume_m3": cut,
        "fill_volume_m3": fill,
        "area_m2": area,
        "datum": datum,
        "datum_rms_m": sigma,
        "est_volume_error_m3": sigma * area,
        "n_points": int(len(interior)),
        "n_rim_points": int(len(rim)) if rim is not None else 0,
        "n_rim_outliers": int((~inliers).sum()) if datum_pts is rim else 0,
        "n_high_dropped": int(dropped.sum()),
        "mean_height_m": float(h.mean()),
        "max_depth_m": float(-h.min()) if len(h) else 0.0,
        "max_height_m": float(h.max()) if len(h) else 0.0,
        "warnings": warnings,
        "_debug": {"uv2": uv2, "h": h, "centroid": c, "normal": n},
    }

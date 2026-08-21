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


def fit_plane_ransac(pts: np.ndarray, iters: int = 250, seed: int = 12345):
    """MSAC plane consensus; returns a boolean inlier mask (or None).

    Seeds `fit_plane_robust` when the rim band carries a *clustered*
    contaminant (vegetation patch, a rubble pile in the band, stereo floaters
    from one bad pair): iterative sigma-clipping starts from an all-points
    fit that such a cluster can drag — and with it the datum normal — before
    clipping ever engages. The consensus search runs on a bounded subsample
    so cost is flat in rim size; the final mask is evaluated on every point.
    Deterministic: fixed seed, no randomness escapes to callers.
    """
    pts = np.asarray(pts, np.float64)
    n = len(pts)
    if n < 30:
        return None
    extent = float(np.ptp(pts, axis=0).max())
    if extent <= 0:
        return None
    rng = np.random.default_rng(seed)
    sub = pts[rng.choice(n, min(n, 20_000), replace=False)]
    thr = max(0.005 * extent, 1e-9)     # 0.5% of scene extent, refined below
    best_mask, best_score = None, np.inf
    m = len(sub)
    for _ in range(iters):
        i, j, k = rng.choice(m, 3, replace=False)
        normal = np.cross(sub[j] - sub[i], sub[k] - sub[i])
        nn = float(np.linalg.norm(normal))
        if nn < 1e-12 * extent:
            continue
        normal /= nn
        d = np.abs((sub - sub[i]) @ normal)
        inl = d <= thr
        cnt = int(inl.sum())
        if cnt < 3:
            continue
        score = float(d[inl].sum()) + (m - cnt) * thr    # MSAC truncation
        if score < best_score:
            best_score, best_mask = score, inl
    if best_mask is None:
        return None
    # refine: TLS plane on the consensus, then re-evaluate the inlier mask at
    # 2.5x the consensus's own robust (MAD-based) scale, on the FULL rim
    c, n0, _ = fit_plane(sub[best_mask])
    d = np.abs((pts - c) @ n0)
    d_con = np.abs((sub[best_mask] - c) @ n0)
    sigma = 1.4826 * float(np.median(d_con)) if len(d_con) else 0.0
    thr_ref = max(2.5 * sigma, 1e-9)
    keep = d <= thr_ref
    if keep.sum() < max(0.25 * n, 15):    # degenerate consensus — refuse
        return None
    return keep


def _clip_loop(pts: np.ndarray, keep: np.ndarray, iters: int, clip: float,
               min_keep_frac: float) -> np.ndarray:
    """Iterative sigma-clip refinement starting from `keep`; may grow or shrink."""
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
    return keep


def _med_abs_resid(pts: np.ndarray, keep: np.ndarray) -> float:
    c, n, _ = fit_plane(pts[keep])
    return float(np.median(np.abs((pts - c) @ n)))


def fit_plane_robust(pts: np.ndarray, iters: int = 3, clip: float = 2.5,
                     min_keep_frac: float = 0.5):
    """Sigma-clipped plane fit: (centroid, normal, in-plane basis, inlier_mask).

    Two candidates are refined and compared: the plain all-points clip (the
    right seed when the rim is genuinely curved — a later paraboloid upgrade
    handles the curvature, and a RANSAC plane would lock onto one band of
    the ring) and a RANSAC-seeded clip (the right seed when a clustered
    contaminant — rubble in the band, a vegetation patch — would drag the
    all-points fit). The seeded fit wins only when its plane explains the
    whole rim decisively better (median absolute residual over ALL points,
    so a tight fit on a tiny subset cannot win by construction).
    """
    pts = np.asarray(pts, np.float64)
    keep_all = _clip_loop(pts, np.ones(len(pts), dtype=bool),
                          iters, clip, min_keep_frac)
    seed = fit_plane_ransac(pts)
    if seed is not None:
        keep_seed = _clip_loop(pts, seed, iters, clip, min_keep_frac)
        if keep_seed.sum() >= max(0.25 * len(pts), 15):
            r_all = _med_abs_resid(pts, keep_all)
            r_seed = _med_abs_resid(pts, keep_seed)
            if r_all > 1e-12 and r_seed < 0.5 * r_all:
                keep_all = keep_seed
    c, n, basis = fit_plane(pts[keep_all])
    return c, n, basis, keep_all


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


def slope_stats(uv: np.ndarray, z: np.ndarray, steep_deg: float = 35.0,
                min_cell_pts: int = 3):
    """Gridded surface-slope statistics: (max_deg, mean_deg, steep_area_m2).

    Per-triangle gradients amplify point noise on thin triangles into ~90°
    spikes; binning to a grid first (mean height over >=3 points per cell)
    and taking central differences over the cell size is stable at the
    decimeter scale the hazard classification needs.
    """
    uv = np.asarray(uv, np.float64)
    z = np.asarray(z, np.float64)
    if len(uv) < 30:
        return 0.0, 0.0, 0.0
    d_self, _ = cKDTree(uv).query(uv, k=2, workers=-1)
    spacing = float(np.median(d_self[:, 1]))
    cell = float(np.clip(2.5 * spacing, 0.05, 1.0))
    nx = int(np.ceil(np.ptp(uv[:, 0]) / cell)) + 1
    ny = int(np.ceil(np.ptp(uv[:, 1]) / cell)) + 1
    ix = np.clip(((uv[:, 0] - uv[:, 0].min()) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((uv[:, 1] - uv[:, 1].min()) / cell).astype(int), 0, ny - 1)
    flat = iy * nx + ix
    counts = np.bincount(flat, minlength=nx * ny)
    sums = np.bincount(flat, weights=z, minlength=nx * ny)
    ok = counts >= min_cell_pts
    if ok.sum() < 9:                      # need a 3x3 core for gradients
        return 0.0, 0.0, 0.0
    grid = np.full(nx * ny, np.nan)
    grid[ok] = sums[ok] / counts[ok]
    g = grid.reshape(ny, nx)
    gy, gx = np.gradient(g, cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    valid = np.isfinite(slope)
    if not valid.any():
        return 0.0, 0.0, 0.0
    steep = valid & (slope > steep_deg)
    return (float(slope[valid].max()),
            float(slope[valid].mean()),
            float(steep.sum()) * cell * cell)


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

    # ---- secondary-hazard: surface slope (over-steepened debris / scarp) ----
    # slope of the real surface above the datum plane (quad datum included),
    # computed on a robust grid — see slope_stats
    z_pts = h + eval_quadratic(quad, uv2) \
        if (quad is not None and datum == "rim_quad") else h
    max_slope, mean_slope, area_steep = slope_stats(uv2, z_pts)
    if area_steep > max(0.5, 0.02 * area):
        warnings.append(f"{area_steep:.1f} m² of the surface is steeper than "
                        "35° (over-steepened debris or scarp — secondary "
                        "slide risk while clearing)")

    # ---- statistical significance (LoD-style, 95%) ----
    # cells whose |height| is below ~2 sigma of the datum/surface noise carry
    # no reliable change signal; reported, not thresholded — zeroing them
    # would bias thin real layers toward zero volume
    lod = 1.96 * max(sigma, 1e-6)
    sig = np.abs(h_tri) > lod
    sig_area_frac = float(area_tri[sig].sum() / area) if area > 0 else 0.0
    vol_noise = lod * area
    if abs(net) < vol_noise:
        warnings.append(f"net volume ({net:.2f} m³) is within survey noise "
                        f"(±{vol_noise:.2f} m³ at 95%) — the change may not "
                        "be real")

    if sigma > 0.5 and area > 0:
        warnings.append(f"datum plane residual is high (rms {sigma:.2f} m) — the "
                        "ground around the polygon is rough or curved; treat the "
                        "absolute volumes with caution")
    # per-point surface height above the datum plane (quad included) for the
    # slope-map artifact
    z_pts = h + eval_quadratic(quad, uv2) \
        if (quad is not None and datum == "rim_quad") else h
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
        "max_slope_deg": max_slope,
        "mean_slope_deg": mean_slope,
        "area_steep_m2": area_steep,
        "lod_m": lod,
        "sig_area_frac": sig_area_frac,
        "warnings": warnings,
        "_debug": {"uv2": uv2, "h": h, "z": z_pts, "centroid": c, "normal": n},
    }

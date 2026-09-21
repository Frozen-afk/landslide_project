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


def _tps_kernel(d2: np.ndarray) -> np.ndarray:
    """Thin-plate-spline kernel r^2 log r of squared distances (0 at r=0)."""
    out = np.zeros_like(d2)
    m = d2 > 0
    out[m] = 0.5 * d2[m] * np.log(d2[m])
    return out


def fit_tps_membrane(uv: np.ndarray, h: np.ndarray, lam: float,
                     seed: int = 0, max_pts: int = 4000):
    """Classic smoothing thin-plate-spline surface; returns model or None.

    f(x) = a + b.x + c.y + sum_i w_i K(||x - x_i||), coordinates unit-scale
    normalized internally (the model stores the scale; eval_tps applies it).
    Solved as the symmetric saddle system [[K + lam*n*I, P], [P', 0]] whose
    side conditions P'w = 0 are essential: they are what let curvature
    propagate from the rim ring across the region interior without the
    runaway oscillation that kept splines out of the datum until now.
    (No robust reweighting needed: rim points reaching this stage are
    already RANSAC/sigma-clip filtered.)
    """
    uv = np.asarray(uv, np.float64)
    h = np.asarray(h, np.float64)
    if len(uv) < 60:
        return None
    if len(uv) > max_pts:
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(uv), max_pts, replace=False)
        uv, h = uv[idx], h[idx]
    s = max(float(np.ptp(uv, axis=0).max()), 1e-9)
    uvn = uv / s
    D2 = ((uvn[:, None, :] - uvn[None, :, :]) ** 2).sum(-1)
    K = _tps_kernel(D2)
    P = np.column_stack([np.ones(len(uvn)), uvn])
    n = len(uvn)
    A = np.zeros((n + 3, n + 3))
    A[:n, :n] = K + lam * n * np.eye(n)
    A[:n, n:] = P
    A[n:, :n] = P.T
    b = np.concatenate([h, np.zeros(3)])
    try:
        sol = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None
    return {"sup": uvn, "w": sol[:n], "a": sol[n:n + 3], "s": s}


def eval_tps(model, uv: np.ndarray, chunk: int = 100_000) -> np.ndarray:
    """Evaluate a fit_tps_membrane model at raw (unnormalized) points."""
    uv = np.asarray(uv, np.float64) / model["s"]
    out = np.empty(len(uv))
    sup, w, a = model["sup"], model["w"], model["a"]
    for s0 in range(0, len(uv), chunk):
        q = uv[s0:s0 + chunk]
        d2 = ((q[:, None, :] - sup[None, :, :]) ** 2).sum(-1)
        out[s0:s0 + chunk] = _tps_kernel(d2) @ w + a[0] \
            + a[1] * q[:, 0] + a[2] * q[:, 1]
    return out


def _fit_surface(kind: str, uv: np.ndarray, h: np.ndarray):
    """Least-squares surface of `kind` ('plane' | 'quad' | 'tps'); returns a
    callable uv -> height, or None if the fit is impossible."""
    if kind == "plane":
        A = np.column_stack([np.ones(len(uv)), uv])
        coef, *_ = np.linalg.lstsq(A, h, rcond=None)
        return lambda q: coef[0] + coef[1] * q[:, 0] + coef[2] * q[:, 1]
    if kind == "quad":
        s = max(float(np.ptp(uv, axis=0).max()), 1e-9)
        F = _quad_features(uv, s)
        ridge = 1e-9 * len(uv)
        coef = np.linalg.solve(F.T @ F + ridge * np.eye(6), F.T @ h)
        return lambda q: _quad_features(q, s) @ coef
    if kind == "tps":
        m = fit_tps_membrane(uv, h, lam=1e-6)
        if m is None:
            return None
        return lambda q: eval_tps(m, q)
    raise ValueError(kind)


def _cv_rmse(kind: str, uv: np.ndarray, h: np.ndarray, folds: int = 3,
             seed: int = 0) -> float:
    """Held-out RMSE of a surface model via k-fold cross-validation."""
    n = len(uv)
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    errs = []
    for k in range(folds):
        te = perm[k::folds]
        tr = np.setdiff1d(perm, te, assume_unique=True)
        if len(tr) < 30:
            continue
        f = _fit_surface(kind, uv[tr], h[tr])
        if f is None:
            return float("inf")
        errs.append(((f(uv[te]) - h[te]) ** 2).mean())
    return float(np.sqrt(np.mean(errs))) if errs else float("inf")


def dem_volume(interior_xyz: np.ndarray, dem_fn, log: Log = print,
               max_edge_factor: float = 20.0,
               max_edge_region_frac: float = 0.5) -> dict:
    """Cut/fill of a surface against an imported pre-event DEM.

    `interior_xyz` are the region's points already mapped into the DEM's
    world frame (see dem.align_to_dem); `dem_fn(xy)` returns DEM heights
    (NaN off the hull). Heights are the direct surface − DEM difference —
    no rim, no extrapolated datum. Shares the bridging/slope/LoD machinery
    of prism_volume.
    """
    interior_xyz = np.asarray(interior_xyz, np.float64)
    if len(interior_xyz) < 30:
        raise RuntimeError("too few points in the region for DEM differencing")
    warnings: list[str] = []
    z_dem = dem_fn(interior_xyz[:, :2])
    ok = np.isfinite(z_dem)
    if ok.mean() < 0.6:
        raise RuntimeError(
            f"only {ok.mean():.0%} of the region lies on the imported DEM — "
            "check the DEM extent or the alignment")
    if not ok.all():
        warnings.append(f"{(1 - ok.mean()):.0%} of the region falls outside "
                        "the DEM and was excluded")
    p = interior_xyz[ok]
    h = p[:, 2] - z_dem[ok]

    tri = Delaunay(p[:, :2])
    simp = tri.simplices
    q0, q1, q2 = p[simp[:, 0]], p[simp[:, 1]], p[simp[:, 2]]
    area_tri = 0.5 * np.abs((q1[:, 0] - q0[:, 0]) * (q2[:, 1] - q0[:, 1]) -
                            (q2[:, 0] - q0[:, 0]) * (q1[:, 1] - q0[:, 1]))
    h_tri = h[simp].mean(axis=1)
    v_tri = area_tri * h_tri

    d_self, _ = cKDTree(p[:, :2]).query(p[:, :2], k=2, workers=-1)
    spacing = float(np.median(d_self[:, 1]))
    lo, hi = np.percentile(p[:, :2], [1, 99], axis=0)
    diam = float(np.linalg.norm(hi - lo))
    max_edge = max(max_edge_factor * spacing, max_edge_region_frac * diam)
    edges = np.stack([np.linalg.norm(q1 - q0, axis=1),
                      np.linalg.norm(q2 - q1, axis=1),
                      np.linalg.norm(q0 - q2, axis=1)])
    keep_tri = edges.max(axis=0) <= max_edge
    if not keep_tri.all():
        bridged = float(area_tri[~keep_tri].sum())
        log(f"[dem-volume] dropping {int((~keep_tri).sum())} bridging "
            f"triangles, {bridged:.1f} m^2 unsupported")
        if bridged > 0.05 * float(area_tri.sum()):
            warnings.append(f"{bridged:.0f} m² of the marked region could "
                            "not be reconstructed and was excluded")
        area_tri, v_tri, h_tri = area_tri[keep_tri], v_tri[keep_tri], \
            h_tri[keep_tri]

    fill = float(v_tri[h_tri > 0].sum())
    cut = float(-v_tri[h_tri < 0].sum())
    net = fill - cut
    area = float(area_tri.sum())

    sigma = float(np.sqrt(((h[simp][keep_tri].mean(axis=1) - h_tri) ** 2)
                          .mean())) if len(h_tri) else 0.0
    max_slope, mean_slope, area_steep = slope_stats(p[:, :2], h + dem_fn(
        p[:, :2]))
    if area_steep > max(0.5, 0.02 * area):
        warnings.append(f"{area_steep:.1f} m² of the surface is steeper than "
                        "35° (over-steepened debris or scarp — secondary "
                        "slide risk while clearing)")
    centroids = (q0[keep_tri] + q1[keep_tri] + q2[keep_tri])[:, :2] / 3.0
    lod_tri, lod_max = _lod_per_triangle(p[:, :2], h, sigma, centroids,
                                         spacing)
    sig = np.abs(h_tri) > lod_tri
    sig_area_frac = float(area_tri[sig].sum() / area) if area > 0 else 0.0
    vol_noise = 1.96 * sigma * area
    if abs(net) < vol_noise:
        warnings.append(f"net volume ({net:.2f} m³) is within survey noise "
                        f"(±{vol_noise:.2f} m³ at 95%) — the change may not "
                        "be real")

    return {
        "net_volume_m3": net, "cut_volume_m3": cut, "fill_volume_m3": fill,
        "area_m2": area, "datum": "dem", "datum_rms_m": sigma,
        "est_volume_error_m3": sigma * area,
        "n_points": int(len(p)), "n_rim_points": 0,
        "mean_height_m": float(h.mean()),
        "max_depth_m": float(-h.min()), "max_height_m": float(h.max()),
        "max_slope_deg": max_slope, "mean_slope_deg": mean_slope,
        "area_steep_m2": area_steep,
        "lod_m": 1.96 * sigma, "lod_max_m": lod_max,
        "sig_area_frac": sig_area_frac,
        "warnings": warnings,
        "_debug": {"uv2": p[:, :2], "h": h, "z": h + dem_fn(p[:, :2])},
    }


def _get_covis(ctx: ReconCtx):
    if getattr(ctx, "_covis", None) is None:
        from .sfm import covisibility_pairs
        ctx._covis = covisibility_pairs(ctx.rec)
    return ctx._covis


def local_roughness(uv: np.ndarray, h: np.ndarray, k: int = 9) -> np.ndarray:
    """Per-point residual to the k-NN local plane — slope-free roughness.

    Cell-wise height spread conflates noise with real slope; the residual to
    each point's own local plane isolates the noise the LoD field needs.
    """
    if len(uv) <= k:
        return np.zeros(len(uv))
    _, idx = cKDTree(uv).query(uv, k=k, workers=-1)
    nb = uv[idx]                                             # (n, k, 2)
    P = np.concatenate([np.ones((*nb.shape[:2], 1)), nb], axis=2)
    H = h[idx]                                               # (n, k)
    A = np.einsum("nki,nkj->nij", P, P)                      # (n, 3, 3)
    b = np.einsum("nki,nk->ni", P, H)
    A = A + 1e-9 * np.eye(3) * np.maximum(
        A.max(axis=(1, 2), keepdims=True), 1e-9)
    try:
        # numpy 2 batched solve wants an explicit trailing rhs dimension
        coef = np.linalg.solve(A, b[..., None])[..., 0]
    except np.linalg.LinAlgError:
        return np.zeros(len(uv))
    Q = np.column_stack([np.ones(len(uv)), uv])              # (n, 3)
    return np.abs(h - np.einsum("ni,ni->n", Q, coef))


def _lod_per_triangle(uv2: np.ndarray, h: np.ndarray, sigma_datum: float,
                      centroids: np.ndarray, spacing: float,
                      min_cell_pts: int = 3) -> tuple[np.ndarray, float]:
    """Spatially varying 95% detection limit at each triangle centroid.

    LoD(x,y) = 1.96 * sqrt(sigma_datum^2 + sigma_local(x,y)^2) where the
    local noise comes from gridded roughness (points matched by fewer
    stereo pairs / farther from the baseline are noisier and looser).
    Returns (lod per centroid, max lod over valid cells).
    """
    rough = local_roughness(uv2, h)
    cell = float(np.clip(2.5 * spacing, 0.05, 1.0))
    x0, y0 = uv2[:, 0].min(), uv2[:, 1].min()
    nx = int(np.ceil(np.ptp(uv2[:, 0]) / cell)) + 1
    ny = int(np.ceil(np.ptp(uv2[:, 1]) / cell)) + 1
    ix = np.clip(((uv2[:, 0] - x0) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((uv2[:, 1] - y0) / cell).astype(int), 0, ny - 1)
    flat = iy * nx + ix
    counts = np.bincount(flat, minlength=nx * ny)
    rsum = np.bincount(flat, weights=rough, minlength=nx * ny)
    sig_local = np.zeros(nx * ny)
    m = counts >= min_cell_pts
    # mean|r| of zero-mean noise ~ 0.8 sigma
    sig_local[m] = np.maximum(rsum[m] / counts[m] / 0.8, 0.0)
    lod_cell = 1.96 * np.sqrt(sigma_datum ** 2 + sig_local ** 2)
    cx = np.clip(((centroids[:, 0] - x0) / cell).astype(int), 0, nx - 1)
    cy = np.clip(((centroids[:, 1] - y0) / cell).astype(int), 0, ny - 1)
    lod_tri = lod_cell[cy * nx + cx]
    lod_max = float(lod_cell[m].max()) if m.any() else 1.96 * sigma_datum
    return lod_tri, lod_max


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


def _front_surface_mask(u: np.ndarray, depth: np.ndarray, width: int, height: int,
                        cell_px: int = 4) -> np.ndarray:
    """Coarse z-buffer occlusion test: keep points near the nearest depth in
    their raster cell, drop points that project into the polygon but sit
    behind the visible surface (e.g. terrain behind a hill, a marker board
    behind the debris).
    """
    n = len(u)
    if n == 0:
        return np.zeros(0, dtype=bool)
    med = float(np.median(depth))
    tol = max(0.02 * med, 1e-9)
    nx = max(1, int(np.ceil(width / cell_px)) + 1)
    ny = max(1, int(np.ceil(height / cell_px)) + 1)
    ix = np.clip((u[:, 0] / cell_px).astype(np.int64), 0, nx - 1)
    iy = np.clip((u[:, 1] / cell_px).astype(np.int64), 0, ny - 1)
    flat = iy * nx + ix
    front_depth = np.full(nx * ny, np.inf)
    np.minimum.at(front_depth, flat, depth)
    return depth <= front_depth[flat] + tol


def select_region(ctx: ReconCtx, image_name: str, polygon, rim_px: float = 12.0,
                  rim_inner_px: float | None = None, extra_views: int = 0):
    """Split the cloud into interior/rim by projecting into the marked photo.

    The rim band is an annulus *outside* the polygon line: users tend to click
    slightly inside the debris edge, so the band starts `rim_inner_px` out
    (default half the band width) to avoid sampling fallen material as
    "undisturbed ground".

    Points behind the marked view's visible surface are excluded by a coarse
    per-view z-buffer (`_front_surface_mask`) before the polygon test, so a
    background object that merely projects inside the polygon line (terrain
    behind a hill on an oblique photo) is not integrated as if it were the
    debris surface.

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
        u, depth = v.project(pts)
        if i == 0:
            uv = u
        finite = np.isfinite(u).all(axis=1) & (depth > 0)
        inside = np.zeros(len(pts), dtype=bool)
        near = np.zeros(len(pts), dtype=bool)
        if finite.any():
            idx = np.flatnonzero(finite)
            front = _front_surface_mask(u[idx], depth[idx], v.width, v.height)
            visible = idx[front]
            if len(visible):
                inside[visible] = points_in_polygon(u[visible], polygon)
                # looser ring band in the secondary views: parallax shifts edge
                # points between views, the band must not shave the rim itself
                m = 1.0 if i == 0 else 2.0
                d = ring_distance(u[visible], polygon)
                near[visible] = (d >= inner * m) & (d <= (inner + rim_px) * m)
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
    tps = None
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

        # membrane challenge: a road that bends AND descends needs more than
        # one paraboloid. The spline's known danger (oscillation across the
        # rim's inner hole) is held in check by smoothing + Huber and the
        # cross-validation gate: the TPS must beat the adopted model on
        # held-out rim points by a clear margin, or the simpler datum stays
        if sigma > 0.03 and len(uv_r) >= 200:
            rng = np.random.default_rng(0)
            if len(uv_r) > 2500:
                idx = rng.choice(len(uv_r), 2500, replace=False)
                uv_c, h_c = uv_r[idx], h_r[idx]
            else:
                uv_c, h_c = uv_r, h_r
            cur = "quad" if datum == "rim_quad" else "plane"
            cv_cur = _cv_rmse(cur, uv_c, h_c)
            cv_tps = _cv_rmse("tps", uv_c, h_c)
            if cv_tps < cv_cur - max(0.02, 0.15 * cv_cur):
                m = fit_tps_membrane(uv_r, h_r, lam=1e-6)
                # extrapolation guard: the rim only proves curvature IT
                # exhibits — a membrane swinging more inside the region than
                # ~the rim's own residual is guessing, and the interior of a
                # large region is genuinely unknowable from a far rim
                if m is not None:
                    uv2_all = (interior - c) @ basis.T
                    base = (eval_quadratic(quad, uv2_all)
                            if datum == "rim_quad" else 0.0)
                    dev = float(np.abs(eval_tps(m, uv2_all) - base).max())
                    if dev <= max(0.10, 1.5 * sigma):
                        tps = m
                        datum = "rim_tps"
                        sigma = float(np.sqrt(
                            ((eval_tps(m, uv_r) - h_r) ** 2).mean()))
                        log(f"[volume] membrane datum (smoothing TPS): "
                            f"held-out rim rms {cv_cur:.3f} -> {cv_tps:.3f} m "
                            f"(interior deviation {dev:.3f} m)")
                    else:
                        log(f"[volume] membrane rejected: interior deviation "
                            f"{dev:.2f} m exceeds the rim's own variation — "
                            "region too large for rim-only curvature")

    cap = max_above_datum if max_above_datum is not None else max(1.5, 8.0 * sigma)
    h = (interior - c) @ n                    # signed heights above datum
    uv2 = (interior - c) @ basis.T            # in-plane 2D coords
    if quad is not None and datum == "rim_quad":
        h = h - eval_quadratic(quad, uv2)     # heights above the curved datum
    elif tps is not None:
        h = h - eval_tps(tps, uv2)            # heights above the membrane
    # cut depth is unbounded in principle (a scarp can legitimately run many
    # meters below the datum) while fill above the datum rarely does, so the
    # low-side cap widens to the region's own height spread (Tukey's 3xIQR
    # extreme-outlier fence) instead of reusing the tight rim-noise cap —
    # a stereo floater a few cm below sparse, real ground is still rejected,
    # a genuine deep depression is not.
    if max_above_datum is not None:
        cap_low = cap
    else:
        iqr = float(np.percentile(h, 75) - np.percentile(h, 25))
        cap_low = max(cap, 3.0 * abs(iqr))
    dropped = h > cap
    dropped_low = h < -cap_low
    if dropped.any():
        log(f"[volume] dropping {int(dropped.sum())} points floating >{cap:.2f} m "
            f"above the datum (stereo outliers / background objects)")
    if dropped_low.any():
        log(f"[volume] dropping {int(dropped_low.sum())} points >{cap_low:.2f} m "
            f"below the datum (stereo floaters below the surface)")
    keep = ~(dropped | dropped_low)
    if not keep.all():
        interior = interior[keep]
        h = h[keep]
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

    # ---- statistical significance (spatially varying LoD, 95%) ----
    # cells whose |height| is below the local detection limit carry no
    # reliable change signal; reported, not thresholded — zeroing them would
    # bias thin real layers toward zero volume
    centroids = (p0[keep_tri] + p1[keep_tri] + p2[keep_tri]) / 3.0
    lod_tri, lod_max = _lod_per_triangle(uv2, h, sigma, centroids, spacing)
    sig = np.abs(h_tri) > lod_tri
    sig_area_frac = float(area_tri[sig].sum() / area) if area > 0 else 0.0
    vol_noise = 1.96 * sigma * area
    if abs(net) < vol_noise:
        warnings.append(f"net volume ({net:.2f} m³) is within survey noise "
                        f"(±{vol_noise:.2f} m³ at 95%) — the change may not "
                        "be real")

    if sigma > 0.5 and area > 0:
        warnings.append(f"datum plane residual is high (rms {sigma:.2f} m) — the "
                        "ground around the polygon is rough or curved; treat the "
                        "absolute volumes with caution")
    # per-point surface height above the datum plane (curved datum included)
    # for the slope-map artifact
    if quad is not None and datum == "rim_quad":
        z_pts = h + eval_quadratic(quad, uv2)
    elif tps is not None:
        z_pts = h + eval_tps(tps, uv2)
    else:
        z_pts = h
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
        "n_low_dropped": int(dropped_low.sum()),
        "mean_height_m": float(h.mean()),
        "max_depth_m": float(-h.min()) if len(h) else 0.0,
        "max_height_m": float(h.max()) if len(h) else 0.0,
        "max_slope_deg": max_slope,
        "mean_slope_deg": mean_slope,
        "area_steep_m2": area_steep,
        "lod_m": 1.96 * sigma,
        "lod_max_m": lod_max,
        "sig_area_frac": sig_area_frac,
        "warnings": warnings,
        "_debug": {"uv2": uv2, "h": h, "z": z_pts, "centroid": c, "normal": n},
    }

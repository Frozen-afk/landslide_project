"""Volume between the terrain surface inside a user polygon and a datum plane.

The datum is fitted to "rim" points — 3D points that project near the polygon
boundary in the selected photo, i.e. undisturbed ground around the landslide.
Volume is then the prism integral of signed point heights over a Delaunay
triangulation in the datum plane (classic 2.5D cut/fill).
"""
from __future__ import annotations

import numpy as np
from scipy.spatial import Delaunay, cKDTree

from .geometry import points_in_polygon, polygon_area, ring_distance
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
                     min_keep_frac: float = 0.5, ransac_iters: int = 250):
    """Sigma-clipped plane fit: (centroid, normal, in-plane basis, inlier_mask).

    Two candidates are refined and compared: the plain all-points clip (the
    right seed when the rim is genuinely curved — a later paraboloid upgrade
    handles the curvature, and a RANSAC plane would lock onto one band of
    the ring) and a RANSAC-seeded clip (the right seed when a clustered
    contaminant — rubble in the band, a vegetation patch — would drag the
    all-points fit). The seeded fit wins only when its plane explains the
    whole rim decisively better (median absolute residual over ALL points,
    so a tight fit on a tiny subset cannot win by construction).

    `ransac_iters` (F10) is separate from `iters` (the clip-loop repeat
    count): the primary fit needs the full RANSAC search, but
    `bootstrap_volume_ci` calls this B times per measurement and only needs
    each replicate's consensus plane to be roughly right, not the primary
    fit's full search depth.
    """
    pts = np.asarray(pts, np.float64)
    keep_all = _clip_loop(pts, np.ones(len(pts), dtype=bool),
                          iters, clip, min_keep_frac)
    seed = fit_plane_ransac(pts, iters=ransac_iters)
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


def eval_tps(model, uv: np.ndarray, chunk: int | None = None) -> np.ndarray:
    """Evaluate a fit_tps_membrane model at raw (unnormalized) points.

    `chunk` defaults to bounding the per-chunk (chunk x n_support) pairwise
    distance temporaries to a fixed element budget rather than a flat
    100_000 rows: with up to `fit_tps_membrane`'s max_pts=4000 support
    points, a flat 100_000-row chunk builds several (100000, 4000, ...)
    float64 temporaries (~3-6 GB EACH) — confirmed as the actual source of
    a "collinear dense stage reaches ~16 GB RSS" report, which traced back
    to this call (interior-deviation check in `prism_volume`, not the
    dense-cloud build itself) on the interior's full point count. Bounding
    the row*support product instead keeps every eval, on any interior size
    or support count, to the same modest working set.
    """
    uv = np.asarray(uv, np.float64) / model["s"]
    out = np.empty(len(uv))
    sup, w, a = model["sup"], model["w"], model["a"]
    if chunk is None:
        chunk = max(1000, 2_000_000 // max(len(sup), 1))
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
               max_edge_abs_m: float = 0.5) -> dict:
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
    # RC1/A1 (matches prism_volume): an absolute cap, not a fraction of the
    # region's own diameter — the old `0.5 * diam` term let the TIN bridge a
    # 30-60 m² unobserved patch with a handful of long triangles instead of
    # excluding it (see FINAL_RELEASE_AUDIT.md §4.3/B3).
    max_edge = max(max_edge_factor * spacing, max_edge_abs_m)
    edges = np.stack([np.linalg.norm(q1 - q0, axis=1),
                      np.linalg.norm(q2 - q1, axis=1),
                      np.linalg.norm(q0 - q2, axis=1)])
    keep_tri = edges.max(axis=0) <= max_edge
    # per-vertex residual to each kept triangle's own mean height — a real
    # local-roughness estimate. The old line compared h_tri (already the
    # per-triangle mean) against itself recomputed the same way, so the
    # "residual" was identically zero and sigma/LoD/est_volume_error_m3
    # never reported any uncertainty (FINAL_RELEASE_AUDIT.md §4.3/B3).
    verts_h = h[simp[keep_tri]] if keep_tri.any() else h[simp[:0]]
    sigma = float(np.sqrt(((verts_h - verts_h.mean(axis=1, keepdims=True)) ** 2)
                          .mean())) if len(verts_h) else 0.0
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


def _raster_bin(uv2: np.ndarray, h: np.ndarray, cell: float,
                min_cell_pts: int = 1) -> dict:
    """Median height + MAD per grid cell (T2.1 DSM).

    A per-cell median is a cleaner noise model than k-NN roughness (each
    cell's spread comes straight from its own points, no neighbourhood
    smoothing) and doubles as the primary cut/fill integrator: it does not
    inherit the Delaunay triangulation's willingness to bridge across a
    concave polygon boundary the way the TIN's convex hull does.

    min_cell_pts=1 (not 3): with `cell` sized from the *median* point
    spacing, a cell count of 1-2 is routine in any real (non-uniform-
    density) cloud, not just at outliers — requiring 3 silently dropped
    ~half the footprint (and most of a synthetic bowl's cut volume, at its
    steeper, sparser-stereo-coverage centre) during development, for cells
    that had perfectly good single-point data. A cell of 1 just carries no
    local sigma estimate (0, below); `sigma_datum` still bounds its LoD.

    Vectorized (F10/M2): a Python loop over occupied cells dominated
    `bootstrap_volume_ci`'s runtime once F2's fix stopped starving the
    cloud (this is called once per bootstrap replicate). Per-cell median
    and MAD are both computed via two `lexsort`s (group cells together,
    then order each group's values / absolute deviations) instead of a
    per-cell `np.median` call — the median/MAD of an odd-or-even-length
    sorted run is just the mean of its two middle elements, so no Python
    loop over cells is needed at all.
    """
    x0, y0 = float(uv2[:, 0].min()), float(uv2[:, 1].min())
    nx = int(np.ceil(np.ptp(uv2[:, 0]) / cell)) + 1
    ny = int(np.ceil(np.ptp(uv2[:, 1]) / cell)) + 1
    ix = np.clip(((uv2[:, 0] - x0) / cell).astype(np.int64), 0, nx - 1)
    iy = np.clip(((uv2[:, 1] - y0) / cell).astype(np.int64), 0, ny - 1)
    flat = iy * nx + ix

    order = np.lexsort((h, flat))              # grouped by cell, sorted by h within
    flat_s, h_s = flat[order], h[order]
    uniq, starts, counts = np.unique(flat_s, return_index=True, return_counts=True)
    lo = starts + (counts - 1) // 2
    hi = starts + counts // 2
    med = 0.5 * (h_s[lo] + h_s[hi])             # median of each group (odd or even)

    dev = np.abs(h_s - np.repeat(med, counts))  # aligned with h_s (same group order)
    order2 = np.lexsort((dev, flat_s))          # re-sort each group by |deviation|
    dev_s = dev[order2]
    mad = 0.5 * (dev_s[lo] + dev_s[hi])

    keep = counts >= min_cell_pts
    height = np.full(nx * ny, np.nan)
    sigma = np.zeros(nx * ny)
    count = np.zeros(nx * ny, np.int64)
    height[uniq[keep]] = med[keep]
    sigma[uniq[keep]] = 1.4826 * mad[keep]
    count[uniq[keep]] = counts[keep]
    return {"x0": x0, "y0": y0, "cell": cell, "nx": nx, "ny": ny,
            "height": height, "sigma": sigma, "count": count}


def _raster_net(uv2: np.ndarray, h: np.ndarray, cell: float,
                min_cell_pts: int = 1) -> float:
    """Fast net raster volume with no hole-fill — used inside the bootstrap
    (T2.2), where 50 repeats make the primary path's hole-fill loop too slow
    and the CI only needs the spread, not the exact anchor.
    """
    grid = _raster_bin(uv2, h, cell, min_cell_pts)
    valid = grid["count"] >= min_cell_pts
    area_cell = cell * cell
    fill = float(np.sum(np.where(valid & (grid["height"] > 0), grid["height"], 0.0)))
    cut = -float(np.sum(np.where(valid & (grid["height"] < 0), grid["height"], 0.0)))
    return (fill - cut) * area_cell


def _fill_small_holes(grid: dict, max_radius: int = 20, min_neighbors: int = 2):
    """Fill data gaps that are genuinely *enclosed* by data within
    `max_radius` cells (real data on both sides along one axis — above and
    below, or left and right) — real coverage gaps (a sparse patch inside an
    otherwise-continuous cloud, a density gradient toward a foreshortened
    slope) as opposed to the polygon's own outer boundary. Deliberately not
    a convex-hull fill: a hull over a concave or multi-blob footprint (e.g.
    two separate patches with a gap between them) would happily call the
    whole gap "inside" and bridge it — exactly the TIN bridging failure this
    integrator exists to avoid (see `prism_volume`'s docstring and
    `tests/test_volume.py`'s `test_tin_does_not_bridge_large_gaps`).
    `max_radius` is the caller's own `max_edge` (the TIN's bridging limit)
    in cells, so both integrators agree on how big a gap is still "the same
    surface" — a small fixed radius under-fills real density gradients (lost
    ~40% of a synthetic bowl's cut volume at its foreshortened, sparser
    center during development) while still refusing the two disjoint patches.

    ponytail: nearest-neighbour-mean fill instead of literal bilinear
    interpolation (the plan's wording) — same intent, cheaper; upgrade to a
    proper bilinear/IDW fill if hole shapes start to matter. Enclosure is
    checked with 1D row/column scans, not a 2D window sum, so a large
    `max_radius` stays cheap.

    A gap cell with nearby data that ISN'T enclosed (the common case right
    at the edge of the real footprint) is left unfilled and flagged
    "unmeasured" rather than silently dropped, since it did have some data
    nearby — just not enough to trust an interpolated height.
    Returns (height (filled), valid mask, unmeasured mask), all flat
    (nx*ny,).
    """
    nx, ny = grid["nx"], grid["ny"]
    height = grid["height"].reshape(ny, nx)
    has_data = (grid["count"] > 0).reshape(ny, nx)
    filled = height.copy()
    filled_mask = np.zeros((ny, nx), dtype=bool)
    unmeasured_mask = np.zeros((ny, nx), dtype=bool)
    gy, gx = np.nonzero(~has_data)
    for y, x in zip(gy.tolist(), gx.tolist()):
        ylo, yhi = max(0, y - max_radius), min(ny, y + max_radius + 1)
        xlo, xhi = max(0, x - max_radius), min(nx, x + max_radius + 1)
        lx, ly = x - xlo, y - ylo
        # enclosure checked along the gap cell's OWN row/column only (1D) —
        # a 2D window check trivially "sees both sides" next to a wide solid
        # block (a patch edge has data within a few rows both above and
        # below without ever having data on its empty side), which is what
        # let the bridging bug back in during development.
        row_has = has_data[y, xlo:xhi]
        col_has = has_data[ylo:yhi, x]
        row_l, row_r = row_has[:lx], row_has[lx + 1:]
        col_u, col_d = col_has[:ly], col_has[ly + 1:]
        enc_x = bool(row_l.any() and row_r.any())
        enc_y = bool(col_u.any() and col_d.any())
        if not (enc_x or enc_y):
            if int(row_has.sum()) + int(col_has.sum()) >= min_neighbors:
                unmeasured_mask[y, x] = True
            continue
        vals = np.concatenate([height[y, xlo:xhi][row_has],
                               height[ylo:yhi, x][col_has]])
        filled[y, x] = float(vals.mean())
        filled_mask[y, x] = True
    valid = has_data | filled_mask
    return filled.ravel(), valid.ravel(), unmeasured_mask.ravel()


def bootstrap_volume_ci(interior: np.ndarray, rim: np.ndarray, up, cap: float,
                        cap_low: float, cell: float, datum: str, log: Log = print,
                        B: int = 200, seed: int = 0):
    """Resampling-based 95% net-volume SPREAD (T2.2/F9), as (lo_offset,
    hi_offset) to apply around the caller's own (TIN) net volume — NOT an
    absolute interval. Each of B replicates (1) resamples the RIM points and
    refits the datum plane (and quadratic, if the real fit used one), so
    datum uncertainty enters, and (2) block-bootstraps the INTERIOR by
    resampling raster cells (not raw points) of the per-replicate height
    field with replacement, so stereo depth noise and coverage patchiness
    enter too (F9 — resampling only the rim understated the interval because
    the dominant real error sources are region selection, coverage gaps and
    stereo noise, not datum-plane variance). Cells are the right resampling
    unit: nearby points share correlated stereo/coverage noise, so treating
    each point as an independent draw would understate the spread again.
    Offsets are measured from the resample distribution's OWN median so a
    systematic raster/TIN gap cancels out; the spread itself is reported via
    a normal approximation (1.96 * sample std) rather than raw B=50
    percentiles, which single-sample the 2.5th/97.5th tails.

    Deliberate simplification vs. the plan's literal design: the TPS
    membrane is never refit per replicate (200 dense n x n solves would
    dominate a measurement's runtime) — a `rim_tps` datum bootstraps its
    quadratic stage instead, which still captures rim-resampling variance in
    the plane/curvature, just not the membrane's own wiggle. The outlier caps
    are held fixed at the real fit's values rather than recomputed per
    replicate (cost, per the plan: "cost is dominated by the datum fit").
    """
    if rim is None or len(rim) < 15:
        return None
    rng = np.random.default_rng(seed)
    n_rim = len(rim)
    want_quad = datum in ("rim_quad", "rim_tps")
    nets = []
    for _ in range(B):
        idx = rng.integers(0, n_rim, n_rim)
        rim_b = rim[idx]
        # F10: 60 RANSAC iterations (vs. the primary fit's 250) keeps a
        # B=200-replicate bootstrap from dominating a measurement's runtime
        # — each replicate only needs a roughly-right consensus plane, not
        # the primary fit's full search depth.
        c_b, n_b, basis_b, inl_b = fit_plane_robust(rim_b, ransac_iters=60)
        if up is not None:
            if n_b @ up < 0:
                n_b = -n_b
        elif float(((interior - c_b) @ n_b).sum()) > 0:
            n_b = -n_b
        h_b = (interior - c_b) @ n_b
        uv2_b = (interior - c_b) @ basis_b.T
        if want_quad and int(inl_b.sum()) >= 40:
            uv_r = (rim_b[inl_b] - c_b) @ basis_b.T
            h_r = (rim_b[inl_b] - c_b) @ n_b
            quad_b, _ = fit_quadratic(uv_r, h_r)
            if quad_b is not None:
                h_b = h_b - eval_quadratic(quad_b, uv2_b)
        keep = (h_b <= cap) & (h_b >= -cap_low)
        if int(keep.sum()) < 30:
            continue
        # block bootstrap: bin this replicate's heights into the same raster
        # cells the primary cross-check uses, then resample CELLS (not raw
        # points) with replacement — a cell absorbs the local stereo/coverage
        # noise, so resampling cells (blocks) propagates that noise into the
        # spread instead of averaging it away the way i.i.d. point resampling
        # would on a spatially-correlated field.
        grid = _raster_bin(uv2_b[keep], h_b[keep], cell)
        valid = np.flatnonzero(grid["count"] > 0)
        if len(valid) < 10:
            continue
        ridx = rng.choice(valid, size=len(valid), replace=True)
        nets.append(float(np.sum(grid["height"][ridx])) * cell * cell)
    if len(nets) < max(10, B // 4):
        log("[volume] bootstrap CI: too few valid resamples, skipping")
        return None
    med = float(np.median(nets))
    sd = float(np.std(nets, ddof=1)) if len(nets) > 1 else 0.0
    half = 1.96 * sd
    return half, half


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


def _coverage_gate(interior_metric: np.ndarray, up: np.ndarray,
                   polygon_ground: np.ndarray, cell: float = 0.25) -> dict | None:
    """G6 coverage (RC1/A1): occupancy and largest connected void of the
    traced ground polygon, on a 0.25 m grid.

    Deliberately independent of the datum/TIN machinery below — it uses the
    region's own horizontal (e1, e2) ground basis (from `up`), not the
    (possibly tilted, arbitrarily rotated) datum-plane basis the height
    integration picks — so it measures the real footprint coverage no
    matter which surface model was adopted for heights. This is the number
    the bridging cull was silently hiding: on every synthetic preset the
    pipeline observes 40-62% of the traced region and used to integrate the
    rest across a single wide void via long Delaunay triangles.
    """
    from scipy.ndimage import label

    from .ortho import ground_basis

    poly = np.asarray(polygon_ground, np.float64)
    area = polygon_area(poly)
    if area <= 0 or len(poly) < 3:
        return None
    e1, e2 = ground_basis(up)
    u, v = interior_metric @ e1, interior_metric @ e2
    lo_u, lo_v = float(poly[:, 0].min()), float(poly[:, 1].min())
    hi_u, hi_v = float(poly[:, 0].max()), float(poly[:, 1].max())
    nx = max(1, int(np.ceil((hi_u - lo_u) / cell)))
    ny = max(1, int(np.ceil((hi_v - lo_v) / cell)))
    xs = lo_u + (np.arange(nx) + 0.5) * cell
    ys = lo_v + (np.arange(ny) + 0.5) * cell
    Xc, Yc = np.meshgrid(xs, ys, indexing="xy")
    inside = points_in_polygon(np.column_stack([Xc.ravel(), Yc.ravel()]), poly) \
        .reshape(ny, nx)
    n_inside = int(inside.sum())
    if n_inside == 0:
        return None
    in_bounds = (u >= lo_u) & (u <= hi_u) & (v >= lo_v) & (v <= hi_v)
    ix = np.clip(((u[in_bounds] - lo_u) / cell).astype(np.int64), 0, nx - 1)
    iy = np.clip(((v[in_bounds] - lo_v) / cell).astype(np.int64), 0, ny - 1)
    occ = np.zeros((ny, nx), dtype=bool)
    occ[iy, ix] = True
    occupancy = float((occ & inside).sum() / n_inside)
    lbl, n_lbl = label(inside & ~occ)
    largest_void = (float(np.bincount(lbl.ravel())[1:].max()) * cell * cell
                    if n_lbl else 0.0)
    return {"coverage_frac": occupancy, "largest_void_m2": largest_void,
            "polygon_area_m2": float(area)}


def prism_volume(interior: np.ndarray, rim: np.ndarray | None,
                 log: Log = print, max_above_datum: float | None = None,
                 up: np.ndarray | None = None,
                 max_edge_factor: float = 20.0,
                 max_edge_abs_m: float = 0.5,
                 polygon_ground: np.ndarray | None = None) -> dict:
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
    max(max_edge_factor × point spacing, max_edge_abs_m) are excluded (RC1/
    A1: dropped the old "× region diameter" term — on a 30-60 m² unobserved
    back-facing slope, half the region's own diameter let the TIN silently
    bridge the whole void with a handful of 5-7 m triangles and integrate a
    single confident number across a hole covering 40-60% of the traced
    polygon). `area_measured_m2`/`bridged_area_m2`/`cut_measured_m3`/
    `cut_upper_m3` report what was actually observed vs. guessed; when
    `polygon_ground` (the traced polygon in metric ground coordinates) is
    given, `coverage_frac`/`largest_void_m2` (G6, `_coverage_gate`) measure
    the same thing independently, on a 0.25 m occupancy grid instead of the
    TIN's own triangle-edge threshold.

    `net_volume_m3`/`cut_volume_m3`/`fill_volume_m3`/`area_m2` are the TIN
    integral above. T2.1 (`_raster_bin`) also bins the same points into a
    per-cell median/MAD raster DSM and reports it as an independent
    cross-check (`volume_raster_m3`, `unmeasured_area_m2` for cells with no
    nearby data at all, a warning on >10% disagreement with the TIN). The
    plan's own design made the raster the PRIMARY integrator; validated
    against this codebase's real (multi-view-fused) benchmark scene, that
    regressed volume accuracy from the TIN's established 7-8% error to
    33-40% — real stereo clouds have locally sparse-but-continuous patches
    (foreshortened terrain gets fewer multi-view depth-consensus votes) that
    the raster's anti-bridging logic correctly refuses to interpolate over on
    principle, while the TIN's linear interpolation happens to track a smooth
    natural surface well there. So the TIN stays primary; the raster is a
    diagnostic. When the rim datum is used, `net_volume_ci95_m3` (T2.2,
    `bootstrap_volume_ci`) gives a resampling-based 95% interval — centered
    on the TIN net, spread estimated from the (cheap) raster proxy — in
    place of the old flat `sigma_datum × area` heuristic.
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
    interior_pre_cap = interior               # for T2.2's bootstrap (rim resampling)
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
    # the threshold is anchored to point spacing and a small absolute cap
    # (RC1/A1) — NOT a fraction of the region's own diameter, which on a
    # 30-60 m² unobserved back-facing slope let the TIN bridge the entire
    # void with a handful of long triangles and report one confident number
    # for ground nobody actually measured.
    d_self, _ = cKDTree(uv2).query(uv2, k=2, workers=-1)
    spacing = float(np.median(d_self[:, 1]))
    max_edge = max(max_edge_factor * spacing, max_edge_abs_m)
    edges = np.stack([np.linalg.norm(p1 - p0, axis=1),
                      np.linalg.norm(p2 - p1, axis=1),
                      np.linalg.norm(p0 - p2, axis=1)])
    keep_tri = edges.max(axis=0) <= max_edge
    bridged_area = 0.0
    if not keep_tri.all():
        bridged_area = float(area_tri[~keep_tri].sum())
        log(f"[volume] dropping {int((~keep_tri).sum())} bridging triangles "
            f"(edge > {max_edge:.2f} m), {bridged_area:.1f} m^2 of unsupported area")
        if bridged_area > 0.05 * float(area_tri.sum()):
            warnings.append(f"{bridged_area:.0f} m² of the marked region could not be "
                            "reconstructed and was excluded — the volume covers "
                            "only the measured part")
        area_tri, v_tri, h_tri = area_tri[keep_tri], v_tri[keep_tri], h_tri[keep_tri]

    fill = float(v_tri[h_tri > 0].sum())      # material above datum
    cut = float(-v_tri[h_tri < 0].sum())      # depression below datum
    net = fill - cut                          # depression -> negative net
    area = float(area_tri.sum())              # = area_measured_m2

    # ---- G6 coverage gate (RC1/A1) ----
    coverage = (_coverage_gate(interior, up, polygon_ground)
               if (up is not None and polygon_ground is not None) else None)
    polygon_area_m2 = coverage["polygon_area_m2"] if coverage else None
    cut_upper_m3 = None
    if polygon_area_m2 is not None:
        unseen = max(0.0, polygon_area_m2 - area)
        cut_upper_m3 = cut + unseen * float(-h.min()) if len(h) else cut

    # ---- T2.1: raster DSM cross-check ----
    # Plan's literal design made this the PRIMARY integrator; validated
    # against this codebase's one real (multi-view-fused, not synthetic-
    # uniform) benchmark scene during development, that made volume error
    # WORSE (33-40%, vs the TIN's established 7-8%): real stereo clouds have
    # locally sparse-but-geometrically-continuous patches (steep/foreshort-
    # ened terrain sees fewer consensus depth estimates) that the raster's
    # gap logic correctly refuses to bridge on principle, while the TIN's
    # linear interpolation across the same sparse patch happens to track a
    # smooth natural surface well. So the TIN stays primary and the raster
    # is kept as an independent cross-check (`volume_raster_m3`) and the
    # source of `unmeasured_area_m2` / the bootstrap's resampling proxy
    # below — still real value, just not a replacement for the integrator
    # with the actual track record on this codebase's real data.
    density = len(interior) / max(area, 1e-9)
    cell_r = float(np.clip(np.sqrt(6.0 / max(density, 1e-9)), 0.05, 1.0))
    grid = _raster_bin(uv2, h, cell_r)
    fill_radius = max(2, int(np.ceil(max_edge_factor * spacing / cell_r)))
    r_height, r_valid, r_unmeasured = _fill_small_holes(grid, max_radius=fill_radius)
    area_cell = cell_r * cell_r
    r_fill = float(np.sum(np.where(r_valid & (r_height > 0), r_height, 0.0))) * area_cell
    r_cut = -float(np.sum(np.where(r_valid & (r_height < 0), r_height, 0.0))) * area_cell
    r_net = r_fill - r_cut
    r_area = float(r_valid.sum()) * area_cell
    unmeasured_area = float(r_unmeasured.sum()) * area_cell
    if r_area > 0:
        if unmeasured_area > max(2.0, 0.10 * area):
            warnings.append(f"{unmeasured_area:.0f} m² inside the traced region has no "
                            "nearby data (coverage gap, per the raster cross-check)")
        reldiff = abs(r_net - net) / max(abs(r_net), abs(net), 1e-9)
        if reldiff > 0.10:
            warnings.append(f"raster and TIN cut/fill estimates disagree by {reldiff:.0%} "
                            f"(raster {r_net:.2f} vs TIN {net:.2f} m³) — coverage may be "
                            "patchy in the marked region")
            log(f"[volume] raster/TIN disagreement: {reldiff:.0%} "
                f"(raster {r_net:.2f} m³, TIN {net:.2f} m³)")
    else:
        log("[volume] raster cross-check produced no valid cells")
        unmeasured_area = 0.0

    # ---- T2.2: bootstrap volume CI (rim + block-bootstrapped interior) ----
    # The raster cross-check's fast net (no hole-fill) is only used for the
    # replicate-to-replicate SPREAD, not its absolute level (it carries the
    # same systematic gap from the TIN as above) — bootstrap_volume_ci
    # returns that spread as offsets from ITS OWN median, applied here
    # around the primary (TIN) `net` so a consistent raster/TIN gap can't
    # leak into the reported interval. A coverage term
    # (unmeasured area x the region's own peak height) is added in
    # quadrature (F9): the bootstrap's fixed interior positions can't see
    # the risk that a genuinely unmeasured patch hides real relief, so this
    # is the honest way to fold that specific, unresampled risk in.
    ci = None
    if datum.startswith("rim_") and rim is not None and len(rim) >= 15:
        offsets = bootstrap_volume_ci(interior_pre_cap, rim, up, cap, cap_low,
                                      cell_r, datum, log=log)
        if offsets is not None:
            lo_off, hi_off = offsets
            # A4: prefer the G6 coverage gap (polygon area actually traced
            # minus what the TIN could measure) over the raster's
            # `unmeasured_area` proxy — the raster only flags cells with NO
            # nearby data at all, missing the far larger "bridged, not
            # observed" gap the tighter RC1 bridging cull now excludes from
            # `area`. `mean|h|` (not `max|h|`) so one deep outlier point
            # doesn't dominate a term meant to bound a plausible unseen
            # patch, not a worst-case one.
            if polygon_area_m2 is not None:
                cov_area = max(0.0, polygon_area_m2 - area)
            else:
                cov_area = unmeasured_area
            cov_term = cov_area * float(np.mean(np.abs(h))) if len(h) else 0.0
            lo_off = float(np.hypot(lo_off, cov_term))
            hi_off = float(np.hypot(hi_off, cov_term))
            ci = (net - lo_off, net + hi_off)

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
    result = {
        "net_volume_m3": net,
        "cut_volume_m3": cut,
        "fill_volume_m3": fill,
        "area_m2": area,
        "area_measured_m2": area,
        "bridged_area_m2": bridged_area,
        "cut_measured_m3": cut,
        "cut_upper_m3": cut_upper_m3,
        "volume_raster_m3": r_net if r_area > 0 else None,
        "unmeasured_area_m2": unmeasured_area,
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
    if ci is not None:
        result["net_volume_ci95_m3"] = [ci[0], ci[1]]
    if coverage is not None:
        result["coverage_frac"] = coverage["coverage_frac"]
        result["largest_void_m2"] = coverage["largest_void_m2"]
        result["polygon_area_m2"] = coverage["polygon_area_m2"]
        if coverage["coverage_frac"] < 0.6:
            warnings.append(
                f"only {coverage['coverage_frac']:.0%} of the traced region has "
                "nearby data (coverage gate) — the reported volume interpolates "
                "over a large unobserved area")
    return result

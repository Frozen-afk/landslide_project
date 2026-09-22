"""Ground-frame region selection for photo-mode tracing (T1.2).

`volume.select_region` selects the debris region by projecting the cloud
INTO the marked photo — a polygon drawn on an oblique photo suffers
perspective parallax and (even with the occlusion z-buffer) still measures
in image space. This module instead casts the polygon OUT of the photo, onto
a top-down digital surface model (DSM) of the scene, so photo-mode tracing
selects a polygon in true ground coordinates — the same coordinates
`ortho.select_region_ortho` already uses for orthophoto tracing, and free of
parallax for the same reason.

For each (densified) polygon vertex: build a camera ray in world space, march
it outward, and find where its height first drops to the DSM's surface
height at the ray's own (u, v). Vertices whose ray never crosses the surface
(pointing at open sky, or off the reconstructed footprint) are dropped; if
too many drop, the caller falls back to `volume.select_region`.
"""
from __future__ import annotations

import numpy as np

from .geometry import median_point_spacing, undistort_normalized
from .ortho import ground_basis, select_region_world
from .sfm import ImageView, Log, ReconCtx


def estimate_cell_size(pts: np.ndarray, e1: np.ndarray, e2: np.ndarray,
                       sample: int = 20_000) -> float:
    """Ground-plane DSM cell size (RC2/A2): 4x the cloud's own median point
    spacing, clipped to [0.1, 0.5] m.

    The earlier "grow the cell until the DSM's own occupied-cell fraction
    clears a 50% target" design (F18/M2) measured occupancy over the
    cloud's BOUNDING BOX, not its real footprint: a frustum/marker-board
    footprint never fills its bounding rectangle, so the cell grew to the
    2 m hard cap on every 21-view preset regardless of the cloud's actual
    3-5 cm point spacing (a 16x15 grid for a 36 m scene). At that cell
    size the per-cell max-height binning sat 0.4-0.6 m above the true
    ground on any real slope, which at a typical 19 deg camera elevation
    shifted the ray-cast hit 1.2-1.7 m toward the camera — the dominant
    source of the photo-mode region-selection error this module exists to
    avoid. A small, density-derived cell (this function) combined with
    `fill_dsm_holes` bridging only genuinely small gaps (not "starved
    cell size masquerading as coverage") measures the real surface instead
    of a coarse, biased proxy for it.
    """
    u, v = pts @ e1, pts @ e2
    spacing = median_point_spacing(u, v, sample)
    return float(np.clip(4.0 * spacing, 0.1, 0.5))


def build_dsm(pts: np.ndarray, up: np.ndarray, e1: np.ndarray, e2: np.ndarray,
             cell: float) -> dict:
    """Top-down median-height raster of `pts` (metric, world frame) — A2.

    Returns {"u0", "v0", "cell", "width", "height", "z"} — `z` is the
    (height, width) grid of the MEDIAN point elevation per cell (NaN where
    no point falls in a cell; small holes are simply invisible to the ray
    march unless `fill_dsm_holes` runs first). Median instead of max-height
    (RC2): with the old coarse (up to 2 m) cells, taking the highest point
    per cell systematically sat 0.4-0.6 m above the real ground on any
    slope or surface texture — the fine, density-derived cells from
    `estimate_cell_size` shrink that bias on their own, and the median is
    robust to the odd high outlier (a rock, a stereo floater) a max-height
    DSM cannot be.
    """
    u, v, h = pts @ e1, pts @ e2, pts @ up
    u0, v0 = float(u.min()), float(v.min())
    width = int(np.ceil((u.max() - u0) / cell)) + 1
    height = int(np.ceil((v.max() - v0) / cell)) + 1
    ix = np.clip(((u - u0) / cell).astype(np.int64), 0, width - 1)
    iy = np.clip(((v - v0) / cell).astype(np.int64), 0, height - 1)
    flat = iy * width + ix

    # per-cell median via two lexsorts (see volume._raster_bin) instead of a
    # Python loop over occupied cells — same trick, this DSM is rebuilt on
    # every photo-mode measurement so it has to stay cheap.
    order = np.lexsort((h, flat))
    flat_s, h_s = flat[order], h[order]
    uniq, starts, counts = np.unique(flat_s, return_index=True, return_counts=True)
    lo = starts + (counts - 1) // 2
    hi = starts + counts // 2
    med = 0.5 * (h_s[lo] + h_s[hi])
    z = np.full(width * height, np.nan)
    z[uniq] = med
    return {"u0": u0, "v0": v0, "cell": float(cell),
            "width": width, "height": height, "z": z.reshape(height, width)}


def fill_dsm_holes(dsm: dict, max_dist_m: float = 2.0) -> dict:
    """Fill DSM gaps from the nearest valid cell, for the ray march only (A2).

    Real stereo clouds always have small unmeasured pockets; leaving them
    as NaN makes the ray march skip straight through the true surface
    there. `scipy.ndimage.distance_transform_edt` finds each gap cell's
    nearest cell WITH data and copies its height — but only within
    `max_dist_m`: a gap wider than that is a genuine coverage hole (not
    "the same surface, one cell over") and is correctly left unfilled so
    the ray-cast still reports it as a miss. Returns a new dsm dict; the
    input is not modified.
    """
    from scipy.ndimage import distance_transform_edt

    z = dsm["z"]
    invalid = np.isnan(z)
    if not invalid.any():
        return dsm
    dist, (iy, ix) = distance_transform_edt(invalid, return_indices=True)
    within = (dist * dsm["cell"]) <= max_dist_m
    out = dict(dsm)
    out["z"] = np.where(invalid & within, z[iy, ix], z)
    return out


def _sample_dsm(dsm: dict, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Nearest-cell DSM height at (u, v); NaN outside the grid or in a hole.

    Floor lookup (RC2), matching `build_dsm`'s own floor-binning of (u, v)
    into cells — the previous `np.round` lookup was off by half a cell
    against a floor-binned grid.
    """
    ix = np.floor((u - dsm["u0"]) / dsm["cell"]).astype(np.int64)
    iy = np.floor((v - dsm["v0"]) / dsm["cell"]).astype(np.int64)
    ok = (ix >= 0) & (ix < dsm["width"]) & (iy >= 0) & (iy < dsm["height"])
    out = np.full(len(u), np.nan)
    flat_ok = np.flatnonzero(ok)
    out[flat_ok] = dsm["z"][iy[flat_ok], ix[flat_ok]]
    return out


def _densify_edges(polygon_px: np.ndarray, max_seg_px: float = 25.0) -> np.ndarray:
    """Subdivide straight image-space polygon edges into short segments so
    the (curved, once cast to the ground) edge is approximated well."""
    out = []
    n = len(polygon_px)
    for i in range(n):
        a, b = polygon_px[i], polygon_px[(i + 1) % n]
        length = float(np.linalg.norm(b - a))
        steps = max(1, int(np.ceil(length / max_seg_px)))
        for k in range(steps):
            out.append(a + (b - a) * (k / steps))
    return np.asarray(out, np.float64)


def cast_polygon_to_ground(view: ImageView, polygon_px, dsm: dict,
                           up: np.ndarray, e1: np.ndarray, e2: np.ndarray,
                           scale: float, max_gap_cells: float = 8.0,
                           max_range: float | None = None) -> tuple[np.ndarray, float]:
    """Ray-cast each (densified) polygon vertex from `view` onto the DSM.

    `scale` converts the (model-unit) camera pose to the DSM's metric frame:
    scaling is an isotropic similarity with no rotation, so the ray
    DIRECTION is unchanged and only the camera CENTER needs scaling.

    Step size is tied to the (density-adaptive) DSM cell, `cell / 2`, so
    resolution along the ray always matches the grid instead of a fixed
    step count under/over-sampling a coarse or fine DSM (F18). A NaN run
    along the ray no longer aborts the crossing search: the sign flip is
    accepted across a gap of up to `max_gap_cells` cells between the last
    valid sample before it and the first valid sample after — a small,
    genuinely-unmeasured pocket (there's no such thing as a perfectly
    dense real stereo cloud) shouldn't make the ray skip straight through
    the true surface undetected. `fill_dsm_holes` already bridges the
    small enclosed gaps directly; this is the residual tolerance for gaps
    that reach the ray but weren't enclosed for the DSM fill.

    Returns (ground_polygon (M, 2) in (e1, e2) coordinates, hit_fraction,
    longest_miss_run) — hit_fraction is the share of densified vertices
    whose ray actually crossed the surface; longest_miss_run (G5) is the
    longest CIRCULAR run of consecutive vertices that missed, which a flat
    hit_fraction hides — e.g. descending's boundary misses in two runs of
    6 and 5 (of 87) rather than scattered singletons, meaning one whole
    stretch of the polygon closes across an unmeasured gap instead of many
    small, harmless single-vertex misses spread around the ring. A vertex
    whose ray points at open sky, or leaves the DSM footprint before
    crossing it, is dropped.
    """
    px = _densify_edges(np.asarray(polygon_px, np.float64))
    uv_n = undistort_normalized(px, view.K, view.dist)          # (M, 2)
    d_cam = np.column_stack([uv_n, np.ones(len(uv_n))])
    d_cam /= np.linalg.norm(d_cam, axis=1, keepdims=True)
    d_world = d_cam @ view.R                                    # (M, 3) unit
    C = view.center * scale

    if max_range is None:
        max_range = 3.0 * max(dsm["cell"] * max(dsm["width"], dsm["height"]), 1.0)
    step = dsm["cell"] * 0.5
    n_steps = int(np.ceil(max_range / step)) + 1
    s = np.linspace(step, max_range, n_steps)
    Xs = C[None, None, :] + s[None, :, None] * d_world[:, None, :]   # (M, S, 3)
    h = Xs @ up
    u = Xs @ e1
    v = Xs @ e2
    H = _sample_dsm(dsm, u.reshape(-1), v.reshape(-1)).reshape(h.shape)
    diff = h - H                                                # >0 above surface

    max_gap = max_gap_cells * dsm["cell"]
    hits_u, hits_v, ok = [], [], []
    for i in range(len(px)):
        d = diff[i]
        valid = np.isfinite(d)
        cross = None
        prev_j = None
        for j in np.flatnonzero(valid):
            if (prev_j is not None and d[prev_j] > 0 >= d[j] and
                    (s[j] - s[prev_j]) <= max_gap):
                t = d[prev_j] / (d[prev_j] - d[j])
                s_hit = s[prev_j] + t * (s[j] - s[prev_j])
                cross = C + s_hit * d_world[i]
                break
            prev_j = j
        if cross is None:
            ok.append(False)
            continue
        hits_u.append(float(cross @ e1))
        hits_v.append(float(cross @ e2))
        ok.append(True)
    ok = np.asarray(ok, dtype=bool)
    ground = np.column_stack([hits_u, hits_v]) if hits_u else np.zeros((0, 2))
    hit_frac = float(ok.mean()) if len(ok) else 0.0
    return ground, hit_frac, _longest_miss_run(ok)


def _longest_miss_run(ok: np.ndarray) -> int:
    """Longest run of consecutive False in a CIRCULAR boolean array (G5)."""
    n = len(ok)
    if n == 0 or ok.all():
        return 0
    if not ok.any():
        return n
    rolled = np.roll(ok, -int(np.argmax(ok)))   # start on a hit: no wrap-split run
    best = cur = 0
    for v in rolled:
        cur = 0 if v else cur + 1
        best = max(best, cur)
    return int(best)


def select_region_ground(ctx: ReconCtx, image_name: str, polygon_px,
                         up: np.ndarray | None = None,
                         min_hit_frac: float = 0.5, log: Log = print,
                         dense: bool = True):
    """Ground-frame counterpart of `volume.select_region`.

    Returns None (caller should fall back to image-plane selection) when the
    ray-cast fails for more than `1 - min_hit_frac` of the (densified)
    polygon vertices, or the DSM/cloud isn't usable yet. On success, returns
    (interior_mask, rim_mask, info) where info carries the ground polygon,
    hit fraction and rim band for the result/UI. `dense` must match the
    cloud the caller will apply the returned masks to.

    `min_hit_frac` dropped from the original 0.7 (F18/M2): a steep/oblique
    capture path (walking downhill, grazing angles) genuinely leaves part
    of the DSM footprint unresolved by any cell size, capping the
    achievable hit fraction below 0.7 on that geometry — the parallax-free
    ground-frame selection is still measurably more accurate than the
    image-plane fallback at that reduced coverage, so 0.7 was rejecting
    the better answer more often than a real footprint failure.
    `test_cast_polygon_reports_low_hit_fraction_off_footprint` still
    enforces a real off-footprint case falls well under this. This is only
    the fallback threshold; `pipeline.measure`'s G5 gate (RC2/A3) separately
    reports the achieved `hit_frac` as `status="indicative"` below 0.85 and
    reflects a low value in the result even when ground-frame selection was
    used.
    """
    from .densify import estimate_up

    view = ctx.views[image_name]
    pts, _ = ctx.cloud(dense=dense)
    if len(pts) < 200:
        return None
    if up is None:
        up = estimate_up(ctx.views, ctx.sparse, log=log)
    e1, e2 = ground_basis(up)
    pts_metric = np.asarray(pts, np.float64) * ctx.scale
    cell = estimate_cell_size(pts_metric, e1, e2)
    dsm = fill_dsm_holes(build_dsm(pts_metric, up, e1, e2, cell))
    ground_poly, hit_frac, max_miss_run = cast_polygon_to_ground(
        view, polygon_px, dsm, up, e1, e2, ctx.scale)
    if hit_frac < min_hit_frac or len(ground_poly) < 3:
        log(f"[ground] ray-cast hit only {hit_frac:.0%} of the traced boundary "
            "— falling back to image-plane selection")
        return None
    interior, rim, rinfo = select_region_world(ctx, e1, e2, ground_poly, log=log,
                                               dense=dense)
    rinfo["hit_frac"] = hit_frac
    rinfo["max_miss_run"] = max_miss_run
    rinfo["ground_polygon"] = ground_poly.tolist()
    return interior, rim, rinfo

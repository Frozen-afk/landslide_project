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

from .geometry import undistort_normalized
from .ortho import ground_basis, select_region_world
from .sfm import ImageView, Log, ReconCtx


def estimate_cell_size(pts: np.ndarray, e1: np.ndarray, e2: np.ndarray,
                       sample: int = 20_000) -> float:
    """Ground-plane DSM cell size: 2.5x the cloud's own point spacing."""
    from scipy.spatial import cKDTree

    uv = np.column_stack([pts @ e1, pts @ e2])
    sub = uv[:: max(1, len(uv) // sample)]
    if len(sub) < 3:
        return 0.1
    d, _ = cKDTree(sub).query(sub, k=2, workers=-1)
    spacing = float(np.median(d[:, 1]))
    return float(np.clip(2.5 * spacing, 0.02, 1.0))


def build_dsm(pts: np.ndarray, up: np.ndarray, e1: np.ndarray, e2: np.ndarray,
             cell: float) -> dict:
    """Top-down max-height raster of `pts` (metric, world frame).

    Returns {"u0", "v0", "cell", "width", "height", "z"} — `z` is the
    (height, width) grid of the highest point's elevation per cell (NaN
    where no point falls in a cell; small holes are simply invisible to the
    ray march, not filled).
    """
    u, v, h = pts @ e1, pts @ e2, pts @ up
    u0, v0 = float(u.min()), float(v.min())
    width = int(np.ceil((u.max() - u0) / cell)) + 1
    height = int(np.ceil((v.max() - v0) / cell)) + 1
    ix = np.clip(((u - u0) / cell).astype(np.int64), 0, width - 1)
    iy = np.clip(((v - v0) / cell).astype(np.int64), 0, height - 1)
    flat = iy * width + ix
    z = np.full(width * height, -np.inf)
    np.maximum.at(z, flat, h)
    z[np.isneginf(z)] = np.nan
    return {"u0": u0, "v0": v0, "cell": float(cell),
            "width": width, "height": height, "z": z.reshape(height, width)}


def _sample_dsm(dsm: dict, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Nearest-cell DSM height at (u, v); NaN outside the grid or in a hole."""
    ix = np.round((u - dsm["u0"]) / dsm["cell"]).astype(np.int64)
    iy = np.round((v - dsm["v0"]) / dsm["cell"]).astype(np.int64)
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
                           scale: float, n_steps: int = 400,
                           max_range: float | None = None) -> tuple[np.ndarray, float]:
    """Ray-cast each (densified) polygon vertex from `view` onto the DSM.

    `scale` converts the (model-unit) camera pose to the DSM's metric frame:
    scaling is an isotropic similarity with no rotation, so the ray
    DIRECTION is unchanged and only the camera CENTER needs scaling.

    Returns (ground_polygon (M, 2) in (e1, e2) coordinates, hit_fraction) —
    the share of densified vertices whose ray actually crossed the surface.
    A vertex whose ray points at open sky, or leaves the DSM footprint
    before crossing it, is dropped.
    """
    px = _densify_edges(np.asarray(polygon_px, np.float64))
    uv_n = undistort_normalized(px, view.K, view.dist)          # (M, 2)
    d_cam = np.column_stack([uv_n, np.ones(len(uv_n))])
    d_cam /= np.linalg.norm(d_cam, axis=1, keepdims=True)
    d_world = d_cam @ view.R                                    # (M, 3) unit
    C = view.center * scale

    if max_range is None:
        max_range = 3.0 * max(dsm["cell"] * max(dsm["width"], dsm["height"]), 1.0)
    s = np.linspace(dsm["cell"] * 0.5, max_range, n_steps)
    Xs = C[None, None, :] + s[None, :, None] * d_world[:, None, :]   # (M, S, 3)
    h = Xs @ up
    u = Xs @ e1
    v = Xs @ e2
    H = _sample_dsm(dsm, u.reshape(-1), v.reshape(-1)).reshape(h.shape)
    diff = h - H                                                # >0 above surface

    hits_u, hits_v, ok = [], [], []
    for i in range(len(px)):
        d = diff[i]
        valid = np.isfinite(d)
        cross = None
        prev_j = None
        for j in np.flatnonzero(valid):
            if prev_j is not None and j == prev_j + 1 and d[prev_j] > 0 >= d[j]:
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
    return ground, hit_frac


def select_region_ground(ctx: ReconCtx, image_name: str, polygon_px,
                         up: np.ndarray | None = None,
                         min_hit_frac: float = 0.7, log: Log = print,
                         dense: bool = True):
    """Ground-frame counterpart of `volume.select_region`.

    Returns None (caller should fall back to image-plane selection) when the
    ray-cast fails for more than `1 - min_hit_frac` of the (densified)
    polygon vertices, or the DSM/cloud isn't usable yet. On success, returns
    (interior_mask, rim_mask, info) where info carries the ground polygon,
    hit fraction and rim band for the result/UI. `dense` must match the
    cloud the caller will apply the returned masks to.
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
    dsm = build_dsm(pts_metric, up, e1, e2, cell)
    ground_poly, hit_frac = cast_polygon_to_ground(
        view, polygon_px, dsm, up, e1, e2, ctx.scale)
    if hit_frac < min_hit_frac or len(ground_poly) < 3:
        log(f"[ground] ray-cast hit only {hit_frac:.0%} of the traced boundary "
            "— falling back to image-plane selection")
        return None
    interior, rim, rinfo = select_region_world(ctx, e1, e2, ground_poly, log=log,
                                               dense=dense)
    rinfo["hit_frac"] = hit_frac
    rinfo["ground_polygon"] = ground_poly.tolist()
    return interior, rim, rinfo

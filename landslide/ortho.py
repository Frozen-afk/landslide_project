"""Top-down orthophoto: render the point cloud as a bird's-eye image and
select the measurement region in ground coordinates.

Tracing a boundary on an oblique perspective photo suffers parallax: a line
drawn across background terrain can accidentally enclose points far behind
the landslide. Projecting the (already metric-scaled) cloud along the scene's
vertical onto a raster gives an orthorectified view whose pixel grid maps
affinely to ground (e1, e2) coordinates — a polygon drawn there selects
exactly the volume under it, with zero perspective foreshortening.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .geometry import points_in_polygon, ring_distance
from .sfm import Log, ReconCtx


def ground_basis(up) -> tuple[np.ndarray, np.ndarray]:
    """Two orthonormal in-plane axes (e1, e2) for a ground normal `up`."""
    up = np.asarray(up, np.float64)
    up = up / np.linalg.norm(up)
    a = np.array([1.0, 0.0, 0.0])
    if abs(a @ up) > 0.9:
        a = np.array([0.0, 1.0, 0.0])
    e1 = a - (a @ up) * up
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    return e1, e2


def _scale_bar(img: np.ndarray, res: float) -> None:
    """Draw a ground-true length bar (bottom-left) on the ortho image."""
    h, w = img.shape[:2]
    for length in (0.5, 1, 2, 5, 10, 20, 50):
        px = length / res
        if 60 <= px <= min(240, w * 0.4):
            x0, y0 = 16, h - 20
            cv2.rectangle(img, (x0, y0 - 3), (x0 + int(px), y0 + 3),
                          (255, 255, 255), -1)
            cv2.putText(img, f"{length:g} m", (x0, y0 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            return


def render_orthophoto(ctx: ReconCtx, up=None, max_side: int = 1400,
                      jpg_path=None, meta_path=None, log: Log = print):
    """Rasterize the metric point cloud top-down. Returns (image, meta).

    Each pixel keeps the color of the highest point along `up` (the visible
    surface). `meta` maps pixels to ground coordinates:
        ground_u = u0 + col * res,  ground_v = v0 + row * res.
    """
    from .densify import estimate_up

    pts, cols = ctx.cloud(dense=True)
    pts = np.asarray(pts, np.float64) * ctx.scale
    if len(pts) < 200:
        raise RuntimeError("too few 3D points for an orthophoto — the "
                           "reconstruction is too sparse")
    if up is None:
        up = estimate_up(ctx.views, ctx.sparse)
    e1, e2 = ground_basis(up)
    u, v, h = pts @ e1, pts @ e2, pts @ up

    u0, v0 = float(u.min()), float(v.min())
    span = max(float(u.max() - u0), float(v.max() - v0))
    res = span / max_side
    width = int(np.ceil((u.max() - u0) / res)) + 1
    height = int(np.ceil((v.max() - v0) / res)) + 1
    ix = np.clip(((u - u0) / res).astype(np.int64), 0, width - 1)
    iy = np.clip(((v - v0) / res).astype(np.int64), 0, height - 1)

    order = np.argsort(h)                    # ascending: last write per pixel
    img = np.full((height, width, 3), (24, 28, 34), np.uint8)   # BGR dark
    img[iy[order], ix[order]] = np.asarray(cols, np.uint8)[order]
    _scale_bar(img, res)

    if jpg_path is not None:
        cv2.imwrite(str(jpg_path), img)
    meta = {
        "u0": u0, "v0": v0, "res": float(res),
        "width": width, "height": height,
        "up": np.asarray(up, np.float64).tolist(),
        "e1": e1.tolist(), "e2": e2.tolist(),
    }
    if meta_path is not None:
        Path(meta_path).write_text(json.dumps(meta))
    covered = int(len(np.unique(iy * width + ix)))
    log(f"[ortho] {width}x{height} px at {res * 100:.2f} cm/px, "
        f"{covered} / {width * height} cells covered by {len(pts)} points")
    return img, meta


def select_region_ortho(ctx: ReconCtx, meta: dict, polygon_px,
                        rim_inner_m: float | None = None,
                        rim_outer_m: float | None = None,
                        log: Log = print):
    """Interior/rim masks of the cloud from a polygon drawn on the orthophoto.

    The rim band is an annulus in true ground units outside the traced line,
    wide enough to hold many cloud points but clear of debris the user may
    have clipped with a slightly-inward trace. Band defaults adapt to the
    cloud's point spacing.
    """
    pts, _ = ctx.cloud(dense=True)
    pts = np.asarray(pts, np.float64) * ctx.scale
    e1 = np.asarray(meta["e1"], np.float64)
    e2 = np.asarray(meta["e2"], np.float64)
    uv = np.column_stack([pts @ e1, pts @ e2])

    poly = np.asarray(polygon_px, np.float64)
    world = np.column_stack([meta["u0"] + poly[:, 0] * meta["res"],
                             meta["v0"] + poly[:, 1] * meta["res"]])

    if rim_outer_m is None or rim_inner_m is None:
        sub = uv[:: max(1, len(uv) // 20000)]
        dd, _ = cKDTree(sub).query(sub, k=2, workers=-1)
        spacing = float(np.median(dd[:, 1]))
        if rim_outer_m is None:
            rim_outer_m = float(np.clip(30.0 * spacing, 0.4, 2.5))
        if rim_inner_m is None:
            rim_inner_m = float(np.clip(6.0 * spacing, 0.08, 0.5))

    interior = points_in_polygon(uv, world)
    d = ring_distance(uv, world)
    rim = (d >= rim_inner_m) & (d <= rim_outer_m) & ~interior
    log(f"[ortho] region: {int(interior.sum())} points inside, "
        f"{int(rim.sum())} in the rim band "
        f"[{rim_inner_m:.2f}, {rim_outer_m:.2f}] m outside the line")
    return interior, rim, {"rim_inner_m": float(rim_inner_m),
                           "rim_outer_m": float(rim_outer_m)}

"""Artifact rendering: photo overlays and a top-down height-difference map."""
from __future__ import annotations

import cv2
import numpy as np

GREEN = (60, 255, 60)
RED = (60, 80, 255)
YELLOW = (60, 220, 255)


def draw_overlay_image(image_path, polygon, marker_pts=None, out_path=None,
                       max_side: int = 1600):
    """Draw the polygon (and reference clicks) on any photo/orthophoto."""
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        raise IOError(f"cannot read {image_path}")
    s = min(1.0, max_side / max(img.shape[:2]))
    if s < 1.0:
        img = cv2.resize(img, None, fx=s, fy=s)
    poly = np.asarray(polygon, np.float64) * s
    if len(poly) >= 2:
        pts = poly.reshape(-1, 1, 2).astype(np.int32)
        cv2.polylines(img, [pts], isClosed=True, color=YELLOW, thickness=3)
        cv2.polylines(img, [pts], isClosed=True, color=(0, 0, 0), thickness=1)
    if marker_pts is not None and len(marker_pts):
        for p in np.asarray(marker_pts, np.float64).reshape(-1, 2) * s:
            cv2.circle(img, (int(p[0]), int(p[1])), 8, RED, 2)
            cv2.circle(img, (int(p[0]), int(p[1])), 2, RED, -1)
    if out_path is not None:
        cv2.imwrite(str(out_path), img)
    return img


def draw_overlay(view, polygon, marker_pts=None, out_path=None,
                 max_side: int = 1600):
    return draw_overlay_image(view.path, polygon, marker_pts, out_path, max_side)


def heat_topdown(uv2: np.ndarray, h: np.ndarray, out_path,
                 title: str = "Height difference to datum plane (m)") -> None:
    """Top-down map of the in-polygon surface, colored by height to datum."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.tri import Triangulation

    fig, ax = plt.subplots(figsize=(7, 6), dpi=130)
    order = np.argsort(h)
    tri = Triangulation(uv2[order, 0], uv2[order, 1])
    tc = ax.tripcolor(tri, h[order], cmap="RdYlBu_r", shading="gouraud")
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.set_xlabel("east-ish (m)")
    ax.set_ylabel("north-ish (m)")
    fig.colorbar(tc, ax=ax, label="surface − datum (m)")
    fig.tight_layout()
    fig.savefig(str(out_path))
    plt.close(fig)


def _slope_grid(uv2: np.ndarray, z: np.ndarray, min_cell_pts: int = 3):
    """Bin points to a robust grid; returns (extent, cell, slope_deg grid).

    Per-triangle gradients amplify point noise on thin triangles into ~90°
    spikes; mean height over >=3 points per cell plus central differences
    over the cell size is stable at the decimeter scale.
    """
    from scipy.spatial import cKDTree

    d_self, _ = cKDTree(uv2).query(uv2, k=2, workers=-1)
    spacing = float(np.median(d_self[:, 1]))
    cell = float(np.clip(2.5 * spacing, 0.05, 1.0))
    x0, y0 = uv2[:, 0].min(), uv2[:, 1].min()
    nx = int(np.ceil(np.ptp(uv2[:, 0]) / cell)) + 1
    ny = int(np.ceil(np.ptp(uv2[:, 1]) / cell)) + 1
    ix = np.clip(((uv2[:, 0] - x0) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((uv2[:, 1] - y0) / cell).astype(int), 0, ny - 1)
    flat = iy * nx + ix
    counts = np.bincount(flat, minlength=nx * ny)
    sums = np.bincount(flat, weights=z, minlength=nx * ny)
    ok = counts >= min_cell_pts
    grid = np.full(nx * ny, np.nan)
    grid[ok] = sums[ok] / counts[ok]
    g = grid.reshape(ny, nx)
    gy, gx = np.gradient(g, cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    return (x0, y0, x0 + nx * cell, y0 + ny * cell), cell, slope


def heat_slope(uv2: np.ndarray, z: np.ndarray, out_path,
               steep_deg: float = 35.0, moderate_deg: float = 25.0) -> float:
    """Slope-hazard map of the in-polygon surface (green/yellow/red).

    z is per-point surface height above the datum plane. Green < moderate_deg
    is stable deposition, yellow to steep_deg is a moderate slope, red >
    steep_deg is an over-steepened scarp/debris face at secondary-slide risk
    (35-42° is the angle of repose of loose rock/soil). Returns the area
    share steeper than steep_deg.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    extent, cell, slope = _slope_grid(uv2, z)
    valid = np.isfinite(slope)
    if not valid.any():
        return 0.0
    steep_share = float((valid & (slope > steep_deg)).sum()
                        / max(valid.sum(), 1))

    fig, ax = plt.subplots(figsize=(7, 6), dpi=130)
    cmap = ListedColormap(["#2e9e4f", "#e8c229", "#d2322c"])
    norm = BoundaryNorm([0, moderate_deg, steep_deg, 90], cmap.N)
    masked = np.ma.masked_invalid(slope)
    im = ax.imshow(masked, origin="lower", extent=extent, cmap=cmap,
                   norm=norm, interpolation="nearest", aspect="equal")
    ax.set_title(f"Slope hazard (red > {steep_deg:.0f}°: secondary-slide risk)")
    ax.set_xlabel("east-ish (m)")
    ax.set_ylabel("north-ish (m)")
    cb = fig.colorbar(im, ax=ax, label="surface slope (°)",
                      ticks=[moderate_deg / 2,
                             (moderate_deg + steep_deg) / 2,
                             (steep_deg + 90) / 2])
    cb.ax.set_yticklabels([f"<{moderate_deg:.0f}° stable",
                           f"{moderate_deg:.0f}–{steep_deg:.0f}° moderate",
                           f">{steep_deg:.0f}° over-steepened"])
    fig.tight_layout()
    fig.savefig(str(out_path))
    plt.close(fig)
    return steep_share

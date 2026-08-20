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

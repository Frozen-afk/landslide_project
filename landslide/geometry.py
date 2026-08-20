"""Pure geometry helpers (no COLMAP dependencies)."""
from __future__ import annotations

import cv2
import numpy as np
from matplotlib.path import Path as MplPath


def camera_center(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Camera center for world->cam map x_cam = R @ x_world + t."""
    return -(R.T @ t)


def undistort_normalized(pixels, K, dist) -> np.ndarray:
    """Pixel coordinates -> undistorted normalized camera coords (N, 2)."""
    pts = np.asarray(pixels, np.float64).reshape(-1, 1, 2)
    out = cv2.undistortPoints(pts, np.asarray(K, np.float64), np.asarray(dist, np.float64).ravel())
    return out.reshape(-1, 2)


def triangulate_dlt(views) -> np.ndarray:
    """Least-squares multi-view triangulation (DLT).

    views: list of (R (3,3), t (3,), uv_normalized (N,2)) — all with equal N.
    Returns world points X (N, 3).
    """
    n_pts = len(views[0][2])
    X = np.empty((n_pts, 3))
    projs = [np.hstack([np.asarray(R, np.float64), np.asarray(t, np.float64).reshape(3, 1)])
             for R, t, _ in views]
    for i in range(n_pts):
        rows = []
        for (R, t, uv), M in zip(views, projs):
            u, v = uv[i]
            rows.append(u * M[2] - M[0])
            rows.append(v * M[2] - M[1])
        _, _, Vt = np.linalg.svd(np.asarray(rows))
        x = Vt[-1]
        X[i] = x[:3] / x[3]
    return X


def points_in_polygon(pts2d, polygon) -> np.ndarray:
    """Boolean mask of 2D points strictly inside polygon (K, 2)."""
    path = MplPath(np.asarray(polygon, np.float64))
    return path.contains_points(np.asarray(pts2d, np.float64))


def ring_distance(pts2d, polygon) -> np.ndarray:
    """Distance of each 2D point to the polygon boundary (K,)."""
    pts = np.asarray(pts2d, np.float64)
    poly = np.asarray(polygon, np.float64)
    dmin = np.full(len(pts), np.inf)
    for a, b in zip(poly, np.roll(poly, -1, axis=0)):
        ab = b - a
        L2 = float(ab @ ab)
        if L2 == 0.0:
            d = np.linalg.norm(pts - a, axis=1)
        else:
            tpar = np.clip(((pts - a) @ ab) / L2, 0.0, 1.0)
            proj = a + tpar[:, None] * ab
            d = np.linalg.norm(pts - proj, axis=1)
        dmin = np.minimum(dmin, d)
    return dmin


def polygon_area(polygon) -> float:
    """Absolute shoelace area of a closed polygon (K, 2)."""
    p = np.asarray(polygon, np.float64)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))

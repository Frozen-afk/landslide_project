import numpy as np
import pytest

import sys; sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from landslide.geometry import (points_in_polygon, polygon_area, ring_distance,
                                triangulate_dlt, undistort_normalized)


def _make_cam(eye, target, up=(0, 0, 1)):
    # CV convention: x right, y down, z forward (x_cam = R (X - eye))
    zc = np.asarray(target) - np.asarray(eye); zc /= np.linalg.norm(zc)
    xc = np.cross(zc, up); xc /= np.linalg.norm(xc)
    yc = np.cross(zc, xc)
    R = np.stack([xc, yc, zc]); t = -R @ np.asarray(eye)
    return R, t


def test_triangulate_dlt_exact():
    rng = np.random.default_rng(0)
    pts = rng.normal(size=(6, 3)) * 5.0
    cams = [_make_cam(np.array([x, -10, 3]), np.zeros(3)) for x in (-4, 0, 4)]
    K = np.array([[800.0, 0, 320], [0, 800, 240], [0, 0, 1]])
    views = []
    for R, t in cams:
        Pc = pts @ R.T + t
        uv = np.stack([K[0, 0] * Pc[:, 0] / Pc[:, 2] + K[0, 2],
                       K[1, 1] * Pc[:, 1] / Pc[:, 2] + K[1, 2]], 1)
        # pixels -> normalized (pinhole: divide by K)
        xn = (uv - K[:2, 2]) / np.array([K[0, 0], K[1, 1]])
        views.append((R, t, xn))
    X = triangulate_dlt(views)
    assert np.allclose(X, pts, atol=1e-6)


def test_triangulate_dlt_noisy():
    rng = np.random.default_rng(1)
    pts = rng.normal(size=(200, 3)) * 5.0
    cams = [_makecam_local(e) for e in [(-4, -10, 3), (4, -10, 3), (0, -12, 5)]]
    K = np.array([[800.0, 0, 320], [0, 800, 240], [0, 0, 1]])
    views = []
    for R, t in cams:
        Pc = pts @ R.T + t
        uv = np.stack([K[0, 0] * Pc[:, 0] / Pc[:, 2] + K[0, 2],
                       K[1, 1] * Pc[:, 1] / Pc[:, 2] + K[1, 2]], 1)
        uv += rng.normal(0, 0.7, uv.shape)  # ~0.7 px click noise
        xn = (uv - K[:2, 2]) / np.array([K[0, 0], K[1, 1]])
        views.append((R, t, xn))
    X = triangulate_dlt(views)
    err = np.linalg.norm(X - pts, axis=1)
    assert np.median(err) < 0.05  # sub-5 cm with 3 views, 0.7 px noise


def _makecam_local(eye):
    return _make_cam(np.asarray(eye, float), np.zeros(3))


def test_points_in_polygon_and_ring():
    poly = np.array([[0, 0], [10, 0], [10, 10], [0, 10.0]])
    pts = np.array([[5, 5], [15, 5], [0, 0.05], [5, 10.2]])
    inside = points_in_polygon(pts, poly)
    assert inside.tolist() == [True, False, True, False]
    d = ring_distance(pts, poly)
    assert d[0] == pytest.approx(5.0)
    assert d[1] == pytest.approx(5.0)
    assert d[3] == pytest.approx(0.2, abs=1e-6)
    assert polygon_area(poly) == pytest.approx(100.0)


def test_undistort_normalized_identity():
    K = np.array([[1000.0, 0, 500], [0, 1000, 400], [0, 0, 1]])
    dist = np.zeros(5)
    px = np.array([[500.0, 400.0], [600.0, 450.0]])
    out = undistort_normalized(px, K, dist)
    expect = (px - K[:2, 2]) / np.array([K[0, 0], K[1, 1]])
    assert np.allclose(out, expect, atol=1e-9)

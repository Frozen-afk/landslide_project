"""Scaling quality gates with synthetic cameras and reference observations."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest

from landslide import scaling
from landslide.scaling import aruco_scale, manual_scale
from landslide.sfm import ImageView

K = np.array([[800.0, 0, 640], [0, 800.0, 480], [0, 0, 1]])
DIST = np.zeros(5)


def make_view(name, image_id, center):
    """Identity-rotation camera at `center` looking along +z."""
    R = np.eye(3)
    t = -np.asarray(center, np.float64)
    return ImageView(name=name, image_id=image_id, camera_id=1, R=R, t=t,
                     K=K, dist=DIST, width=1280, height=960, path=None)


def project(v, X):
    Xc = np.asarray(X, np.float64) @ v.R.T + v.t
    return np.stack([K[0, 0] * Xc[:, 0] / Xc[:, 2] + K[0, 2],
                     K[1, 1] * Xc[:, 1] / Xc[:, 2] + K[1, 2]], 1)


def make_ctx(views):
    return SimpleNamespace(views=views, scale=1.0, scale_info={})


A = make_view("A", 1, (0.0, 0, 0))
B = make_view("B", 2, (3.0, 1.2, 0))     # offset in y too: clicked rays are
X = np.array([[0.0, 0.1, 6.0], [0.6, 0.1, 6.0]])   # generically skew, so
LENGTH = 0.6                                        # misclicks show up as
                                                    # reprojection residuals
def specs(uvA, uvB, name_a="A", name_b="B"):
    return ({"image": name_a, "p1": uvA[0].tolist(), "p2": uvA[1].tolist()},
            {"image": name_b, "p1": uvB[0].tolist(), "p2": uvB[1].tolist()})


def test_manual_scale_clean_clicks():
    ctx = make_ctx({"A": A, "B": B})
    sa, sb = specs(project(A, X), project(B, X))
    info = manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)
    assert abs(info["scale"] - 1.0) < 1e-6        # model == world here
    assert info["reproj_px_mean"] < 0.5
    assert info["angle_deg"] > 5
    assert not info["warnings"]
    assert 0 < info["scale_rel_error"] <= 0.02    # clamped floor, tiny


def test_manual_scale_rejects_bad_clicks():
    ctx = make_ctx({"A": A, "B": B})
    uvA, uvB = project(A, X), project(B, X).copy()
    uvB[:, 0] += 100.0                            # wrong object in photo B
    sa, sb = specs(uvA, uvB)
    with pytest.raises(RuntimeError, match="don't match"):
        manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)


def test_manual_scale_flags_sloppy_clicks():
    ctx = make_ctx({"A": A, "B": B})
    uvA, uvB = project(A, X), project(B, X).copy()
    uvB[0, 0] += 30.0                             # one endpoint 30 px off
    sa, sb = specs(uvA, uvB)
    info = manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)
    # not rejected, but the scale-accuracy estimate must absorb the sloppiness
    assert info["scale_rel_error"] > 0.02


def test_manual_scale_rejects_parallel_views():
    Bp = make_view("B", 2, (0.02, 0, 0))
    ctx = make_ctx({"A": A, "B": Bp})
    sa, sb = specs(project(A, X), project(Bp, X))
    with pytest.raises(RuntimeError, match="same direction"):
        manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)


def test_manual_scale_rejects_same_photo():
    ctx = make_ctx({"A": A, "B": B})
    uv = project(A, X)
    sa, sb = specs(uv, uv, name_a="A", name_b="A")
    with pytest.raises(ValueError, match="DIFFERENT"):
        manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)


@pytest.fixture
def prior_ctx():
    ctx = make_ctx({"A": make_view("A", 1, (0, 0, 0)),
                    "B": make_view("B", 2, (3, 1.2, 0))})
    ctx.scale = 2.5
    info = {"applied": True, "method": "previous", "scale": 2.5}
    ctx.scale_info = info
    yield ctx
    assert ctx.scale == 2.5
    assert ctx.scale_info is info
    assert info == {"applied": True, "method": "previous", "scale": 2.5}


@pytest.fixture(params=["manual", "aruco"])
def reference_case(request, monkeypatch, prior_ctx):
    ctx = prior_ctx
    points = X.copy()
    if request.param == "aruco":
        points = np.vstack([X, X[::-1] + [0, LENGTH, 0]])
    pixels = {name: project(v, points) for name, v in ctx.views.items()}
    if request.param == "manual":
        def apply(length=LENGTH):
            return manual_scale(ctx, *specs(pixels["A"], pixels["B"]),
                                length, log=lambda *_: None)
    else:
        monkeypatch.setattr(scaling, "_available_dicts", lambda _: ["DICT_6X6_250"])
        for name, view in ctx.views.items():
            view.path = name
        monkeypatch.setattr(scaling, "_load_gray", lambda path: (None, path, 1.0))
        monkeypatch.setattr(scaling, "detect_marker_corners",
                            lambda gray, *args: {7: pixels[gray]})

        def apply(length=LENGTH):
            return aruco_scale(ctx, length, log=lambda *_: None)
    return ctx, points, pixels, apply


@pytest.mark.parametrize("length", [0, -1, np.nan, np.inf, -np.inf])
def test_scale_rejects_invalid_physical_length(reference_case, length):
    _, _, _, apply = reference_case
    with pytest.raises(ValueError, match="finite and positive"):
        apply(length)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_scale_rejects_nonfinite_pixels(reference_case, bad):
    _, _, pixels, apply = reference_case
    pixels["B"][-1, 0] = bad
    with pytest.raises(ValueError, match="coordinates must be finite"):
        apply()


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_scale_rejects_nonfinite_triangulation(reference_case, monkeypatch, bad):
    _, points, _, apply = reference_case
    points[-1, 0] = bad
    monkeypatch.setattr(scaling, "_triangulate_pixels", lambda *args: points)
    with pytest.raises(RuntimeError, match="nonfinite"):
        apply()


@pytest.mark.parametrize("depth", [0, -1, np.nan, np.inf, -np.inf])
def test_scale_rejects_invalid_camera_depth(reference_case, monkeypatch, depth):
    ctx, _, _, apply = reference_case
    original = ctx.views["B"].project

    def invalid_depth(points):
        uv, depths = original(points)
        depths[-1] = depth
        return uv, depths

    monkeypatch.setattr(ctx.views["B"], "project", invalid_depth)
    with pytest.raises(RuntimeError, match="finite positive depth"):
        apply()


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_scale_rejects_nonfinite_reprojection(reference_case, monkeypatch, bad):
    ctx, _, _, apply = reference_case
    original = ctx.views["B"].project

    def invalid_projection(points):
        uv, depth = original(points)
        uv[-1, 0] = bad
        return uv, depth

    monkeypatch.setattr(ctx.views["B"], "project", invalid_projection)
    with pytest.raises(RuntimeError, match="reprojection produced nonfinite"):
        apply()


def test_scale_rejects_points_behind_only_second_camera(reference_case):
    ctx, points, pixels, apply = reference_case
    # Positive world z is not sufficient: this camera looks away from the points.
    ctx.views["B"].R = np.diag([-1.0, 1.0, -1.0])
    pixels["B"][:] = project(ctx.views["B"], points)
    with pytest.raises(RuntimeError, match="finite positive depth"):
        apply()


def test_manual_scale_rejects_reversed_endpoints_with_zero_residual(prior_ctx):
    ctx = prior_ctx
    ctx.views["B"] = make_view("B", 2, (0.3, 0, 0))
    sa, sb = specs(project(ctx.views["A"], X), project(ctx.views["B"], X)[::-1])
    obs = [(s["image"], np.array([s["p1"], s["p2"]])) for s in (sa, sb)]
    points = scaling._triangulate_pixels(obs, ctx.views)
    assert np.any(points[:, 2] < 0)
    for name, pixels in obs:
        uv, _ = ctx.views[name].project(points)
        np.testing.assert_allclose(uv, pixels, atol=1e-9)
    with pytest.raises(RuntimeError, match="finite positive depth"):
        manual_scale(ctx, sa, sb, LENGTH, log=lambda *_: None)


def test_aruco_scale_clean_corners(monkeypatch):
    ctx = make_ctx({"A": A, "B": B})
    points = np.vstack([X, X[::-1] + [0, LENGTH, 0]])
    pixels = iter([project(A, points), project(B, points)])
    monkeypatch.setattr(scaling, "_available_dicts", lambda _: ["DICT_6X6_250"])
    monkeypatch.setattr(scaling, "_load_gray", lambda _: (None, None, 1.0))
    monkeypatch.setattr(scaling, "detect_marker_corners", lambda *args: {7: next(pixels)})
    info = aruco_scale(ctx, 2 * LENGTH, log=lambda *_: None)
    assert ctx.scale == pytest.approx(2.0)
    assert ctx.scale_info is info
    assert info["reproj_px_mean"] < 1e-9
    assert info["side_spread_rel"] < 1e-9
    assert info["scale_rel_error"] == 0.005
    np.testing.assert_allclose(info["marker_corners_m"], 2 * points, atol=1e-9)

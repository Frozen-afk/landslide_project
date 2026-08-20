"""Quality gates of the manual (two-photo) scaling path, with synthetic cameras."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest

from landslide.scaling import manual_scale
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

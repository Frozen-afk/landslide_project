import numpy as np

from landslide.volume import (fit_plane, fit_plane_robust, fit_quadratic,
                              prism_volume)


def make_bowl_points(R=6.0, depth=2.0, r_poly=5.6, step=0.22, rim_width=0.35):
    """Regular-grid sample of a cosine bowl; rim = flat ring at r_poly..+w."""
    xs = np.arange(-r_poly - rim_width, r_poly + rim_width + step, step)
    X, Y = np.meshgrid(xs, xs)
    r = np.hypot(X, Y)
    interior_m = r <= r_poly
    bowl = depth * 0.5 * (1 + np.cos(np.pi * r / R))
    bowl[r >= R] = 0.0

    interior = np.stack([X[interior_m], Y[interior_m], -bowl[interior_m]], 1)
    rim_m = (r > r_poly) & (r <= r_poly + rim_width)
    rim = np.stack([X[rim_m], Y[rim_m], -bowl[rim_m]], 1)
    analytic = depth * np.pi * (
        r_poly ** 2 / 2
        + R * r_poly / np.pi * np.sin(np.pi * r_poly / R)
        + R ** 2 / np.pi ** 2 * (np.cos(np.pi * r_poly / R) - 1.0)
    )
    return interior, rim, analytic


def test_prism_volume_matches_analytic():
    interior, rim, analytic = make_bowl_points()
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["datum"] == "rim_plane"
    rel = abs(res["cut_volume_m3"] - analytic) / analytic
    assert rel < 0.02, f"cut {res['cut_volume_m3']:.2f} vs analytic {analytic:.2f}"
    assert res["fill_volume_m3"] < 0.01 * analytic
    assert res["net_volume_m3"] == pytest_approx(-analytic, 0.02)


def test_datum_fallback_without_rim():
    interior, _, analytic = make_bowl_points(rim_width=0.0)
    res = prism_volume(interior, None, log=lambda *_: None)
    assert res["datum"] == "surface_plane"
    # without a rim the datum hugs the surface -> volume shrinks toward zero
    assert res["cut_volume_m3"] < 0.5 * analytic


def pytest_approx(expected, rel):
    class A:
        def __eq__(self, other):
            return abs(other - expected) <= rel * abs(expected)
    return A()


def test_fit_plane_orientation():
    rng = np.random.default_rng(3)
    pts = rng.normal(size=(500, 3)) * [10, 10, 0.001] + [5, 5, 3]
    c, n, basis = fit_plane(pts)
    assert abs(abs(n @ np.array([0, 0, 1.0])) - 1) < 1e-6
    assert abs(n @ basis[0]) < 1e-9 and abs(n @ basis[1]) < 1e-9


def test_fit_plane_robust_excludes_outliers():
    rng = np.random.default_rng(11)
    pts = rng.normal(size=(600, 3)) * [10, 10, 0.02] + [5, 5, 3]
    bad = pts[rng.choice(len(pts), 80, replace=False)].copy()
    bad[:, 2] -= 4.0                          # floaters far below the plane
    c, n, basis, keep = fit_plane_robust(np.concatenate([pts, bad]))
    assert keep[:600].mean() > 0.95           # real points survive (2.5σ trims
    assert not keep[600:].any()               # the Gaussian tail, not the bulk)
    assert abs(abs(c @ n) - 3.0) < 0.05       # plane height not dragged down


def test_robust_datum_survives_rim_outliers():
    interior, rim, analytic = make_bowl_points()
    rng = np.random.default_rng(11)
    bad = rim[rng.choice(len(rim), int(0.15 * len(rim)), replace=False)].copy()
    bad[:, 2] -= 3.0                          # stereo junk in the rim band
    res = prism_volume(interior, np.concatenate([rim, bad]), log=lambda *_: None)
    rel = abs(res["cut_volume_m3"] - analytic) / analytic
    assert rel < 0.05, f"cut {res['cut_volume_m3']:.2f} vs analytic {analytic:.2f}"
    assert res["n_rim_outliers"] >= len(bad) - 5   # nearly all junk clipped
    assert any("outlier" in w for w in res["warnings"])


def test_tin_does_not_bridge_large_gaps():
    # two flat 2x2 m patches separated by a 6 m gap: the triangulation must
    # not count the gap as area (a polygon marked over unreconstructed
    # background)
    patches = []
    for cx in (0.0, 8.0):
        g = np.arange(0, 2.001, 0.1)
        X, Y = np.meshgrid(g, g)
        patches.append(np.stack([X.ravel() + cx, Y.ravel(),
                                 np.zeros(X.size)], 1))
    interior = np.concatenate(patches)
    res = prism_volume(interior, None, log=lambda *_: None)
    assert 7.0 < res["area_m2"] < 9.6         # ~ the two patches, not 20 m²
    assert abs(res["net_volume_m3"]) < 0.05
    assert any("could not be reconstructed" in w for w in res["warnings"])


def make_curved_bowl_points(R=6.0, depth=2.0, r_poly=5.6, step=0.22):
    """Cosine bowl carved into a curved hillslope z = 0.05x² − 0.02y².

    Rim band sits well outside the bowl (r 5.8–6.3) so it samples pure
    curved ground. Returns (interior, rim, analytic_bowl_volume).
    """
    xs = np.arange(-6.5, 6.5 + step, step)
    X, Y = np.meshgrid(xs, xs)
    r = np.hypot(X, Y)
    ground = 0.05 * X ** 2 - 0.02 * Y ** 2
    bowl = depth * 0.5 * (1 + np.cos(np.pi * r / R))
    bowl[r >= R] = 0.0
    z = ground - bowl
    interior_m = r <= r_poly
    rim_m = (r > 5.8) & (r <= 6.3)
    interior = np.stack([X[interior_m], Y[interior_m], z[interior_m]], 1)
    rim = np.stack([X[rim_m], Y[rim_m], z[rim_m]], 1)
    analytic = depth * np.pi * (
        r_poly ** 2 / 2
        + R * r_poly / np.pi * np.sin(np.pi * r_poly / R)
        + R ** 2 / np.pi ** 2 * (np.cos(np.pi * r_poly / R) - 1.0)
    )
    return interior, rim, analytic


def test_flat_bowl_stays_on_plane_datum():
    # gentle noise only: the quadratic must NOT be adopted (residuals already
    # plane-like), keeping the datum model simple where a plane suffices
    interior, rim, analytic = make_bowl_points()
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["datum"] == "rim_plane"


def test_curved_hillside_gets_quadratic_datum():
    interior, rim, analytic = make_curved_bowl_points()
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["datum"] == "rim_quad"
    rel = abs(res["cut_volume_m3"] - analytic) / analytic
    assert rel < 0.05, f"cut {res['cut_volume_m3']:.2f} vs analytic {analytic:.2f}"
    # a plane datum through this curved rim would be badly biased — prove the
    # upgrade matters by forcing the plane path
    import landslide.volume as V
    orig = V.fit_quadratic
    V.fit_quadratic = lambda *a, **k: (None, float("inf"))
    try:
        res_plane = prism_volume(interior, rim, log=lambda *_: None)
    finally:
        V.fit_quadratic = orig
    assert res_plane["datum"] == "rim_plane"
    assert abs(res_plane["cut_volume_m3"] - analytic) / analytic > 0.10


def test_fit_quadratic_recovers_paraboloid():
    rng = np.random.default_rng(2)
    uv = rng.uniform(-5, 5, size=(400, 2))
    h = 0.3 + 0.1 * uv[:, 0] - 0.2 * uv[:, 1] + 0.05 * uv[:, 0] ** 2 \
        + 0.02 * uv[:, 1] ** 2 - 0.01 * uv[:, 0] * uv[:, 1]
    quad, rms = fit_quadratic(uv, h)
    assert quad is not None and rms < 1e-6


def make_pile_on_road_with_wall(half=5.0, step=0.25, pile_h=0.6, pile_half=2.0):
    """A debris pile sitting ON a flat road, with the polygon edge climbing a
    retaining wall: the rim band holds flat road points plus a vertical wall
    patch (the contamination that inverts cut into fill)."""
    xs = np.arange(-half - 1.6, half + 1.6 + step, step)
    X, Y = np.meshgrid(xs, xs)
    inside = (np.abs(X) <= half) & (np.abs(Y) <= half)
    # square pyramid pile centred in the region
    z = pile_h * np.clip(1 - np.maximum(np.abs(X), np.abs(Y)) / pile_half, 0, 1)
    interior = np.stack([X[inside], Y[inside], z[inside]], 1)
    rim_m = ~inside
    rim = np.stack([X[rim_m], Y[rim_m], np.zeros(int(rim_m.sum()))], 1)
    # vertical wall patch just outside one edge: x fixed, z sweeping up
    rng = np.random.default_rng(7)
    wall = np.column_stack([np.full(60, half + 0.6),
                            rng.uniform(-half, half, 60),
                            rng.uniform(0.0, 1.2, 60)])
    rim = np.vstack([rim, wall])
    truth = 4.0 * pile_half ** 2 * pile_h / 3.0        # pyramid volume
    return interior, rim, truth


def test_wall_rim_does_not_invert_pile_into_depression():
    interior, rim, truth = make_pile_on_road_with_wall()
    up = np.array([0.0, 0.0, 1.0])
    res = prism_volume(interior, rim, log=lambda *_: None, up=up)
    # the pile must read as FILL (positive net), not a depression
    assert res["fill_volume_m3"] > 0.7 * truth, \
        f"fill {res['fill_volume_m3']:.2f} vs pile truth {truth:.2f}"
    assert res["net_volume_m3"] > 0, "pile inverted into a depression"
    rel = abs(res["net_volume_m3"] - truth) / truth
    assert rel < 0.15, f"net {res['net_volume_m3']:.2f} vs truth {truth:.2f}"
    assert any("steep surfaces" in w for w in res["warnings"])

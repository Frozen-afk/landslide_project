import numpy as np

from landslide.volume import (_fill_small_holes, _raster_bin, bootstrap_volume_ci,
                              fit_plane, fit_plane_robust, fit_quadratic,
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
    # richer models matter by forcing the simple-datum path
    import landslide.volume as V
    orig_q, orig_t = V.fit_quadratic, V.fit_tps_membrane
    V.fit_quadratic = lambda *a, **k: (None, float("inf"))
    V.fit_tps_membrane = lambda *a, **k: None
    try:
        res_plane = prism_volume(interior, rim, log=lambda *_: None)
    finally:
        V.fit_quadratic, V.fit_tps_membrane = orig_q, orig_t
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


def _clustered_contaminated_rim(n_good=400, n_bad=250, seed=7):
    """Flat road rim + a dense rubble patch sitting 0.5 m above it.

    Scattered outliers are handled by sigma clipping alone; a dense cluster
    (a rubble pile inside the rim band, vegetation patch) tilts the initial
    all-points fit before clipping engages — this is the RANSAC case.
    """
    rng = np.random.default_rng(seed)
    good = rng.uniform([0, 0, 0], [10, 10, 0.01], (n_good, 3))
    patch = rng.uniform([4, 4, 0.5], [6, 6, 0.55], (n_bad, 3))
    return np.concatenate([good, patch]), good, patch


def test_fit_plane_ransac_ignores_clustered_contamination():
    from landslide.volume import fit_plane_ransac
    pts, good, patch = _clustered_contaminated_rim()
    keep = fit_plane_ransac(pts)
    assert keep is not None
    assert keep[:len(good)].mean() > 0.9        # road points are the consensus
    assert keep[len(good):].mean() < 0.1        # the rubble cluster is rejected
    # and the full robust fit lands on the road, not between road and rubble
    c, n, _, inliers = fit_plane_robust(pts)
    assert abs(abs(n @ np.array([0, 0, 1.0])) - 1) < 0.02   # normal ~vertical
    assert abs(c[2]) < 0.05                                 # height ~road level


def test_fit_plane_ransac_degenerate_inputs():
    from landslide.volume import fit_plane_ransac
    assert fit_plane_ransac(np.zeros((10, 3))) is None        # too few points
    assert fit_plane_ransac(np.ones((100, 3))) is None        # zero extent
    # degenerate consensus must not break the robust fit
    c, n, _, keep = fit_plane_robust(np.ones((100, 3)))
    assert keep.all() and np.isfinite(c).all()


def test_robust_datum_survives_clustered_rim_contamination():
    interior, rim, analytic = make_bowl_points()
    rng = np.random.default_rng(5)
    # dense stereo-floater cluster hugging one side of the rim band
    side = rim[rim[:, 0] > rim[:, 0].mean()]
    cluster_anchor = side.mean(axis=0)
    bad = cluster_anchor + rng.uniform([-0.3, -0.3, 0.6], [0.3, 0.3, 0.7],
                                       (120, 3))
    res = prism_volume(interior, np.concatenate([rim, bad]), log=lambda *_: None)
    rel = abs(res["cut_volume_m3"] - analytic) / analytic
    assert rel < 0.05, f"cut {res['cut_volume_m3']:.2f} vs analytic {analytic:.2f}"


def _inclined_interior(slope_deg=45.0, half=4.0, step=0.25, rim_w=1.0):
    """Interior plane inclined `slope_deg` above a flat rim ring."""
    xs = np.arange(-half - rim_w, half + rim_w + step, step)
    X, Y = np.meshgrid(xs, xs)
    inside = (np.abs(X) <= half) & (np.abs(Y) <= half)
    g = np.tan(np.radians(slope_deg))
    z = g * (X + half)                        # plane rising along x
    interior = np.stack([X[inside], Y[inside], z[inside]], 1)
    rim_m = ~inside
    rim = np.stack([X[rim_m], Y[rim_m], np.zeros(int(rim_m.sum()))], 1)
    return interior, rim


def test_slope_hazard_fields():
    # shallow flat-ish interior: no steep area, slopes stay gentle
    interior, rim, _ = make_bowl_points(depth=0.3)
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["max_slope_deg"] < 15.0
    assert res["area_steep_m2"] < 0.5
    # 45° inclined surface: the interior must flag as over-steepened (the
    # gridded estimator drops boundary cells, so not quite the full area)
    interior, rim = _inclined_interior(45.0)
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert abs(res["mean_slope_deg"] - 45.0) < 3.0
    assert res["area_steep_m2"] > 0.75 * res["area_m2"]
    assert any("steeper than 35" in w for w in res["warnings"])


def test_significance_reporting():
    # a clear signal: every cell far above noise -> significant
    interior, rim = _inclined_interior(30.0)
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["sig_area_frac"] > 0.9
    assert not any("within survey noise" in w for w in res["warnings"])
    # a whisper-thin layer over a noisy rim: net should sit inside the noise
    # band and say so
    rng = np.random.default_rng(4)
    xs = np.arange(-4, 4.25, 0.25)
    X, Y = np.meshgrid(xs, xs)
    inside = np.hypot(X, Y) <= 4.0
    z = 0.002 * rng.standard_normal(inside.sum())   # 2 mm of noise, no signal
    interior = np.stack([X[inside], Y[inside], z], 1)
    rim_m = ~inside
    rim = np.stack([X[rim_m], Y[rim_m],
                    0.05 * rng.standard_normal(int(rim_m.sum()))], 1)
    res = prism_volume(interior, rim, log=lambda *_: None)
    assert res["sig_area_frac"] < 0.2
    assert any("within survey noise" in w for w in res["warnings"])


def make_s_curve_road_with_pile(step=0.22, half=2.0, pile_r=1.3,
                                pile_h=0.5, seed=9):
    """Debris pile on a road that bends AND descends (z = 0.5 sin(2*pi*x/6)).

    The rim band hugs the traced region (as real traces do); a single
    paraboloid cannot follow the S-curve, the TPS membrane can. Returns
    (interior, rim, analytic_pile_volume).
    """
    xs = np.arange(-half - 1.0, half + 1.0 + step, step)
    X, Y = np.meshgrid(xs, xs)
    r = np.hypot(X, Y)
    road = 0.5 * np.sin(2 * np.pi * X / 6.0)
    inside = r <= half
    pile = np.clip(1 - r / pile_r, 0, 1)
    z = road + pile_h * pile
    interior = np.stack([X[inside], Y[inside], z[inside]], 1)
    rim_m = (r > half) & (r <= half + 1.0)
    rng = np.random.default_rng(seed)
    rim_z = road[rim_m] + 0.01 * rng.standard_normal(int(rim_m.sum()))
    rim = np.stack([X[rim_m], Y[rim_m], rim_z], 1)
    # cone pile volume above the road
    analytic = np.pi * pile_r ** 2 * pile_h / 3.0
    return interior, rim, analytic


# ---------- F15/M4: T2.1/T2.2 unit tests (none existed before this audit) ----------

def test_raster_bin_median_and_mad_per_cell():
    # two points in one cell, one in another: median/count/sigma per cell
    uv2 = np.array([[0.05, 0.05], [0.06, 0.06], [1.5, 1.5]])
    h = np.array([1.0, 3.0, -2.0])
    grid = _raster_bin(uv2, h, cell=1.0)
    assert grid["nx"] == 3 and grid["ny"] == 3
    cell0 = 0 * grid["nx"] + 0     # (0,0): both first points land here
    cell1 = 1 * grid["nx"] + 1     # (1,1): the third point
    assert grid["count"][cell0] == 2
    assert grid["height"][cell0] == 2.0                 # median(1, 3)
    assert grid["sigma"][cell0] == 1.4826 * 1.0          # MAD(1,3) around 2
    assert grid["count"][cell1] == 1
    assert grid["height"][cell1] == -2.0
    assert grid["sigma"][cell1] == 0.0                   # single point, no spread


def test_raster_bin_empty_cells_are_nan():
    uv2 = np.array([[0.05, 0.05], [3.5, 3.5]])
    h = np.array([1.0, 2.0])
    grid = _raster_bin(uv2, h, cell=1.0)
    empty = grid["count"] == 0
    assert empty.any()
    assert np.isnan(grid["height"][empty]).all()


def test_fill_small_holes_fills_gap_enclosed_along_a_row():
    # data - gap - data, 1 row: the gap has data on both sides of ITS OWN row
    grid = {"nx": 3, "ny": 1, "height": np.array([1.0, np.nan, 3.0]),
            "count": np.array([1, 0, 1])}
    filled, valid, unmeasured = _fill_small_holes(grid, max_radius=20, min_neighbors=1)
    assert valid[1] and not unmeasured[1]
    assert filled[1] == 2.0                              # mean of the two neighbours


def test_fill_small_holes_leaves_edge_gap_unmeasured():
    # gap - gap - data: the gaps only ever see data on ONE side (edge of the
    # real footprint, not an enclosed hole) -> left unfilled, flagged
    grid = {"nx": 3, "ny": 1, "height": np.array([np.nan, np.nan, 1.0]),
            "count": np.array([0, 0, 1])}
    filled, valid, unmeasured = _fill_small_holes(grid, max_radius=20, min_neighbors=1)
    assert not valid[0] and not valid[1]
    assert unmeasured[0] and unmeasured[1]


def test_fill_small_holes_does_not_bridge_a_diagonal_gap():
    # data only at the two opposite corners of a 3x3 block: the center gap's
    # OWN row and column are both entirely empty, so the 1D row/column
    # enclosure check does not see the diagonal data at all (documented
    # asymmetry, F15) — it must stay unfilled, not silently bridged.
    height = np.full((3, 3), np.nan)
    count = np.zeros((3, 3), np.int64)
    height[0, 0], count[0, 0] = 1.0, 1
    height[2, 2], count[2, 2] = 1.0, 1
    grid = {"nx": 3, "ny": 3, "height": height.ravel(), "count": count.ravel()}
    filled, valid, unmeasured = _fill_small_holes(grid, max_radius=20, min_neighbors=1)
    center = 1 * 3 + 1
    assert not valid[center]
    assert not unmeasured[center]      # no data at all in its own row/column


def test_bootstrap_volume_ci_returns_none_below_15_rim_points():
    interior, rim, _ = make_bowl_points()
    assert bootstrap_volume_ci(interior, rim[:14], None, 10.0, 10.0, 0.2,
                               "rim_plane", log=lambda *_: None) is None


def test_bootstrap_volume_ci_deterministic_for_fixed_seed():
    interior, rim, _ = make_bowl_points()
    up = np.array([0.0, 0.0, 1.0])
    a = bootstrap_volume_ci(interior, rim, up, 5.0, 5.0, 0.5, "rim_plane",
                            log=lambda *_: None, B=30, seed=0)
    b = bootstrap_volume_ci(interior, rim, up, 5.0, 5.0, 0.5, "rim_plane",
                            log=lambda *_: None, B=30, seed=0)
    assert a == b


def test_bootstrap_volume_ci_widens_with_rim_noise():
    interior, rim, _ = make_bowl_points()
    up = np.array([0.0, 0.0, 1.0])
    clean = bootstrap_volume_ci(interior, rim, up, 5.0, 5.0, 0.5, "rim_plane",
                                log=lambda *_: None, B=60, seed=0)
    rng = np.random.default_rng(1)
    noisy_rim = rim.copy()
    noisy_rim[:, 2] += rng.normal(0, 0.15, len(rim))
    noisy = bootstrap_volume_ci(interior, noisy_rim, up, 5.0, 5.0, 0.5, "rim_plane",
                                log=lambda *_: None, B=60, seed=0)
    assert clean is not None and noisy is not None
    assert (noisy[0] + noisy[1]) > (clean[0] + clean[1])


def test_s_curve_road_gets_membrane_datum():
    interior, rim, truth = make_s_curve_road_with_pile()
    up = np.array([0.0, 0.0, 1.0])
    res = prism_volume(interior, rim, log=lambda *_: None, up=up)
    assert res["datum"] == "rim_tps", f"datum was {res['datum']}"
    rel = abs(res["fill_volume_m3"] - truth) / truth
    assert rel < 0.12, f"fill {res['fill_volume_m3']:.2f} vs truth {truth:.2f}"
    assert res["net_volume_m3"] > 0
    # prove the membrane matters: without it the paraboloid datum biases the
    # pile volume badly
    import landslide.volume as V
    orig = V.fit_tps_membrane
    V.fit_tps_membrane = lambda *a, **k: None
    try:
        res_no_tps = prism_volume(interior, rim, log=lambda *_: None, up=up)
    finally:
        V.fit_tps_membrane = orig
    assert res_no_tps["datum"] != "rim_tps"
    rel_no = abs(res_no_tps["fill_volume_m3"] - truth) / truth
    assert rel_no > 0.20, \
        f"quad-only fill {res_no_tps['fill_volume_m3']:.2f} unexpectedly close"

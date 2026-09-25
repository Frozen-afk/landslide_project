"""Quality gate status logic (gates.py)."""
import numpy as np

from landslide.gates import evaluate_gates


class _FakeCtx:
    dense = {"points": np.zeros((30_000, 3))}
    scale_info = {}
    warnings = []
    rec = object()   # not a real pycolmap.Reconstruction; collinearity check
                      # must degrade gracefully (caught by gates.py's except)


def test_dem_mode_status_never_ok():
    # B3 regression: dem_volume has no G6 coverage gate wired (its interior
    # points live in the DEM-aligned frame, not the model's up/polygon_ground
    # frame G6 needs), so it was the only path that could report status="ok"
    # with a fully un-gated coverage risk (FINAL_RELEASE_AUDIT.md §4.3).
    res = {"cloud": "dense", "datum": "dem"}
    status, reasons = evaluate_gates(_FakeCtx(), res, "ortho")
    assert status != "ok", reasons
    assert any("coverage gate" in r for r in reasons)


def test_non_dem_mode_unaffected_by_dem_guard():
    res = {"cloud": "dense", "datum": "rim_plane", "coverage_frac": 0.9,
           "largest_void_m2": 0.1, "datum_holdout_m": 0.001,
           "datum_holdout_windows": 8, "area_m2": 100.0, "net_volume_m3": 60.0}
    status, reasons = evaluate_gates(_FakeCtx(), res, "ground_frame")
    assert status == "ok", reasons


def test_g9_holdout_threshold_mapping():
    # R6 (DATUM_STABILITY_PLAN.md §8): pure evaluate_gates mapping from the
    # G9 inputs to status/reason id, independent of prism_volume's own
    # fixtures — this is what actually pins the 5%/6-window thresholds.
    from landslide.gates import classify_measurement_path

    base = {"cloud": "dense", "datum": "rim_plane", "coverage_frac": 0.9,
            "largest_void_m2": 0.1, "area_m2": 100.0, "net_volume_m3": 60.0}

    # (a) tight holdout, full rim coverage -> ok
    res_a = {**base, "datum_holdout_m": 0.001, "datum_holdout_windows": 8}
    status, reasons = evaluate_gates(_FakeCtx(), res_a, "ground_frame")
    assert status == "ok", reasons
    assert res_a["reason_ids"] == []
    assert classify_measurement_path(status, res_a["reason_ids"], "rim_plane") == "rim"

    # (b) holdout above the 5% ratio (0.07 x 100 / 60 = 11.7% > 5%) -> indicative
    res_b = {**base, "datum_holdout_m": 0.07, "datum_holdout_windows": 8}
    status, reasons = evaluate_gates(_FakeCtx(), res_b, "ground_frame")
    assert status == "indicative", reasons
    assert any("datum uncertain" in r for r in reasons), reasons
    assert res_b["reason_ids"] == ["G9_datum_unstable"]
    assert classify_measurement_path(
        status, res_b["reason_ids"], "rim_plane") == "indicative_only"

    # (c) too few usable windows -> indicative, regardless of the holdout value
    res_c = {**base, "datum_holdout_m": 0.001, "datum_holdout_windows": 5}
    status, reasons = evaluate_gates(_FakeCtx(), res_c, "ground_frame")
    assert status == "indicative", reasons
    assert any("of 8 sides" in r for r in reasons), reasons
    assert res_c["reason_ids"] == ["G9_rim_partial"]
    assert classify_measurement_path(
        status, res_c["reason_ids"], "rim_plane") == "capture_failure"

    # (d) dem datum: unaffected by any rim-only G9 input (the dem branch
    # itself is covered by test_dem_mode_status_never_ok above)
    res_d = {"cloud": "dense", "datum": "dem"}
    status, reasons = evaluate_gates(_FakeCtx(), res_d, "ortho")
    assert status != "ok"
    assert any("coverage gate" in r for r in reasons)
    assert classify_measurement_path(status, res_d["reason_ids"], "dem") == "dem"

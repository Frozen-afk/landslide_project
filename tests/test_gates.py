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
           "largest_void_m2": 0.1}
    status, reasons = evaluate_gates(_FakeCtx(), res, "ground_frame")
    assert status == "ok", reasons

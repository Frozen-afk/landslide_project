"""Per-image (T1) and per-sequence (T2) guided-capture check correctness
(GUIDED_CAPTURE_PLAN.md S2, criterion 2). Fixtures: tests/fixtures/capture_t1/
(regenerate via `.venv/bin/python -m tests.fixtures.capture_t1.make_fixtures`
after any threshold change).

Two kinds of test here, and why:
- Threshold-arithmetic tests monkeypatch the expensive real-CV calls
  (extract_feats/_match_pair/_marker_check) with exact numbers straddling
  each §4.1 cut point, so the pass/warn/fail boundaries are pinned precisely
  without depending on this repo's only synthetic scene happening to produce
  a keypoint count or h_ratio on the right side of a threshold real phone
  photos would clear (see GUIDED_CAPTURE_PROGRESS.md "S2" deviations: this
  scene's SIFT keypoint counts and pivot-pair h_ratio sit below the real
  §4.1 numbers even on maximally-textured/pure-rotation content).
- Fixture tests run the real detector/matcher on real (if synthetic) 3-D
  content, to prove the wiring — detect_marker_corners, SIFT matching,
  _photo_metrics — is actually exercised end to end, asserting on the
  specific reason id each check owns rather than the aggregate verdict
  (which every fixture here also fails on "features", per the note above).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from landslide import capture as C

F = Path(__file__).resolve().parent / "fixtures" / "capture_t1"


def _ids(res) -> set[str]:
    return {r["id"] for r in res["reasons"]}


# ---------------- threshold-arithmetic (monkeypatched) ----------------

def _patch_photo_open(monkeypatch):
    """image_checks does `from PIL import Image; Image.open(path)` — a stub
    context manager sidesteps decoding a real file for the pure threshold
    tests below."""
    import PIL.Image as PILImage
    monkeypatch.setattr(PILImage, "open",
                        lambda p: type("F", (), {"__enter__": lambda s: s,
                                                  "__exit__": lambda s, *a: None})())


def test_blur_fail_below_floor_ok_above(monkeypatch):
    _patch_photo_open(monkeypatch)
    monkeypatch.setattr(C, "extract_feats", lambda *a, **kw: ([0] * 900, object()))

    monkeypatch.setattr(C, "_photo_metrics", lambda im: (3.0, 0.0, None))
    assert "blur" in _ids(C.image_checks("dummy.jpg"))

    monkeypatch.setattr(C, "_photo_metrics", lambda im: (50.0, 0.0, None))
    assert "blur" not in _ids(C.image_checks("dummy.jpg"))


def test_features_fail_warn_ok_thresholds(monkeypatch):
    _patch_photo_open(monkeypatch)
    monkeypatch.setattr(C, "_photo_metrics", lambda im: (999.0, 0.0, None))

    for n_feat, expect_fail, expect_warn in [(100, True, False), (500, False, True), (900, False, False)]:
        monkeypatch.setattr(C, "extract_feats", lambda *a, n=n_feat, **kw: ([0] * n, object()))
        r = C.image_checks("dummy.jpg")
        assert ("features" in _ids(r)) == expect_fail, n_feat
        if expect_warn:
            assert r["verdict"] == "warn"
        if not expect_fail and not expect_warn:
            assert r["verdict"] == "ok"


def test_overlap_fail_warn_ok_thresholds(monkeypatch):
    _patch_photo_open(monkeypatch)
    monkeypatch.setattr(C, "_photo_metrics", lambda im: (999.0, 0.0, None))
    monkeypatch.setattr(C, "extract_feats", lambda *a, **kw: ([0] * 900, object()))

    for f_inliers, expect_fail in [(10, True), (60, False), (150, False)]:
        monkeypatch.setattr(C, "_match_pair", lambda *a, f=f_inliers, **kw:
                            {"matches": f + 5, "f_inliers": f, "h_inliers": 0, "h_ratio": 0.0})
        r = C.image_checks("dummy.jpg", prev_path="prev.jpg")
        assert ("overlap" in _ids(r)) == expect_fail, f_inliers


def test_parallax_warns_once_then_fails_two_in_a_row(monkeypatch):
    _patch_photo_open(monkeypatch)
    monkeypatch.setattr(C, "_photo_metrics", lambda im: (999.0, 0.0, None))
    monkeypatch.setattr(C, "extract_feats", lambda *a, **kw: ([0] * 900, object()))
    # good overlap (>= OVERLAP_FAIL) but near-pure homography (a pivot)
    monkeypatch.setattr(C, "_match_pair", lambda *a, **kw:
                        {"matches": 200, "f_inliers": 150, "h_inliers": 148, "h_ratio": 0.987})

    r_first = C.image_checks("dummy.jpg", prev_path="prev.jpg", prev_pivot=False)
    assert "parallax" not in _ids(r_first)
    assert r_first["verdict"] == "warn"

    r_second = C.image_checks("dummy.jpg", prev_path="prev.jpg", prev_pivot=True)
    assert "parallax" in _ids(r_second)
    assert r_second["verdict"] == "fail"


def test_marker_size_and_angle_thresholds(monkeypatch):
    _patch_photo_open(monkeypatch)
    monkeypatch.setattr(C, "_photo_metrics", lambda im: (999.0, 0.0, None))
    monkeypatch.setattr(C, "extract_feats", lambda *a, **kw: ([0] * 900, object()))

    def fake_marker(side_px, ratio, clipped):
        def f(*a, **kw):
            return _marker_from(side_px, ratio, clipped)
        return f

    monkeypatch.setattr(C, "_marker_check", fake_marker(20, 0.9, False))
    assert "marker_small" in _ids(C.image_checks("dummy.jpg", marker_side_m=0.5))

    monkeypatch.setattr(C, "_marker_check", fake_marker(90, 0.3, False))
    assert "marker_edge_on" in _ids(C.image_checks("dummy.jpg", marker_side_m=0.5))

    monkeypatch.setattr(C, "_marker_check", fake_marker(90, 0.9, True))
    assert "marker_clipped" in _ids(C.image_checks("dummy.jpg", marker_side_m=0.5))

    monkeypatch.setattr(C, "_marker_check", fake_marker(90, 0.9, False))
    r = C.image_checks("dummy.jpg", marker_side_m=0.5)
    assert not any(x.startswith("marker_") for x in _ids(r))


def _marker_from(side_px, ratio, clipped):
    size_v = "ok" if side_px >= C.MARKER_SIDE_OK_PX else \
        "warn" if side_px >= C.MARKER_SIDE_WARN_PX else "fail"
    angle_v = "ok" if ratio >= C.MARKER_RATIO_WARN else \
        "warn" if ratio >= C.MARKER_RATIO_FAIL else "fail"
    verdict = "fail" if "fail" in (size_v, angle_v) else \
        "warn" if clipped or "warn" in (size_v, angle_v) else "ok"
    reason = None
    if size_v != "ok":
        reason = "marker_small"
    if angle_v != "ok":
        reason = "marker_edge_on"
    if clipped:
        reason = "marker_clipped"
    return {"id": 0, "side_px": side_px, "side_ratio": ratio, "clipped": clipped,
            "verdict": verdict, "reason": reason}


# ---------------- real-fixture wiring (synthetic 3-D content) ----------------

def test_blur_fixture_flags_gaussian_blur():
    assert "blur" in _ids(C.image_checks(F / "blurry.jpg"))
    assert "blur" not in _ids(C.image_checks(F / "overlap_a.jpg"))


def test_marker_ok_on_arc_edge_on_fail_on_nadir():
    r_ok = C.image_checks(F / "marker_arc.jpg", marker_side_m=0.5)
    assert r_ok["server"]["marker"]["verdict"] == "ok"
    assert not any(x.startswith("marker_") for x in _ids(r_ok))

    r_fail = C.image_checks(F / "marker_nadir.jpg", marker_side_m=0.5)
    assert r_fail["server"]["marker"]["side_ratio"] < 0.4
    assert "marker_edge_on" in _ids(r_fail)


def test_overlap_consecutive_ok_three_apart_fails():
    r_consec = C.image_checks(F / "overlap_b.jpg", prev_path=F / "overlap_a.jpg")
    assert "overlap" not in _ids(r_consec)

    r_far = C.image_checks(F / "overlap_far.jpg", prev_path=F / "overlap_a.jpg")
    assert "overlap" in _ids(r_far)
    assert r_far["server"]["overlap_prev"]["f_inliers"] < r_consec["server"]["overlap_prev"]["f_inliers"]


def test_pivot_pair_scores_higher_h_ratio_than_a_translating_pair():
    """A same-eye 10 deg pan (pivot_a/b) is explained far better by a
    homography than a genuinely translating pair (overlap_a/b) from the
    same scene — the signal §4.1's parallax check relies on. The absolute
    0.95 cut point itself is a real-print calibration number (S5/S7); this
    scene's low keypoint count keeps both ratios under it (see this file's
    own docstring and GUIDED_CAPTURE_PROGRESS.md)."""
    r_pivot = C.image_checks(F / "pivot_b.jpg", prev_path=F / "pivot_a.jpg")
    r_translate = C.image_checks(F / "overlap_b.jpg", prev_path=F / "overlap_a.jpg")
    assert r_pivot["server"]["overlap_prev"]["h_ratio"] > \
        r_translate["server"]["overlap_prev"]["h_ratio"]


def test_texture_blank_has_far_fewer_features_than_real_content():
    r_blank = C.image_checks(F / "texture_blank.jpg")
    r_real = C.image_checks(F / "texture_arc.jpg")
    assert "features" in _ids(r_blank)
    assert r_blank["server"]["n_feat"] < r_real["server"]["n_feat"]


# ---------------- sequence_checks (T2) ----------------

def _photo(uuid, arm, verdict="ok", excluded=False, marker_verdict=None,
          overlap_inliers=None):
    server = {}
    if marker_verdict is not None:
        server["marker"] = {"verdict": marker_verdict}
    if overlap_inliers is not None:
        server["overlap_prev"] = {"f_inliers": overlap_inliers}
    return {"uuid": uuid, "arm": arm, "verdict": verdict, "excluded": excluded,
            "server": server}


def test_sequence_checks_counts_marker_views_and_overlap_gaps():
    photos = [
        _photo("1", "A", marker_verdict="ok"),
        _photo("2", "A", marker_verdict=None, overlap_inliers=80),
        _photo("3", "A", excluded=True, marker_verdict="ok"),   # excluded: ignored
        _photo("4", "B", marker_verdict="warn"),
        _photo("5", "B", overlap_inliers=10),   # a gap
    ]
    seq = C.sequence_checks(photos)
    assert seq["counts"] == {"A": 2, "B": 2}
    assert seq["total"] == 4
    assert seq["marker_views"] == {"A": 1, "B": 1}
    assert seq["marker_views_total"] == 2
    assert seq["overlap_gaps"] == ["5"]

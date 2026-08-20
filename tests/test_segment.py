"""Parsing/simplification of hosted-segmentation output (no network)."""
import base64

import cv2
import numpy as np

from landslide.segment import (_landslide_class_id, _simplify,
                               load_api_key, regions_from_response)


def _mask_resp(mask: np.ndarray, confidence=0.9) -> dict:
    """Roboflow SegFormer-style response: class mask + confidence mask PNGs."""
    def b64png(arr):
        ok, buf = cv2.imencode(".png", arr)
        assert ok
        return base64.b64encode(buf).decode()
    conf = np.full(mask.shape, int(confidence * 255), np.uint8)
    return {"image": {"width": mask.shape[1], "height": mask.shape[0]},
            "predictions": {
                "segmentation_mask": b64png(mask),
                "confidence_mask": b64png(conf),
                "class_map": {"0": "background", "1": "landslide"},
                "present_class_ids": [int(v) for v in np.unique(mask)]}}


def _two_blob_mask(w=400, h=300):
    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (110, 150), 70, 1, -1)      # big blob
    cv2.circle(mask, (320, 80), 30, 1, -1)       # small blob
    return mask


def test_mask_regions_extracted_largest_first():
    out = regions_from_response(_mask_resp(_two_blob_mask()))
    assert len(out) == 2
    assert out[0]["area_px"] > out[1]["area_px"]
    big = np.array(out[0]["polygon"])
    # centroid of the big polygon ~ (110, 150)
    assert abs(big[:, 0].mean() - 110) < 15 and abs(big[:, 1].mean() - 150) < 15
    assert 3 <= len(big) <= 120


def test_mask_scale_maps_back_to_source_frame():
    out = regions_from_response(_mask_resp(_two_blob_mask()), scale=(2.0, 2.0))
    big = np.array(out[0]["polygon"])
    assert abs(big[:, 0].mean() - 220) < 30    # 110 * 2
    assert abs(big[:, 1].mean() - 300) < 30    # 150 * 2


def test_mask_empty_and_specks():
    assert regions_from_response(_mask_resp(np.zeros((300, 400), np.uint8))) == []
    mask = np.zeros((300, 400), np.uint8)
    cv2.circle(mask, (200, 150), 4, 1, -1)     # below min_area
    assert regions_from_response(_mask_resp(mask)) == []


def test_mask_low_confidence_dropped():
    mask = _two_blob_mask()
    assert len(regions_from_response(_mask_resp(mask, confidence=0.9))) == 2
    assert regions_from_response(_mask_resp(mask, confidence=0.2)) == []


def test_landslide_class_id_from_map():
    assert _landslide_class_id({"0": "background", "1": "landslide"}) == 1
    assert _landslide_class_id({"0": "bg", "2": "Landslide"}) == 2
    assert _landslide_class_id(None) == 1


def _poly_resp(points, confidence=0.9, cls="landslide"):
    return {"predictions": [
        {"class": cls, "confidence": confidence,
         "points": [{"x": x, "y": y} for x, y in points]}]}


# a wobbly circle with 120 vertices -> simplification must kick in
RING = [(100 + 60 * np.cos(2 * np.pi * i / 120),
         100 + 60 * np.sin(2 * np.pi * i / 120)) for i in range(120)]


def test_regions_largest_first_and_confidence_filter():
    small = _poly_resp([(0, 0), (40, 0), (40, 40), (0, 40)], confidence=0.95)
    big = _poly_resp([(0, 0), (200, 0), (200, 200), (0, 200)], confidence=0.8)
    weak = _poly_resp([(0, 0), (500, 0), (500, 500), (0, 500)], confidence=0.1)
    out = regions_from_response({"predictions":
                                 [weak["predictions"][0], small["predictions"][0],
                                  big["predictions"][0]]})
    assert len(out) == 2                     # 0.1 confidence dropped
    assert out[0]["area_px"] > out[1]["area_px"]


def test_regions_simplify_and_scale():
    out = regions_from_response(_poly_resp(RING),
                                scale=(2.0, 2.0), max_points=40)
    assert len(out) == 1
    r = out[0]
    assert len(r["polygon"]) <= 40, "boundary not simplified"
    assert len(r["polygon"]) >= 3
    # points must be scaled back to the source frame
    xs = [p[0] for p in r["polygon"]]
    assert min(xs) >= 2 * 35 and max(xs) <= 2 * 165


def test_regions_garbage_tolerant():
    assert regions_from_response(None) == []
    assert regions_from_response({}) == []
    assert regions_from_response({"predictions": []}) == []
    # malformed entries never raise
    assert regions_from_response({"predictions": [
        {"confidence": "x"}, {"points": []},
        {"confidence": 0.9, "points": [{"x": 1}, {"x": 2}]},
    ]}) == []
    # sub-100 px^2 specks are artefacts, not regions
    assert regions_from_response(_poly_resp([(0, 0), (9, 0), (9, 9)])) == []


def test_simplify_never_loses_the_polygon():
    poly = np.array(RING, np.float64)
    out = _simplify(poly, 8)
    assert len(out) <= 8 and len(out) >= 3
    # a triangle already fits: comes back usable
    tri = _simplify(np.array([[0, 0], [10, 0], [5, 10.0]]), 8)
    assert len(tri) == 3


def test_load_api_key_env_and_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nROBOFLOW_API_KEY="abc123"\nOTHER=1\n')
    monkeypatch.delenv("ROBOFLOW_API_KEY", raising=False)
    assert load_api_key(env_file) == "abc123"
    # environment beats the file
    monkeypatch.setenv("ROBOFLOW_API_KEY", "from-env")
    assert load_api_key(env_file) == "from-env"
    # nothing configured -> None, and a missing file is fine
    monkeypatch.delenv("ROBOFLOW_API_KEY", raising=False)
    assert load_api_key(tmp_path / "missing.env") is None

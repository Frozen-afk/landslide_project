"""Drift guard for tests/fixtures/capture/expected.json (GUIDED_CAPTURE_PLAN.md
S1): re-derives the server-side numbers from the *committed* JPEGs and checks
they still land close to what's recorded, so a future change to
landslide.pipeline._photo_metrics (or EXPOSURE_CLIP_FRAC) that the JS parity
test (tests/test_capture_metrics.mjs) relies on doesn't go unnoticed. Not a
byte-exact check: the committed fixtures are JPEG-compressed, expected.json's
numbers were taken before compression (see make_fixtures.py), so JPEG
quantization is expected to move them slightly.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image

from landslide.pipeline import _photo_metrics

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capture"


def _expected():
    return json.loads((FIXTURES / "expected.json").read_text())


def test_metric_fixtures_match_photo_metrics_within_jpeg_tolerance():
    expected = _expected()
    for m in expected["metrics"]:
        im = Image.open(FIXTURES / m["name"]).convert("RGB")
        lap, clip, dhash = _photo_metrics(im)
        assert abs(clip - m["clip"]) <= 0.02, m["name"]
        assert abs(lap - m["lap"]) <= max(3.0, 0.15 * m["lap"]), m["name"]
        ham = int(np.count_nonzero(dhash != np.array(m["dhash"], dtype=bool)))
        assert ham <= 4, (m["name"], ham)
        assert (lap < max(6.0, 0.25 * expected["batchMedianLap"])) == m["serverBlurFail"]
        assert (clip >= 0.5) == m["serverExposureFail"]


def test_exif_fixtures_round_trip_the_tags_capture_metrics_js_reads():
    expected = _expected()
    by_name = {e["name"]: e for e in expected["exifFixtures"]}

    ref = Image.open(FIXTURES / "exif_ref.jpg")
    exif = ref.getexif()
    assert exif.get(0x0112) == 1
    sub = exif.get_ifd(0x8769)
    assert abs(float(sub[0x920A]) - 5.0) < 1e-6
    assert sub[0xA405] == 26
    assert sub[0xA434] == "Test Cam 1x"

    switched = Image.open(FIXTURES / "exif_lens_switch.jpg").getexif().get_ifd(0x8769)
    assert switched[0xA434] == "Test Cam 2x"

    none_exif = Image.open(FIXTURES / "exif_none.jpg").getexif()
    assert 0x8769 not in none_exif

    rotated = Image.open(FIXTURES / "exif_rotated.jpg").getexif()
    assert rotated.get(0x0112) == 6

    assert set(by_name) == {"exif_ref.jpg", "exif_lens_switch.jpg", "exif_none.jpg", "exif_rotated.jpg"}

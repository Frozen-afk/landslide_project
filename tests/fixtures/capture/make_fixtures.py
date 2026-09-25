"""Generates the S1 parity fixtures for tests/test_capture_metrics.mjs
(GUIDED_CAPTURE_PLAN.md §11 criterion 1: capture_metrics.js vs
landslide.pipeline._photo_metrics). Writes tests/fixtures/capture/*.jpg and
tests/fixtures/capture/expected.json. Re-run after any change to
_photo_metrics or to this script's fixture set:

    .venv/bin/python -m tests.fixtures.capture.make_fixtures

Two independent fixture families, kept small on purpose (committed JSON):
- "metrics": blur/exposure/duplicate parity. Real textured content (crops of
  data/bench/arc's synthetic renders — the closest thing to a real phone
  photo already in this repo; no actual phone JPEGs are checked in anywhere
  in this repo's test suite, so this follows the same synthetic-fixture
  convention tests/test_import.py already uses), downscaled below the 900px
  cap so the dumped grey grid *is* what _photo_metrics measured (no need to
  also replicate PIL's own resize algorithm in JS — the plan's parity
  tolerance is explicitly about the metric math, not about matching a resize
  algorithm across two libraries).
- "exif": lens/orientation parity. Tiny solid images — parseExif() never
  looks at pixels, only at the file's byte structure.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from PIL.TiffImagePlugin import IFDRational

from landslide.pipeline import _photo_metrics

HERE = Path(__file__).resolve().parent
SRC_A = Path("data/bench/arc/images/IMG_00.png")
SRC_B = Path("data/bench/arc/images/IMG_05.png")
SIZE = (360, 270)  # well under the 900px cap: _photo_metrics does no resize


def _gray(im: Image.Image) -> np.ndarray:
    """Same conversion _photo_metrics uses (its resize is a no-op at SIZE)."""
    return np.asarray(im.convert("L"))


def _exif_bytes(orientation=1, focal_mm=5.0, focal35=26, lens="Test Cam 1x"):
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    sub = exif.get_ifd(0x8769)
    if focal_mm is not None:
        sub[0x920A] = IFDRational(int(round(focal_mm * 100)), 100)
    if focal35 is not None:
        sub[0xA405] = focal35
    if lens is not None:
        sub[0xA434] = lens
    return exif.tobytes()


def _save_jpeg(im: Image.Image, name: str, exif: bytes | None):
    path = HERE / name
    kwargs = {"quality": 92}
    if exif is not None:
        kwargs["exif"] = exif
    im.save(path, "JPEG", **kwargs)
    return path


def main():
    a = Image.open(SRC_A).convert("RGB").resize(SIZE)
    b = Image.open(SRC_B).convert("RGB").resize(SIZE)
    blurry = a.filter(ImageFilter.GaussianBlur(6))
    overexposed = ImageEnhance.Brightness(b).enhance(3.0)

    metric_srcs = {
        "sharp_a.jpg": a,
        "sharp_b.jpg": b,
        "dup_of_a.jpg": a,
        "blurry.jpg": blurry,
        "overexposed.jpg": overexposed,
    }
    metrics = []
    for name, im in metric_srcs.items():
        _save_jpeg(im, name, _exif_bytes())
        lap, clip, dhash = _photo_metrics(im)
        gray = _gray(im)
        metrics.append({
            "name": name,
            "w": int(gray.shape[1]),
            "h": int(gray.shape[0]),
            "gray": gray.flatten().astype(int).tolist(),
            "lap": lap,
            "clip": clip,
            "dhash": dhash.astype(int).tolist(),
        })

    exif_srcs = {
        "exif_ref.jpg": _exif_bytes(),
        "exif_lens_switch.jpg": _exif_bytes(focal_mm=10.0, focal35=52, lens="Test Cam 2x"),
        "exif_none.jpg": None,
        "exif_rotated.jpg": _exif_bytes(orientation=6),
    }
    tiny = Image.new("RGB", (64, 48), (120, 130, 140))
    exif_fixtures = []
    for name, exif in exif_srcs.items():
        path = _save_jpeg(tiny, name, exif)
        exif_fixtures.append({"name": name, "jpeg_b64": base64.b64encode(path.read_bytes()).decode()})

    laps = [m["lap"] for m in metrics]
    median_lap = float(np.median(laps))
    # landslide/pipeline.py::_cull's own fail formulas — the ground truth
    # server verdict, independent of anything capture_metrics.js computes.
    blur_cut = max(6.0, 0.25 * median_lap)
    for m in metrics:
        m["serverBlurFail"] = bool(m["lap"] < blur_cut)
        m["serverExposureFail"] = bool(m["clip"] >= 0.5)

    expected = {
        "batchMedianLap": median_lap,
        "metrics": metrics,
        "exifFixtures": exif_fixtures,
    }
    (HERE / "expected.json").write_text(json.dumps(expected))
    print(f"wrote {len(metrics)} metric + {len(exif_fixtures)} exif fixtures to {HERE}")


if __name__ == "__main__":
    main()

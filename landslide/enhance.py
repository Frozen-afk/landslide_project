"""Radiometric enhancement for degraded field photos.

Deep shadows, washed-out gravel and wet low-contrast mud starve SIFT of
features. CLAHE on the luminance channel recovers local texture gradients
without blowing highlights, and a light unsharp mask restores high-frequency
edges lost to mild motion blur. Both operators are strictly radiometric —
they never move pixels — so features detected on an enhanced copy land at
the exact same pixel coordinates as on the original, and the reconstruction
poses apply to the unmodified photos unchanged.
"""
from __future__ import annotations

import cv2
import numpy as np
from pathlib import Path

from .sfm import IMAGE_EXTS, Log


def enhance_degraded_image(img_bgr: np.ndarray,
                           clahe_clip: float = 2.0) -> np.ndarray:
    """CLAHE (luminance only) + light unsharp mask. Purely radiometric."""
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
    l = clahe.apply(l)
    enhanced = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    gaussian = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=2.0)
    return cv2.addWeighted(enhanced, 1.5, gaussian, -0.5, 0)


def write_enhanced_dir(photos_dir: Path, dest_dir: Path,
                       jpeg_quality: int = 95, log: Log = print) -> Path:
    """Write CLAHE+unsharp copies of every photo under the SAME filenames.

    Same names + same geometry means a reconstruction built on `dest_dir`
    is directly usable with the original `photos_dir` (display, clicks,
    stereo). Reuses existing copies when the source set has not changed
    (mtime-based) so repeated ladder attempts don't redo the work.
    """
    photos_dir, dest_dir = Path(photos_dir), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(p for p in photos_dir.iterdir()
                      if p.suffix.lower() in IMAGE_EXTS):
        dst = dest_dir / src.name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        img = cv2.imread(str(src), cv2.IMREAD_COLOR)
        if img is None:
            log(f"[enhance] cannot read {src.name}, copying as-is")
            dst.write_bytes(src.read_bytes())
            continue
        cv2.imwrite(str(dst), enhance_degraded_image(img),
                    [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        n += 1
    if n:
        log(f"[enhance] wrote {n} enhanced copies in {dest_dir.name}")
    return dest_dir

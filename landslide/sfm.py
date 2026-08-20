"""Structure-from-Motion via pycolmap, plus camera/pose helpers.

Conventions used everywhere in this package:
  - world -> camera:  x_cam = R @ x_world + t
  - pixels are in the ORIGINAL image resolution of the stored photo files
    (uploads are EXIF-normalized before SfM, so display, clicks and
    features all live in the same coordinate frame).
"""
from __future__ import annotations

import os

# Bound glibc's per-thread malloc arenas BEFORE pycolmap/cv2 load: a dozen
# C++ threads each growing their own arena multiplies RSS far past what the
# logical allocations need. Must precede the heavy imports below.
os.environ.setdefault("MALLOC_ARENA_MAX", "4")

import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np
import pycolmap

from .geometry import camera_center

Log = Callable[[str], None]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# pycolmap runs one SIFT extractor PER THREAD, and each thread decodes a full
# photo and builds its octave pyramid — peak RAM scales with thread count
# (>24 GB with all-core extraction on ~7 MP phone photos). Four threads keep
# extraction in the ~2-3 GB range; wall-time cost is small because extraction
# is a minor share of total runtime (incremental mapping dominates and is
# left multithreaded).
SFM_THREADS = 4

# Below this median per-image keypoint count the photo set is considered
# low-contrast (shadows, wet mud, washed-out gravel) and the CLAHE-enhanced
# SIFT fallback attempt is worth a full re-run.
LOW_CONTRAST_KP = 2500

# A reconstruction can register most images yet rest on absurdly few 3D
# tracks (seen in the field: 19/21 images, 63 points — poses held up by
# ~3 points each, unusable for measurement). An attempt only counts as
# usable when the sparse cloud holds at least this many points per
# registered image; healthy sets sit 10-100x higher.
MIN_SPARSE_PER_IMG = 30


def _median_keypoints(db_path: Path) -> float:
    """Median per-image keypoint count straight from the COLMAP database.

    Reads the blob-table layout of this pycolmap (image_id, rows, ...) and
    the newer table_id-variant, so a COLMAP upgrade doesn't silently break
    the low-contrast gate.
    """
    import sqlite3
    con = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(keypoints)")]
        if not cols:
            return float("inf")
        if "table_id" in cols:
            rows = con.execute(
                "SELECT rows FROM keypoints WHERE table_id = 0").fetchall()
        else:
            rows = con.execute("SELECT rows FROM keypoints").fetchall()
    except Exception:
        return float("inf")
    finally:
        con.close()
    counts = [int(r[0]) for r in rows if r and r[0]]
    return float(np.median(counts)) if counts else 0.0


def _build_attempts(n: int, max_image_size: int) -> list[dict]:
    """SfM retry ladder, cheapest and most-likely-to-work first.

    Each attempt after the first addresses a distinct field-photo failure
    mode: denser sequential matching (weak overlap), CLAHE-enhanced copies
    with relaxed SIFT thresholds (low contrast / shadows / wet mud), then
    one shared camera (same-zoom sets with unstable per-image intrinsics).
    """
    attempts = [
        {"label": "default", "matcher": "exhaustive" if n <= 45 else "sequential",
         "overlap": 12, "size": max_image_size, "shared_camera": False},
        {"label": "dense sequential @3200px", "matcher": "sequential",
         "overlap": min(25, n - 1), "size": 3200, "shared_camera": False},
        {"label": "enhanced low-contrast", "matcher": "sequential",
         "overlap": min(25, n - 1), "size": max_image_size, "shared_camera": False,
         "enhanced": True, "peak": 0.0035, "edge": 15.0},
    ]
    if getattr(pycolmap.CameraMode, "SINGLE", None) is not None:
        attempts.append(
            {"label": "shared intrinsics", "matcher": "exhaustive" if n <= 60
             else "sequential", "overlap": 12, "size": max_image_size,
             "shared_camera": True})
    return attempts


def _prop(obj, name):
    """pycolmap mixes properties and methods across versions; normalize."""
    v = getattr(obj, name)
    return v() if callable(v) else v


def dist_coeffs(camera: pycolmap.Camera) -> np.ndarray:
    """Map a COLMAP camera model to an OpenCV distortion vector."""
    name = camera.model_name
    p = np.asarray(camera.params, np.float64)
    zeros = np.zeros(5)
    if name in ("SIMPLE_PINHOLE", "PINHOLE"):
        return zeros
    if name == "SIMPLE_RADIAL":
        return np.array([p[3], 0, 0, 0, 0])
    if name == "RADIAL":
        return np.array([p[3], p[4], 0, 0, 0])
    if name == "OPENCV":
        return np.array([p[4], p[5], p[6], p[7], 0])
    if name == "FULL_OPENCV":
        return np.array([p[4], p[5], p[6], p[7], p[8]])
    # RADIAL_TANGENTIAL and exotic models: treat as pinhole (rare on phones)
    return zeros


@dataclass
class ImageView:
    name: str
    image_id: int
    camera_id: int
    R: np.ndarray          # (3,3) world->cam
    t: np.ndarray          # (3,)  world->cam
    K: np.ndarray          # (3,3)
    dist: np.ndarray       # (5,)
    width: int
    height: int
    path: Path

    @property
    def center(self) -> np.ndarray:
        return camera_center(self.R, self.t)

    def project(self, points_world) -> tuple[np.ndarray, np.ndarray]:
        """Project (N,3) world points -> ((N,2) pixels, (N,) depth in cam)."""
        pts = np.asarray(points_world, np.float64).reshape(-1, 3)
        Xc = pts @ self.R.T + self.t
        rvec, _ = cv2.Rodrigues(self.R)
        uv, _ = cv2.projectPoints(pts, rvec, self.t, self.K, self.dist)
        return uv.reshape(-1, 2), Xc[:, 2]


@dataclass
class ReconCtx:
    rec: pycolmap.Reconstruction
    views: dict                       # name -> ImageView
    sparse: np.ndarray                # (N,3) model units
    sparse_colors: np.ndarray         # (N,3) uint8
    photos_dir: Path
    workdir: Path
    scale: float = 1.0                # meters per model unit
    scale_info: dict = field(default_factory=dict)
    dense: dict | None = None         # {'points': (M,3), 'colors': (M,3)} model units

    @property
    def scaled(self) -> bool:
        return self.scale_info.get("applied", False)

    def cloud(self, dense: bool = True):
        """Return (points, colors) in model units, falling back to sparse."""
        if dense and self.dense is not None and len(self.dense["points"]) > 0:
            return self.dense["points"], self.dense["colors"]
        return self.sparse, self.sparse_colors


def count_photos(photos_dir: Path) -> int:
    return sum(1 for p in photos_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def _attempt_score(nreg: int, npts: int) -> tuple[int, int, int]:
    """Rank SfM attempts: usable geometry first, then images, then tracks.

    A high registration count with a starved sparse cloud (poses on a
    handful of tracks) must lose to a slightly smaller but well-triangulated
    model, so usability dominates the comparison.
    """
    usable = 1 if npts >= MIN_SPARSE_PER_IMG * max(nreg, 1) else 0
    return (usable, nreg, npts)


def reconstruct(photos_dir, workdir, max_image_size: int = 2400,
                log: Log = print, reuse: bool = False) -> ReconCtx:
    """Run the full COLMAP incremental SfM pipeline on a folder of photos.

    If too few images register with the fast default settings, progressively
    more expensive / more constrained attempts are made (denser sequential
    matching at higher resolution, CLAHE-enhanced copies for low-contrast
    sets, then one shared camera when the photos were all taken with the
    same zoom but EXIF focals are missing/unstable). The ladder only stops
    early on an attempt that registers ~all images AND triangulates at
    least MIN_SPARSE_PER_IMG points per image.
    """
    photos_dir = Path(photos_dir)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "database.db"

    if reuse and db_path.exists() and (workdir / "sparse").exists():
        try:
            rec = _largest_reconstruction(None, workdir / "sparse")
            log(f"[sfm] reusing cached reconstruction "
                f"({_prop(rec, 'num_reg_images')} images)")
            return build_ctx(rec, photos_dir, workdir)
        except Exception:
            log("[sfm] cached reconstruction unusable, redoing SfM")

    n = count_photos(photos_dir)
    if n < 3:
        raise ValueError(f"need at least 3 photos, found {n}")

    attempts = _build_attempts(n, max_image_size)

    best = None      # (score, rec, label, kp)
    for i, a in enumerate(attempts):
        if a.get("enhanced") and best is not None \
                and best[3] >= LOW_CONTRAST_KP:
            log(f"[sfm] skipping '{a['label']}' — {best[3]:.0f} median "
                "keypoints/image means contrast is not the problem")
            continue
        try:
            rec, nreg, kp = _run_attempt(
                photos_dir, workdir, n,
                matcher=a["matcher"], overlap=a["overlap"],
                max_image_size=a["size"],
                shared_camera=a["shared_camera"],
                enhanced=a.get("enhanced", False),
                peak=a.get("peak"), edge=a.get("edge"), log=log)
        except Exception as e:
            log(f"[sfm] attempt '{a['label']}' failed: {e}")
            continue
        score = _attempt_score(nreg, len(rec.points3D))
        if best is None or score > best[0]:
            best = (score, rec, a["label"], kp)
        done = (score[0] == 1 and nreg >= max(3, int(0.9 * n)))
        if done:
            break
        if i < len(attempts) - 1:
            why = (f"{nreg}/{n} images" if score[0] == 1 else
                   f"only {len(rec.points3D)} sparse points for {nreg} images")
            log(f"[sfm] attempt '{a['label']}' gave {why} — retrying with "
                f"'{attempts[i + 1]['label']}'")

    if best is None:
        raise RuntimeError(
            "SfM failed to produce any reconstruction. Check that photos have "
            "60-80% overlap, are sharp, and the scene has texture.")

    _, best_rec, best_label, _ = best
    best_nreg = _prop(best_rec, "num_reg_images")
    best_pts = len(best_rec.points3D)
    log(f"[sfm] reconstruction done ({best_label}): {best_nreg}/{n} images "
        f"registered, {best_pts} sparse points")
    if best_nreg < max(3, int(0.5 * n)):
        raise RuntimeError(
            f"only {best_nreg}/{n} images registered — photos need more overlap/texture, "
            "or too few distinct viewpoints")
    if best_pts < MIN_SPARSE_PER_IMG * max(best_nreg, 1):
        log(f"[sfm] warning: thin geometry ({best_pts} points for "
            f"{best_nreg} images) — volume accuracy will be limited")
    return build_ctx(best_rec, photos_dir, workdir)


def _run_attempt(photos_dir: Path, workdir: Path, n: int, matcher: str,
                 overlap: int, max_image_size: int, shared_camera: bool,
                 log: Log, enhanced: bool = False,
                 peak: float | None = None, edge: float | None = None):
    """One full SfM pass; wipes any previous database first.

    enhanced=True runs the pass on CLAHE+unsharp copies (same filenames,
    same geometry — the poses apply to the originals unchanged) with the
    SIFT peak/edge thresholds relaxed to keep weak low-contrast texture.
    Returns (reconstruction, n_registered, median_keypoints_per_image).
    """
    db_path = workdir / "database.db"
    if db_path.exists():
        db_path.unlink()
    db_path.touch()   # 4.x requires the file to exist; schema is created on open
    out_dir = workdir / "sparse"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    img_dir = photos_dir
    if enhanced:
        from .enhance import write_enhanced_dir
        img_dir = write_enhanced_dir(photos_dir, workdir / "enhanced", log=log)

    log(f"[sfm] importing {n} photos"
        + (" (enhanced copies)" if enhanced else ""))
    mode = (pycolmap.CameraMode.SINGLE if shared_camera else
            getattr(pycolmap.CameraMode, "PER_IMAGE", pycolmap.CameraMode.AUTO))
    pycolmap.import_images(database_path=str(db_path), image_path=str(img_dir),
                           camera_mode=mode)

    log("[sfm] extracting SIFT features"
        + (f" (peak {peak}, edge {edge})" if enhanced else ""))
    fo = pycolmap.FeatureExtractionOptions()
    try:
        fo.sift.max_image_size = max_image_size
    except Exception:
        pass
    try:
        # per-thread SIFT cost grows with the working resolution: the 3200px
        # retry attempt runs with half the threads to hold the same ceiling
        threads = min(SFM_THREADS, os.cpu_count() or 1)
        if max_image_size > 2400:
            threads = max(2, threads // 2)
        fo.num_threads = threads
    except Exception:
        pass
    if peak is not None:
        try:
            fo.sift.peak_threshold = peak
        except Exception:
            pass
    if edge is not None:
        try:
            fo.sift.edge_threshold = edge
        except Exception:
            pass
    pycolmap.extract_features(database_path=str(db_path), image_path=str(img_dir),
                              extraction_options=fo)

    matching = pycolmap.FeatureMatchingOptions()
    try:
        matching.num_threads = min(SFM_THREADS, os.cpu_count() or 1)
    except Exception:
        pass
    if matcher == "exhaustive":
        log("[sfm] exhaustive matching")
        pycolmap.match_exhaustive(database_path=str(db_path),
                                  matching_options=matching)
    else:
        log(f"[sfm] sequential matching (overlap {overlap}, capture order)")
        po = pycolmap.SequentialPairingOptions()
        try:
            po.overlap = overlap
            po.loop_detection = False
        except Exception:
            pass
        pycolmap.match_sequential(database_path=str(db_path),
                                  matching_options=matching,
                                  pairing_options=po)

    log("[sfm] incremental mapping (this is the slow part)")
    result = pycolmap.incremental_mapping(database_path=str(db_path),
                                          image_path=str(img_dir),
                                          output_path=str(out_dir))
    rec = _largest_reconstruction(result, out_dir)
    return rec, _prop(rec, "num_reg_images"), _median_keypoints(db_path)


def _largest_reconstruction(result, out_dir: Path) -> pycolmap.Reconstruction:
    cands: list = []
    if isinstance(result, pycolmap.ReconstructionManager):
        for i in range(result.size):
            cands.append(result.get(i))
    elif isinstance(result, dict):
        cands = list(result.values())
    if not cands and out_dir.exists():
        for sub in sorted(out_dir.iterdir()):
            if sub.is_dir():
                try:
                    cands.append(pycolmap.Reconstruction(str(sub)))
                except Exception:
                    continue
    cands = [r for r in cands if _prop(r, "num_reg_images") > 0]
    if not cands:
        raise RuntimeError(
            "SfM failed to produce any reconstruction. Check that photos have "
            "60-80% overlap, are sharp, and the scene has texture.")
    return max(cands, key=lambda r: _prop(r, "num_reg_images"))


def build_ctx(rec: pycolmap.Reconstruction, photos_dir: Path,
              workdir: Path) -> ReconCtx:
    views: dict[str, ImageView] = {}
    for img in rec.images.values():
        if not _prop(img, "has_pose"):
            continue
        cfw = _prop(img, "cam_from_world")
        R = np.asarray(cfw.rotation.matrix(), np.float64)
        t = np.asarray(cfw.translation, np.float64).ravel()
        cam = rec.cameras[img.camera_id]
        views[img.name] = ImageView(
            name=img.name,
            image_id=img.image_id,
            camera_id=img.camera_id,
            R=R, t=t,
            K=np.asarray(cam.calibration_matrix(), np.float64),
            dist=dist_coeffs(cam),
            width=int(cam.width), height=int(cam.height),
            path=Path(photos_dir) / img.name,
        )

    pts = np.array([np.asarray(p.xyz, np.float64) for p in rec.points3D.values()]) \
        .reshape(-1, 3)
    cols = np.array([np.asarray(p.color, np.float64) for p in rec.points3D.values()]) \
        .reshape(-1, 3)
    if len(cols) == 0 or cols.max() < 2:  # colors not extracted yet
        try:
            rec.extract_colors_for_all_images(str(photos_dir))
            cols = np.array([np.asarray(p.color, np.float64)
                             for p in rec.points3D.values()]).reshape(-1, 3)
        except Exception:
            cols = np.full_like(pts, 128.0)
    return ReconCtx(rec=rec, views=views, sparse=pts, sparse_colors=cols,
                    photos_dir=Path(photos_dir), workdir=Path(workdir))


def image_metadata(ctx: ReconCtx) -> list[dict]:
    """Per-image info for the UI (sorted by name = capture order)."""
    out = []
    for name in sorted(ctx.views):
        v = ctx.views[name]
        img = ctx.rec.images[v.image_id]
        try:
            npts = int(_prop(img, "num_points3D"))
        except Exception:
            npts = 0
        out.append({"name": name, "width": v.width, "height": v.height,
                    "points": npts})
    return out


def covisibility_pairs(rec: pycolmap.Reconstruction) -> Counter:
    """Counter over (image_id_a, image_id_b) of shared 3D point tracks."""
    cnt: Counter = Counter()
    for p3 in rec.points3D.values():
        ids = []
        for e in p3.track.elements:
            iid = getattr(e, "image_id", None)
            if iid is None and isinstance(e, tuple):
                iid = e[0]
            if iid is not None:
                ids.append(int(iid))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                cnt[(a, b)] += 1
    return cnt

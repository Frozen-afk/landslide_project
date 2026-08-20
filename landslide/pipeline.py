"""High-level orchestration: photo import/normalization, measure, spec runs."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageOps

from .densify import dense_cloud, estimate_up
from .sfm import IMAGE_EXTS, ReconCtx, count_photos, reconstruct
from .volume import prism_volume, select_region
from .viz import draw_overlay, draw_overlay_image, heat_topdown


def _photo_metrics(im, max_side: int = 900):
    """Sharpness, exposure clipping, dhash of a PIL image.

    Returns (laplacian_var, clipped_fraction, dhash). `clipped_fraction` is
    the share of pixels pinned to black or white — a frame half-blown by a
    stuck auto-exposure carries no recoverable texture in those regions no
    matter how sharp the rest is.
    """
    import cv2
    g = im.convert("L")
    s = min(1.0, max_side / max(im.size))
    if s < 1.0:
        g = g.resize((max(1, round(g.width * s)), max(1, round(g.height * s))))
    arr = np.asarray(g)
    lap = float(cv2.Laplacian(arr, cv2.CV_64F).var())
    clipped = float(((arr <= 2) | (arr >= 253)).mean())
    small = cv2.resize(arr, (9, 8))
    dhash = (small[:, 1:] > small[:, :-1]).flatten()
    return lap, clipped, dhash


# a frame with at least this fraction of pixels pinned to the histogram rails
# is unusable for SfM; deliberately harsh so only hopeless frames go
EXPOSURE_CLIP_FRAC = 0.5


def _cull(metrics: list, log) -> list[int]:
    """Indices of photos to drop: bad exposure, blur, near-duplicate neighbours.

    Over/under-exposed frames (half the histogram pinned at the rails) go
    first: SIFT has nothing to anchor on where the sensor clipped. Motion
    blur starves SIFT of features and produces noisy stereo edges;
    near-duplicate shots add no parallax and slow matching down. Blur
    thresholds are deliberately conservative (the 0.25×median guard keeps
    uniformly-soft sets intact, at most half the upload goes, ≥ 3 photos
    always survive); near-duplicates are exempt from the cap — they are
    redundant by definition.
    """
    n = len(metrics)
    laps = np.array([m[0] for m in metrics])
    clips = np.array([m[1] for m in metrics])
    med = float(np.median(laps))
    reasons: dict[int, str] = {}

    drop = {i for i in range(n) if clips[i] >= EXPOSURE_CLIP_FRAC}
    for i in drop:
        reasons[i] = "over/under-exposed"
    blur_cut = max(6.0, 0.25 * med)
    blur_fail = {i for i in range(n) if laps[i] < blur_cut}
    if len(blur_fail) > 0.5 * n:                   # metric failure, not blur
        keep_n = n - int(np.ceil(0.5 * n))
        blur_drop = set(np.argsort(laps)[:keep_n].tolist())
    else:
        blur_drop = blur_fail - drop
    for i in blur_drop:
        drop.add(i)
        reasons[i] = "blurry"

    # near-duplicates of the previous kept frame: drop the blurrier of the two
    prev = -1
    for i in range(n):
        if i in drop:
            continue
        if prev >= 0:
            ham = int(np.count_nonzero(metrics[i][2] != metrics[prev][2]))
            if ham <= 3:
                if laps[i] <= laps[prev]:
                    drop.add(i)
                    reasons[i] = "near-duplicate"
                    continue
                drop.add(prev)
                reasons[prev] = "near-duplicate"
        prev = i
    if n - len(drop) < 3:      # always leave 3 survivors: best exposure+sharpness
        order = np.lexsort((laps, clips))          # worst exposure/sharpness first
        drop -= set(order[-3:].tolist())
    for i in sorted(drop):
        log(f"[import] dropping {metrics[i][3].name}: {reasons[i]}")
    if drop:
        counts = Counter(reasons.values())
        log("[import] quality gate: "
            + ", ".join(f"{c} {r}" for r, c in counts.most_common())
            + f" — {n - len(drop)} of {n} photos kept")
    return sorted(drop)


def import_photos(sources: Iterable[Path], photos_dir: Path, max_side: int = 3000,
                  jpeg_quality: int = 92, log=print) -> list[str]:
    """Normalize photos for SfM: apply EXIF orientation, downscale, re-encode.

    Output names are prefixed with an order index so lexicographic order equals
    capture order (sequential matching relies on it). Blurry frames and
    near-duplicate neighbours are culled before SfM. Returns stored names.
    """
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    photos_dir = Path(photos_dir)
    photos_dir.mkdir(parents=True, exist_ok=True)
    names, metrics = [], []
    for i, src in enumerate(sources):
        src = Path(src)
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                s = min(1.0, max_side / max(im.size))
                if s < 1.0:
                    im = im.resize((max(1, round(im.width * s)),
                                    max(1, round(im.height * s))),
                                   Image.LANCZOS)
                name = f"{i:03d}_{src.stem}.jpg"
                im.save(photos_dir / name, "JPEG", quality=jpeg_quality)
                names.append(name)
                metrics.append((*_photo_metrics(im), src))
        except Exception as e:
            log(f"[import] skipping {src.name}: {e}")
    if len(names) < 3:
        raise ValueError("need at least 3 readable photos")
    drop = set(_cull(metrics, log))
    for i in drop:
        (photos_dir / names[i]).unlink(missing_ok=True)
    return [n for i, n in enumerate(names) if i not in drop]


def ensure_reconstruction(photos_dir, workdir, log=print) -> ReconCtx:
    if not count_photos(Path(photos_dir)):
        raise ValueError(f"no photos in {photos_dir}")
    return reconstruct(photos_dir, workdir, log=log)


def measure(ctx: ReconCtx, image_name: str | None, polygon, dense: bool = True,
            rim_px: float = 12.0, rim_inner_px: float | None = None,
            mode: str = "photo", ortho: dict | None = None,
            artifacts_dir=None, log=print, save_cloud: bool = False) -> dict:
    """Full measurement of one marked region. Returns the result dict.

    mode="photo": polygon is drawn on `image_name` (pixel coords).
    mode="ortho": polygon is drawn on the top-down orthophoto rendered by
    landslide.ortho.render_orthophoto; `ortho` is its metadata dict. Region
    selection then happens in true ground coordinates — no parallax.
    """
    if not ctx.scale_info.get("applied"):
        raise RuntimeError("metric scale not set yet (mark the reference first)")
    polygon = np.asarray(polygon, np.float64)
    if len(polygon) < 3:
        raise ValueError("polygon needs at least 3 vertices")

    if dense:
        dense_cloud(ctx, log=log)
    pts, _ = ctx.cloud(dense=dense)

    if mode == "ortho":
        from .ortho import select_region_ortho
        if not ortho:
            raise ValueError("mode='ortho' needs the orthophoto metadata "
                             "(render it first)")
        interior, rim, rinfo = select_region_ortho(ctx, ortho, polygon, log=log)
        up = np.asarray(ortho["up"], np.float64)
        res = prism_volume(pts[interior] * ctx.scale, pts[rim] * ctx.scale,
                           log=log, up=up)
        res["mode"] = "ortho"
        res["rim_band_m"] = [rinfo["rim_inner_m"], rinfo["rim_outer_m"]]
    elif mode == "photo":
        if image_name not in ctx.views:
            raise ValueError(f"image '{image_name}' is not part of the reconstruction")
        view, uv, interior, rim = select_region(ctx, image_name, polygon,
                                                rim_px, rim_inner_px)
        up = estimate_up(ctx.views, ctx.sparse)
        res = prism_volume(pts[interior] * ctx.scale, pts[rim] * ctx.scale,
                           log=log, up=up)
        res["mode"] = "photo"
        res["image"] = image_name
        res["rim_band_px"] = [rim_px * 0.5 if rim_inner_px is None else rim_inner_px,
                              (rim_px * 0.5 if rim_inner_px is None else rim_inner_px) + rim_px]
    else:
        raise ValueError(f"unknown mode {mode!r}")
    debug = res.pop("_debug")

    res["polygon_px"] = polygon.tolist()
    res["scale"] = ctx.scale
    res["scale_method"] = ctx.scale_info.get("method")
    res["cloud"] = ("dense" if (dense and ctx.dense is not None and
                                len(ctx.dense["points"]) > 0) else "sparse")
    res["n_cloud_points"] = int(len(pts))

    # propagated uncertainty: datum roughness over the footprint, plus the
    # scale error acting multiplicatively on the volume (2-sigma)
    scale_rel = ctx.scale_info.get("scale_rel_error")
    if scale_rel:
        res["scale_rel_error"] = float(scale_rel)
        res["est_volume_error_m3"] = float(
            res["datum_rms_m"] * res["area_m2"] + 2.0 * scale_rel * abs(res["net_volume_m3"]))
    for w in ctx.scale_info.get("warnings") or []:
        res.setdefault("warnings", []).append(f"scale: {w}")

    if artifacts_dir is not None:
        artifacts_dir = Path(artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        if mode == "ortho":
            ortho_path = artifacts_dir / "ortho.jpg"
            if ortho_path.exists():
                draw_overlay_image(ortho_path, polygon,
                                   out_path=artifacts_dir / "overlay.jpg")
            res["artifacts"] = ["overlay.jpg", "heightmap.png"]
        else:
            marker_px = ctx.scale_info.get("marker_px", {}).get(image_name)
            draw_overlay(view, polygon, marker_pts=marker_px,
                         out_path=artifacts_dir / "overlay.jpg")
            res["artifacts"] = ["overlay.jpg", "heightmap.png"]
        heat_topdown(debug["uv2"], debug["h"],
                     artifacts_dir / "heightmap.png")
        if save_cloud:
            _save_ply(pts * ctx.scale, ctx.cloud(dense=dense)[1],
                      artifacts_dir / "pointcloud.ply")
            res["artifacts"].append("pointcloud.ply")
    return res


def _save_ply(points: np.ndarray, colors: np.ndarray, path: Path) -> None:
    with open(path, "wb") as f:
        f.write((f"ply\nformat binary_little_endian 1.0\n"
                 f"element vertex {len(points)}\n"
                 "property float x\nproperty float y\nproperty float z\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\n"
                 "end_header\n").encode())
        rec = np.zeros(len(points), dtype=[("xyz", "<f4", 3), ("rgb", "u1", 3)])
        rec["xyz"] = points.astype(np.float32)
        rec["rgb"] = colors.astype(np.uint8)
        f.write(rec.tobytes())


def run_spec(spec: dict, log=print) -> dict:
    """Run a full job from a JSON-able spec (used by the CLI).

    spec = {
      photos_dir, out_dir?, reuse_dense?,
      scale: {"method": "aruco", "side_m", "dict"?, "id"?} or
             {"method": "manual", "length_m", "a": {...}, "b": {...}},
      region: {"image", "polygon": [[x,y],...], "dense"?, "rim_px"?},
      save_cloud?
    }
    """
    from .scaling import aruco_scale, manual_scale

    photos_dir = Path(spec["photos_dir"])
    out_dir = Path(spec.get("out_dir") or (photos_dir.parent / "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"

    ctx = ensure_reconstruction(photos_dir, work, log=log)

    sc = spec["scale"]
    if sc["method"] == "aruco":
        aruco_scale(ctx, side_m=sc["side_m"], dict_name=sc.get("dict", "auto"),
                    marker_id=sc.get("id"), log=log)
    elif sc["method"] == "manual":
        manual_scale(ctx, sc["a"], sc["b"], sc["length_m"], log=log)
    else:
        raise ValueError(f"unknown scale method {sc['method']}")

    reg = spec["region"]
    mode = reg.get("mode", "photo")
    ortho = None
    if mode == "ortho":
        from .ortho import render_orthophoto
        dense_cloud(ctx, log=log)
        _, ortho = render_orthophoto(
            ctx, jpg_path=out_dir / "artifacts" / "ortho.jpg",
            meta_path=out_dir / "artifacts" / "ortho.json", log=log)
    res = measure(ctx, reg.get("image"), reg["polygon"],
                  dense=reg.get("dense", True), rim_px=reg.get("rim_px", 12.0),
                  rim_inner_px=reg.get("rim_inner_px"),
                  mode=mode, ortho=ortho,
                  artifacts_dir=out_dir / "artifacts", log=log,
                  save_cloud=spec.get("save_cloud", True))
    with open(out_dir / "result.json", "w") as f:
        json.dump(res, f, indent=2)
    log(f"[done] net {res['net_volume_m3']:.2f} m^3 | cut {res['cut_volume_m3']:.2f} | "
        f"fill {res['fill_volume_m3']:.2f} | area {res['area_m2']:.1f} m^2")
    return res

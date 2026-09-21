"""Metric scaling of the reconstruction from a reference of known size.

Two modes:
  - ArUco auto: a printed marker of known side length is detected in every
    photo, its 4 corners are triangulated into the model, and the scale is
    side_real / side_model.
  - Manual: the user clicks the two endpoints of a known-length segment
    (ruler, pole, tape) in TWO photos; the endpoints are triangulated.

Both modes estimate a relative scale uncertainty from the reprojection
residual of the triangulated reference, which the volume step folds into the
reported volume uncertainty. Bad input (misclicks, near-parallel viewing
rays) is rejected with a diagnostic message instead of silently producing a
wrong scale.
"""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import camera_center, triangulate_dlt, undistort_normalized
from .sfm import ImageView, Log, ReconCtx

ARUCO_DICTS = ["DICT_6X6_250", "DICT_5X5_100", "DICT_4X4_50", "DICT_7X7_250",
               "DICT_ARUCO_ORIGINAL"]


def _available_dicts(dict_name: str) -> list[str]:
    """Requested dictionaries that this OpenCV build actually provides."""
    names = ARUCO_DICTS if dict_name == "auto" else [dict_name]
    return [d for d in names if hasattr(cv2.aruco, d)]


def _clip_rel(err: float) -> float:
    """Clamp a relative-error estimate to a sane band."""
    return float(np.clip(err, 0.005, 0.5))


def _load_gray(path, max_side: int = 2200):
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise IOError(f"cannot read {path}")
    s = min(1.0, max_side / max(img.shape[:2]))
    if s < 1.0:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img, gray, s


def detect_marker_corners(gray, s: float, dict_name: str,
                          marker_id: int | None = None):
    """Return {marker_id: (4,2) corners in full-res pixel coords}."""
    ar = cv2.aruco
    dictionary = ar.getPredefinedDictionary(getattr(ar, dict_name))
    detector = ar.ArucoDetector(dictionary, ar.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(gray)
    out = {}
    if ids is None:
        return out
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01)
    for crn, mid in zip(corners, ids.flatten()):
        if marker_id is not None and int(mid) != int(marker_id):
            continue
        crn = crn.reshape(-1, 1, 2).astype(np.float32)
        try:
            cv2.cornerSubPix(gray, crn, (5, 5), (-1, -1), crit)
        except cv2.error:
            pass
        out[int(mid)] = crn.reshape(-1, 2).astype(np.float64) / s
    return out


def _fit_square_side(corners3d: np.ndarray) -> float:
    """Model-unit side length of the rigid square that best explains the
    four triangulated corners (similarity Procrustes fit to a unit square),
    used instead of the mean of four independently-noisy edge lengths so a
    single off corner (motion blur, a grazing-angle detection) can't drag
    the scale as hard as it drags one edge.
    """
    c = corners3d.mean(axis=0)
    _, _, Vt = np.linalg.svd(corners3d - c, full_matrices=False)
    basis = Vt[:2]                              # best-fit plane's in-plane axes
    uv = (corners3d - c) @ basis.T              # (4, 2) planar coordinates
    template = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    uv_c = uv - uv.mean(axis=0)
    t_c = template - template.mean(axis=0)
    denom = float((t_c ** 2).sum())
    if denom <= 0:
        return 0.0
    _, S, _ = np.linalg.svd(uv_c.T @ t_c)
    return float(S.sum() / denom)


def _pnp_scale_estimate(view: ImageView, corners_px: np.ndarray, side_m: float,
                        corners3d_model: np.ndarray) -> float | None:
    """Independent per-view scale estimate: solvePnP recovers the camera's
    METRIC distance to the marker from its known real-world side length
    alone; the ratio to the model-unit distance (from the joint-DLT
    triangulation) is a scale estimate that shares no computation with the
    primary one, so the spread across views is a genuine cross-check.
    """
    obj = np.array([[0.0, 0.0, 0.0], [side_m, 0.0, 0.0],
                    [side_m, side_m, 0.0], [0.0, side_m, 0.0]])
    try:
        ok, rvec, tvec = cv2.solvePnP(
            obj, corners_px.reshape(-1, 1, 2).astype(np.float64), view.K, view.dist)
    except cv2.error:
        return None
    if not ok:
        return None
    R_pnp, _ = cv2.Rodrigues(rvec)
    cam_center_m = -(R_pnp.T @ tvec).ravel()
    dist_m = float(np.linalg.norm(cam_center_m - obj.mean(axis=0)))
    dist_model = float(np.linalg.norm(view.center - corners3d_model.mean(axis=0)))
    if dist_model <= 1e-12 or not np.isfinite(dist_m) or dist_m <= 0:
        return None
    return dist_m / dist_model


def _triangulate_pixels(observations, ctx_views: dict[str, ImageView]):
    """observations: list of (view_name, (N,2) pixels) -> (N,3) world."""
    views = []
    n = len(observations[0][1])
    for name, pixels in observations:
        if not np.isfinite(pixels).all():
            raise ValueError("reference pixel coordinates must be finite")
        v = ctx_views[name]
        uv = undistort_normalized(pixels, v.K, v.dist)
        if not np.isfinite(uv).all():
            raise RuntimeError("reference undistortion produced nonfinite coordinates")
        assert len(uv) == n
        views.append((v.R, v.t, uv))
    return triangulate_dlt(views)


def aruco_scale(ctx: ReconCtx, side_m: float, dict_name: str = "auto",
                marker_id: int | None = None, log: Log = print) -> dict:
    """Detect an ArUco marker across all views and set ctx.scale.

    dict_name="auto" tries every known dictionary and keeps the marker seen
    in the most views (you may not know which marker you printed).
    """
    if not np.isfinite(side_m) or side_m <= 0:
        raise ValueError("marker side must be finite and positive")
    if dict_name != "auto" and dict_name not in ARUCO_DICTS:
        raise ValueError(f"unknown ArUco dict {dict_name}")
    dicts = _available_dicts(dict_name)
    if not dicts:
        raise RuntimeError(f"ArUco dictionary '{dict_name}' not available in "
                           "this OpenCV build")

    # (dict, marker_id) -> {view_name: corners}; images are decoded once
    found: dict[tuple[str, int], dict[str, np.ndarray]] = {}
    for name in sorted(ctx.views):
        v = ctx.views[name]
        _, gray, s = _load_gray(v.path)
        for d in dicts:
            for mid, corners in detect_marker_corners(gray, s, d, marker_id).items():
                found.setdefault((d, mid), {})[name] = corners
    if not found:
        raise RuntimeError(
            f"no ArUco marker found in any photo (tried {', '.join(dicts)}). "
            "Print the provided marker file, or use manual scaling.")

    best_dict, best_mid = max(found, key=lambda k: len(found[k]))
    per_view = found[(best_dict, best_mid)]
    views_with = sorted(per_view)
    if len(views_with) < 2:
        raise RuntimeError("marker visible in only one photo — need at least two")

    obs = [(n, per_view[n]) for n in views_with]
    corners3d = _triangulate_pixels(obs, ctx.views)   # (4,3)
    if not np.isfinite(corners3d).all():
        raise RuntimeError("marker triangulation produced nonfinite corners")

    # per-view outlier rejection: a single mis-ID'd or motion-blurred
    # detection can drag every corner if it's just averaged in, so views
    # whose reprojection is far worse than the pack are dropped and the
    # triangulation is refit on the rest (needs >=3 views to safely drop one
    # and still have >=2 left for triangulation).
    dropped_views: list[str] = []
    if len(views_with) >= 3:
        resid_per_view = {}
        for n in views_with:
            uv, depth = ctx.views[n].project(corners3d)
            if np.isfinite(depth).all() and np.all(depth > 0) and np.isfinite(uv).all():
                resid_per_view[n] = float(np.linalg.norm(uv - per_view[n], axis=1).mean())
        if len(resid_per_view) >= 3:
            med = float(np.median(list(resid_per_view.values())))
            bad = [n for n, r in resid_per_view.items() if med > 0 and r > 3 * med]
            if bad and len(views_with) - len(bad) >= 2:
                dropped_views = bad
                views_with = [n for n in views_with if n not in bad]
                obs = [(n, per_view[n]) for n in views_with]
                corners3d = _triangulate_pixels(obs, ctx.views)
                if not np.isfinite(corners3d).all():
                    raise RuntimeError("marker triangulation produced nonfinite corners")
                log(f"[scale] dropping {len(bad)} view(s) with outlier marker "
                    f"reprojection ({', '.join(bad)}) and refitting")

    sides = [float(np.linalg.norm(corners3d[i] - corners3d[(i + 1) % 4]))
             for i in range(4)]
    sides = np.array(sides)
    mean_side = float(sides.mean())
    if mean_side <= 0 or not np.isfinite(mean_side):
        raise RuntimeError("marker triangulation degenerated")
    spread = float(sides.std() / mean_side)
    # squareness-enforced side length: a rigid-square Procrustes fit to the
    # four corners, less sensitive to one noisy corner than a plain mean of
    # four edges (each edge shares 2 of the 4 possibly-noisy corners).
    square_side = _fit_square_side(corners3d)
    if square_side <= 0 or not np.isfinite(square_side):
        raise RuntimeError("marker triangulation degenerated")

    # reprojection residual of the triangulated corners -> scale uncertainty
    reproj, px_side = [], []
    for n in views_with:
        uv, depth = ctx.views[n].project(corners3d)
        if not np.isfinite(depth).all() or np.any(depth <= 0):
            raise RuntimeError("marker corners must be in front of every observing "
                               "camera with finite positive depth")
        if not np.isfinite(uv).all():
            raise RuntimeError("marker reprojection produced nonfinite coordinates")
        reproj.append(np.linalg.norm(uv - per_view[n], axis=1))
        px_side.append(np.linalg.norm(np.diff(np.vstack(
            [per_view[n], per_view[n][:1]]), axis=0), axis=1).mean())
    reproj_px_mean = float(np.concatenate(reproj).mean())
    mean_px_side = float(np.mean(px_side))
    if (not np.isfinite([spread, reproj_px_mean, mean_px_side]).all()
            or mean_px_side <= 0):
        raise RuntimeError("marker reprojection quality is not finite or degenerated")
    rel_err = _clip_rel(max(spread, reproj_px_mean / mean_px_side))

    # per-view PnP cross-check: an independent scale estimate per view that
    # shares no computation with the joint-DLT one above; reported, not used
    # in the primary scale, so a bug in one path doesn't silently mask itself
    pnp_scales = []
    for n in views_with:
        est = _pnp_scale_estimate(ctx.views[n], per_view[n], side_m, corners3d)
        if est is not None:
            pnp_scales.append(est)
    pnp_spread = (float(np.std(pnp_scales) / np.mean(pnp_scales))
                  if len(pnp_scales) >= 2 else 0.0)

    scale = float(side_m / square_side)
    if not np.isfinite(scale) or scale <= 0:
        raise RuntimeError("marker scale must be finite and positive")
    ctx.scale = scale
    ctx.scale_info = {
        "applied": True, "method": "aruco", "dict": best_dict,
        "marker_id": int(best_mid), "side_m": float(side_m),
        "views_used": views_with, "model_sides": sides.tolist(),
        "side_spread_rel": spread, "scale": scale,
        "reproj_px_mean": reproj_px_mean,
        "scale_rel_error": rel_err,
        "marker_px": {n: per_view[n].tolist() for n in views_with},
        "dropped_views": dropped_views,
        "pnp_scale_estimates": pnp_scales,
        "pnp_scale_spread": pnp_spread,
        # metric corners of the marker IN THE MODEL FRAME: if the same
        # physical marker is present in a second survey of the site, these
        # four points anchor an exact rigid registration between the two
        # reconstructions (both are metric) — no ICP guesswork needed
        "marker_corners_m": (corners3d * scale).tolist(),
    }
    log(f"[scale] ArUco {best_dict} id={best_mid} in {len(views_with)} views; "
        f"sides(model)={np.round(sides, 3).tolist()} spread={spread:.3f}; "
        f"square-fit side={square_side:.4g}; "
        f"reproj={reproj_px_mean:.2f}px; scale={scale:.6g} m/unit "
        f"(±{rel_err * 100:.1f}%); PnP cross-check spread={pnp_spread * 100:.1f}%")
    if spread > 0.05:
        log("[scale] warning: corner sides differ >5% — marker may be blurred "
            "or seen at a grazing angle")
    if pnp_spread > 0.10:
        log("[scale] warning: per-view PnP scale cross-check spread "
            f"{pnp_spread * 100:.0f}% — the marker geometry may not be a "
            f"clean flat square of the stated side length")
    return ctx.scale_info


def _point2(p) -> list[float]:
    """Accept a clicked point as [x, y] or {x, y} (what the web UI sends)."""
    if isinstance(p, dict):
        return [float(p["x"]), float(p["y"])]
    return [float(p[0]), float(p[1])]


def manual_scale(ctx: ReconCtx, spec_a: dict, spec_b: dict, length_m: float,
                 log: Log = print) -> dict:
    """Scale from a known segment clicked in two photos.

    spec: {"image": name, "p1": [x, y], "p2": [x, y]} in full-res pixels.

    The triangulated endpoints are reprojected back into both photos; a large
    residual means the clicks don't correspond to the same physical points
    (misclick in one photo) and the scale is rejected rather than silently
    wrong. A tiny viewing-ray angle means the two photos are near-duplicates
    and the triangulated depth — hence the scale — is ill-conditioned.
    """
    if not np.isfinite(length_m) or length_m <= 0:
        raise ValueError("reference length must be finite and positive")
    for spec in (spec_a, spec_b):
        if spec["image"] not in ctx.views:
            raise ValueError(f"unknown image {spec['image']}")
        if _point2(spec["p1"]) == _point2(spec["p2"]):
            raise ValueError("p1 and p2 coincide — click two distinct endpoints")
    if spec_a["image"] == spec_b["image"]:
        raise ValueError("pick two DIFFERENT photos that both show the reference")

    obs, views, clicked = [], [], []
    for spec in (spec_a, spec_b):
        px = np.array([_point2(spec["p1"]), _point2(spec["p2"])], np.float64)
        obs.append((spec["image"], px))
        views.append(ctx.views[spec["image"]])
        clicked.append(px)
    pts = _triangulate_pixels(obs, ctx.views)         # (2,3)
    if not np.isfinite(pts).all():
        raise RuntimeError("reference triangulation produced nonfinite endpoints")
    model_len = float(np.linalg.norm(pts[0] - pts[1]))
    if not np.isfinite(model_len) or model_len < 1e-9:
        raise RuntimeError("triangulation of the reference segment failed — "
                           "pick two photos with a clearly different viewpoint")

    # --- quality gate -----------------------------------------------------
    reproj = []
    for v, px in zip(views, clicked):
        uv, depth = v.project(pts)
        # A point behind a camera can still have zero reprojection residual.
        if not np.isfinite(depth).all() or np.any(depth <= 0):
            raise RuntimeError("reference endpoints must be in front of both "
                               "cameras with finite positive depth - re-click "
                               "the SAME two physical points in both photos")
        if not np.isfinite(uv).all():
            raise RuntimeError("reference reprojection produced nonfinite coordinates")
        reproj.append(np.linalg.norm(uv - px, axis=1))
    reproj = np.concatenate(reproj)
    reproj_mean, reproj_max = float(reproj.mean()), float(reproj.max())
    if not np.isfinite([reproj_mean, reproj_max]).all():
        raise RuntimeError("reference reprojection error must be finite")

    angle_deg = float("inf")
    ca, cb = views[0].center, views[1].center
    for X in pts:
        ra, rb = ca - X, cb - X
        cosang = float(np.clip(ra @ rb /
                               (np.linalg.norm(ra) * np.linalg.norm(rb)), -1, 1))
        if not np.isfinite(cosang):
            raise RuntimeError("reference viewing-ray angle must be finite")
        angle_deg = min(angle_deg, float(np.degrees(np.arccos(cosang))))

    if reproj_mean > 15.0 or reproj_max > 40.0:
        raise RuntimeError(
            f"the clicked endpoints don't match between the two photos "
            f"(reprojection error {reproj_mean:.0f} px mean, {reproj_max:.0f} px "
            "max) — re-click the SAME two physical points in both photos")
    if angle_deg < 0.3:
        raise RuntimeError(
            f"the two photos view the reference from nearly the same direction "
            f"(ray angle {angle_deg:.1f}°) — pick two photos taken from "
            "clearly different positions")

    px_len = float(np.mean([np.linalg.norm(px[0] - px[1]) for px in clicked]))
    if not np.isfinite(px_len):
        raise RuntimeError("reference pixel length must be finite")
    rel_err = _clip_rel(reproj_mean / max(px_len, 1e-6))

    warnings: list[str] = []
    if reproj_mean > 8.0:
        warnings.append(f"clicks are sloppy (reprojection {reproj_mean:.0f} px) — "
                        "zoom in and click the exact endpoints")
    if angle_deg < 1.5:
        warnings.append(f"viewing angle between the photos is small "
                        f"({angle_deg:.1f}°) — the scale is poorly conditioned")
    if px_len < 50.0:
        warnings.append(f"the reference spans only {px_len:.0f} px — use a "
                        "longer reference or a closer photo for accurate scale")

    scale = float(length_m / model_len)
    if not np.isfinite(scale) or scale <= 0:
        raise RuntimeError("reference scale must be finite and positive")
    ctx.scale = scale
    ctx.scale_info = {
        "applied": True, "method": "manual", "length_m": float(length_m),
        "model_len": model_len, "images": [spec_a["image"], spec_b["image"]],
        "scale": scale, "reproj_px_mean": reproj_mean,
        "reproj_px_max": reproj_max, "angle_deg": angle_deg,
        "scale_rel_error": rel_err, "warnings": warnings,
    }
    log(f"[scale] manual segment {model_len:.4f} model units = {length_m} m "
        f"-> scale={scale:.6g} (reproj {reproj_mean:.1f} px, ray angle "
        f"{angle_deg:.1f}°, ±{rel_err * 100:.1f}%)")
    for w in warnings:
        log(f"[scale] warning: {w}")
    return ctx.scale_info

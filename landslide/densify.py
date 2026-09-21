"""Semi-dense point cloud by rectified SGBM stereo on well-connected pairs.

COLMAP's CUDA dense pipeline is unavailable (CPU wheel), so we densify the
sparse cloud ourselves: pick stereo pairs from the covisibility graph with a
sane baseline, rectify with the known SfM poses, run SGBM, and lift the
disparity map to world coordinates. Points from all pairs are fused, voxel
downsampled and outlier-filtered.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .geometry import undistort_normalized
from .sfm import ImageView, Log, ReconCtx, covisibility_pairs


@dataclass
class StereoConfig:
    """One place for the stereo-pair geometry gates and the SGBM knobs.

    Pair-selection thresholds: too small a baseline/convergence/ray-angle
    gives noisy, ill-conditioned depth; too large breaks rectification (the
    fronto-parallel block-matching assumption foreshortens badly) or forces
    a large rectification shear.
    """
    min_covis: int = 25
    per_image: int = 2
    max_pairs: int = 30
    rescue_per_image: int = 3
    baseline_ratio: tuple[float, float] = (0.10, 1.5)
    convergence_deg: tuple[float, float] = (4.0, 35.0)
    ray_angle_deg: tuple[float, float] = (3.0, 30.0)
    max_rect_shear_deg: float = 40.0
    fusion_k: int = 4
    block_size: int = 5
    p1: int = 8 * 25
    p2: int = 32 * 25 * 4
    uniqueness_ratio: int = 10
    speckle_window: int = 300
    speckle_range: int = 3
    prefilter_cap: int = 63
    disp12_max_diff: int = 1


def _load_scaled(view: ImageView, max_width: int):
    img = cv2.imread(str(view.path), cv2.IMREAD_COLOR)
    if img is None:
        raise IOError(f"cannot read {view.path}")
    s = min(1.0, max_width / max(img.shape[:2]))
    if s < 1.0:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    K = view.K.copy()
    K[0, :] *= s
    K[1, :] *= s
    return img, K


def _view_depth_stats(view: ImageView, sparse: np.ndarray):
    _, depth = view.project(sparse)
    depth = depth[depth > 0]
    if len(depth) < 20:
        return None
    return float(np.median(depth))


def _forward_dir(v: ImageView) -> np.ndarray:
    """Unit world-space direction the camera's optical axis points along."""
    f = v.R[2, :]
    return f / np.linalg.norm(f)


def _rectification_shear_deg(va: ImageView, vb: ImageView) -> float:
    """Rotation angle cv2.stereoRectify applies to align this pair's epipoles.

    R1 depends only on the relative pose and intrinsics, not image size, so
    this is checked before any pixel data is touched.
    """
    R_rel = vb.R @ va.R.T
    t_rel = (vb.t - R_rel @ va.t).reshape(3, 1)
    try:
        R1, _, _, _, _, _, _ = cv2.stereoRectify(
            va.K, va.dist, vb.K, vb.dist, (va.width, va.height), R_rel, t_rel,
            flags=cv2.CALIB_ZERO_DISPARITY, alpha=0.0)
    except cv2.error:
        return 180.0
    cos_th = float(np.clip((np.trace(R1) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cos_th)))


def _pair_geometry_ok(va: ImageView, vb: ImageView, base: float, med: float,
                      scene_centroid: np.ndarray, cfg: "StereoConfig",
                      check_angles: bool) -> bool:
    lo, hi = cfg.baseline_ratio
    if not (lo * med <= base <= hi * med):
        return False
    if not check_angles:
        return True
    fa, fb = _forward_dir(va), _forward_dir(vb)
    conv = float(np.degrees(np.arccos(np.clip(fa @ fb, -1.0, 1.0))))
    lo_c, hi_c = cfg.convergence_deg
    if not (lo_c <= conv <= hi_c):
        return False
    ra, rb = scene_centroid - va.center, scene_centroid - vb.center
    cosang = float(np.clip(ra @ rb / (np.linalg.norm(ra) * np.linalg.norm(rb) + 1e-12),
                          -1.0, 1.0))
    ray = float(np.degrees(np.arccos(cosang)))
    lo_r, hi_r = cfg.ray_angle_deg
    if not (lo_r <= ray <= hi_r):
        return False
    return _rectification_shear_deg(va, vb) <= cfg.max_rect_shear_deg


def select_pairs(ctx: ReconCtx, cfg: "StereoConfig | None" = None,
                 log: Log = print) -> list[tuple[ImageView, ImageView, int]]:
    """Greedy selection of covisible image pairs with usable stereo geometry.

    Beyond the baseline/depth ratio, pairs must have a workable convergence
    angle between optical axes, a workable ray angle at the (approximate)
    scene point they both look at, and a rectification that doesn't need to
    shear either image too far. Images left with zero pairs after these
    geometry gates get a relaxed rescue pass (baseline/depth ratio only,
    `rescue_per_image` cap) so a strongly-convergent small set still gets
    dense coverage everywhere instead of holes at the tightest viewpoints.
    """
    cfg = cfg or StereoConfig()
    scene_centroid = np.asarray(ctx.sparse, np.float64).mean(axis=0)
    by_id = {v.image_id: v for v in ctx.views.values()}
    cnt = covisibility_pairs(ctx.rec)
    depth_med = {}
    for v in ctx.views.values():
        st = _view_depth_stats(v, ctx.sparse)
        if st is not None:
            depth_med[v.image_id] = st

    candidates = []
    for (a, b), c in cnt.items():
        if c < cfg.min_covis or a not in by_id or b not in by_id:
            continue
        va, vb = by_id[a], by_id[b]
        base = float(np.linalg.norm(va.center - vb.center))
        med = depth_med.get(a, depth_med.get(b))
        if med is None:
            continue
        candidates.append((va, vb, c, base, med))
    candidates.sort(key=lambda p: -p[2])

    def _greedy(pool, per_image, geometry_gate, per_img):
        chosen = []
        for va, vb, c, base, med in pool:
            if len(chosen) >= cfg.max_pairs:
                break
            if per_img.get(va.image_id, 0) >= per_image or \
               per_img.get(vb.image_id, 0) >= per_image:
                continue
            if not geometry_gate(va, vb, base, med):
                continue
            chosen.append((va, vb, c))
            per_img[va.image_id] = per_img.get(va.image_id, 0) + 1
            per_img[vb.image_id] = per_img.get(vb.image_id, 0) + 1
        return chosen

    per_img: dict[int, int] = {}
    chosen = _greedy(candidates, cfg.per_image,
                     lambda va, vb, base, med: _pair_geometry_ok(
                         va, vb, base, med, scene_centroid, cfg, True),
                     per_img)

    covered = {i for pair in chosen for i in (pair[0].image_id, pair[1].image_id)}
    starved = {v.image_id for v in ctx.views.values() if v.image_id not in covered}
    if starved and len(chosen) < cfg.max_pairs:
        rescue_pool = [p for p in candidates
                      if p[0].image_id in starved or p[1].image_id in starved]
        rescued = _greedy(rescue_pool, cfg.rescue_per_image,
                          lambda va, vb, base, med: _pair_geometry_ok(
                              va, vb, base, med, scene_centroid, cfg, False),
                          per_img)
        if rescued:
            log(f"[dense] geometry gate left {len(starved)} image(s) with no pair; "
                f"rescue pass (baseline/depth only) added {len(rescued)} pair(s)")
        chosen += rescued
    return chosen


def _covis_neighbors(ref: ImageView, by_id: dict, cnt, depth_med: dict,
                     scene_centroid: np.ndarray, cfg: "StereoConfig",
                     check_angles: bool) -> list[tuple[int, ImageView]]:
    """Covisible neighbours of `ref` passing the geometry gate, most
    covisible first — the per-reference-image analogue of `select_pairs`'
    candidate list (T1.4 reuses the T1.3 gate, not the greedy global pairing)."""
    cands = []
    for (a, b), c in cnt.items():
        if a == ref.image_id:
            other = b
        elif b == ref.image_id:
            other = a
        else:
            continue
        if c < cfg.min_covis or other not in by_id:
            continue
        nb = by_id[other]
        base = float(np.linalg.norm(ref.center - nb.center))
        med = depth_med.get(ref.image_id, depth_med.get(other))
        if med is None:
            continue
        if _pair_geometry_ok(ref, nb, base, med, scene_centroid, cfg, check_angles):
            cands.append((c, nb))
    cands.sort(key=lambda x: -x[0])
    return cands


def _neighbors_for_view(ref: ImageView, by_id: dict, cnt, depth_med: dict,
                        scene_centroid: np.ndarray, cfg: "StereoConfig") -> list[ImageView]:
    """Up to `cfg.fusion_k` neighbours to fuse depth for `ref` from."""
    cands = _covis_neighbors(ref, by_id, cnt, depth_med, scene_centroid, cfg, True)
    if not cands:
        cands = _covis_neighbors(ref, by_id, cnt, depth_med, scene_centroid, cfg, False)
    return [nb for _, nb in cands[:cfg.fusion_k]]


def _fuse_depth_candidates(Z: np.ndarray, STEP: np.ndarray):
    """Per-pixel consensus over K candidate depth maps (K, H, W each).

    Two candidates "agree" within max(1% of their depth, 2x the one-
    disparity depth step) — a coarser (farther, or short-baseline) estimate
    needs a wider band to call two readings "the same point". Each pixel is
    assigned to its largest mutually-agreeing cluster; the value is the mean
    of that cluster.

    Returns (fused_z (H, W), best_which (H, W) int, best_count (H, W) int).
    `best_which` indexes which candidate's colour to keep (an arbitrary but
    deterministic member of the winning cluster); `best_count` is the
    per-pixel cross-neighbour agreement count the caller thresholds on.
    """
    K = Z.shape[0]
    valid = np.isfinite(Z)
    agree_count = np.zeros(Z.shape, np.int16)
    sum_z = np.zeros(Z.shape, np.float64)
    for a in range(K):
        for b in range(K):
            tol = np.maximum(0.01 * 0.5 * (np.abs(Z[a]) + np.abs(Z[b])),
                             2.0 * 0.5 * (STEP[a] + STEP[b]))
            agree = valid[a] & valid[b] & (np.abs(Z[a] - Z[b]) <= tol)
            agree_count[a] += agree
            sum_z[a] += np.where(agree, Z[b], 0.0)
    best_which = np.argmax(agree_count, axis=0)
    best_count = np.take_along_axis(agree_count, best_which[None], axis=0)[0]
    best_sum = np.take_along_axis(sum_z, best_which[None], axis=0)[0]
    fused_z = best_sum / np.maximum(best_count, 1)
    return fused_z, best_which, best_count


def _depth_map_for_view(ref: ImageView, neighbors: list[ImageView],
                        sparse: np.ndarray, stereo_width: int,
                        cfg: "StereoConfig", log: Log = print):
    """`ref`'s own depth map, fused from independent stereo estimates against
    each of `neighbors` (T1.4 — the main dense-cloud accuracy lever).

    Each neighbour's rectified-SGBM result is lifted to world points (via
    the existing `stereo_pair`) and re-projected through `ref`'s OWN
    distortion model onto `ref`'s native pixel grid — equivalent to, and
    simpler than, un-rectifying the disparity map by inverting the
    rectification remap, and it reuses `ImageView.project` exactly as every
    other occlusion/selection test in this codebase does. A pixel keeps a
    depth only when its largest cross-neighbour agreement cluster has >= 2
    members (>= 1 when `ref` has only one usable neighbour at all — no
    second opinion is possible, but a single stereo estimate beats none).

    Returns (points_world (M, 3), colors (M, 3) uint8) — a drop-in
    replacement for one `stereo_pair` call's output at the dense_cloud
    call site, fused across all of `ref`'s neighbours instead of one pair.
    """
    s = min(1.0, stereo_width / max(ref.width, ref.height))
    gw, gh = max(1, round(ref.width * s)), max(1, round(ref.height * s))
    K = len(neighbors)
    if K == 0:
        return np.zeros((0, 3)), np.zeros((0, 3))
    Z = np.full((K, gh, gw), np.nan, np.float32)
    STEP = np.full((K, gh, gw), np.nan, np.float32)
    COL = np.zeros((K, gh, gw, 3), np.uint8)
    fx_ref = float(ref.K[0, 0])

    for k, nb in enumerate(neighbors):
        pts_world, cols = stereo_pair(ref, nb, sparse, max_width=stereo_width, cfg=cfg)
        if len(pts_world) == 0:
            continue
        uv, depth = ref.project(pts_world)
        ix = np.round(uv[:, 0] * s).astype(np.int64)
        iy = np.round(uv[:, 1] * s).astype(np.int64)
        ok = ((depth > 0) & np.isfinite(depth) & (ix >= 0) & (ix < gw) &
              (iy >= 0) & (iy < gh))
        if not ok.any():
            continue
        ix, iy, depth, cols = ix[ok], iy[ok], depth[ok], cols[ok]
        baseline = float(np.linalg.norm(ref.center - nb.center))
        step = depth ** 2 / max(fx_ref * baseline, 1e-9)
        # first occurrence per cell: this pair's own output is already
        # spatially coherent, so a second hit in one cell is rare noise
        _, order = np.unique(iy * gw + ix, return_index=True)
        Z[k][iy[order], ix[order]] = depth[order]
        STEP[k][iy[order], ix[order]] = step[order]
        COL[k][iy[order], ix[order]] = cols[order]

    if not np.isfinite(Z).any():
        return np.zeros((0, 3)), np.zeros((0, 3))

    fused_z, best_which, best_count = _fuse_depth_candidates(Z, STEP)
    min_agree = 1 if K == 1 else 2
    keep = np.isfinite(fused_z) & (best_count >= min_agree)
    if not keep.any():
        return np.zeros((0, 3)), np.zeros((0, 3))

    iy, ix = np.nonzero(keep)
    z = fused_z[iy, ix].astype(np.float64)
    col = COL[best_which[iy, ix], iy, ix]
    uv_native = np.column_stack([ix, iy]).astype(np.float64) / s

    uvn = undistort_normalized(uv_native, ref.K, ref.dist)
    d_cam = np.column_stack([uvn, np.ones(len(uvn))])
    Xc = d_cam * z[:, None]                       # camera-frame 3D points
    pts_world = (Xc - ref.t) @ ref.R
    return pts_world, col


def stereo_pair(va: ImageView, vb: ImageView, sparse: np.ndarray,
                max_width: int = 1280, cfg: "StereoConfig | None" = None,
                _swapped: bool = False):
    """One rectified SGBM stereo reconstruction; returns world points+colors.

    max_width sets the working resolution. 1280 (default) is the accuracy
    choice; on the synthetic bowl benchmark 640px runs ~5x faster but the
    measured volume error grows from ~18% to ~33% — half-res disparity
    smears steep walls. Only drop to 640 for quick previews, not finals.
    """
    cfg = cfg or StereoConfig()
    img_a, Ka = _load_scaled(va, max_width)
    img_b, Kb = _load_scaled(vb, max_width)
    h = min(img_a.shape[0], img_b.shape[0])
    w = min(img_a.shape[1], img_b.shape[1])
    img_a, img_b = img_a[:h, :w], img_b[:h, :w]
    Ka, Kb = Ka.copy(), Kb.copy()

    R_rel = vb.R @ va.R.T
    t_rel = (vb.t - R_rel @ va.t).reshape(3, 1)   # OpenCV 5 wants a column
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        Ka, va.dist, Kb, vb.dist, (w, h), R_rel, t_rel,
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=0.0)

    fx = P1[0, 0]
    baseline = -P2[0, 3] / fx
    if baseline <= 0 and not _swapped:
        # cameras are order-swapped for stereo; retry with a/b exchanged
        return stereo_pair(vb, va, sparse, max_width, cfg, _swapped=True)
    baseline = abs(baseline)

    m1a, m1b = cv2.initUndistortRectifyMap(Ka, va.dist, R1, P1[:3, :3],
                                           (w, h), cv2.CV_32FC1)
    m2a, m2b = cv2.initUndistortRectifyMap(Kb, vb.dist, R2, P2[:3, :3],
                                           (w, h), cv2.CV_32FC1)
    ra = cv2.remap(cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY), m1a, m1b,
                   cv2.INTER_LINEAR)
    rb = cv2.remap(cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY), m2a, m2b,
                   cv2.INTER_LINEAR)
    col_r = cv2.remap(img_a, m1a, m1b, cv2.INTER_LINEAR)

    # disparity search range from the sparse depth range seen by view a
    _, depth_a = va.project(sparse)
    depth_a = depth_a[depth_a > 0]
    if len(depth_a) < 20:
        return np.zeros((0, 3)), np.zeros((0, 3))
    zmin, zmax = np.percentile(depth_a, 1), np.percentile(depth_a, 99)
    if zmin <= 0 or baseline == 0:
        return np.zeros((0, 3)), np.zeros((0, 3))
    d_far, d_near = fx * baseline / zmax, fx * baseline / zmin
    min_disp = int(max(0, np.floor(d_far) - 8))
    span = max(d_near - min_disp, 16)
    num_disp = int(np.clip((int(np.ceil(span / 16)) + 1) * 16, 16, 320))

    sgbm = cv2.StereoSGBM_create(
        minDisparity=min_disp, numDisparities=num_disp, blockSize=cfg.block_size,
        P1=cfg.p1, P2=cfg.p2,
        disp12MaxDiff=cfg.disp12_max_diff, uniquenessRatio=cfg.uniqueness_ratio,
        speckleWindowSize=cfg.speckle_window, speckleRange=cfg.speckle_range,
        preFilterCap=cfg.prefilter_cap,
    )
    try:
        sgbm.setMode(cv2.STEREO_SGBM_MODE_HH4)
    except Exception:
        pass
    disp = sgbm.compute(ra, rb).astype(np.float32) / 16.0

    # left/right consistency: kill matches that don't survive a reverse match
    sgbm_r = cv2.StereoSGBM_create(
        minDisparity=-(min_disp + num_disp), numDisparities=num_disp,
        blockSize=cfg.block_size, P1=cfg.p1, P2=cfg.p2,
        disp12MaxDiff=cfg.disp12_max_diff, uniquenessRatio=cfg.uniqueness_ratio,
        speckleWindowSize=cfg.speckle_window, speckleRange=cfg.speckle_range,
        preFilterCap=cfg.prefilter_cap,
    )
    try:
        sgbm_r.setMode(cv2.STEREO_SGBM_MODE_HH4)
    except Exception:
        pass
    disp_r = sgbm_r.compute(rb, ra).astype(np.float32) / 16.0
    rows, cols = np.nonzero(disp > min_disp + 1.0)
    d_l = disp[rows, cols]
    cols_r = np.clip((cols - d_l.round()).astype(np.int64), 0, w - 1)
    consistent = np.abs(disp_r[rows, cols_r] + d_l) <= 1.5
    mask = np.zeros(disp.shape, dtype=bool)
    mask[rows[consistent], cols[consistent]] = True
    if not mask.any():
        return np.zeros((0, 3)), np.zeros((0, 3))
    pts_rect = cv2.reprojectImageTo3D(disp, Q, handleMissingValues=False)
    pts = pts_rect[mask]
    cols = col_r[mask]

    # rectified-cam1 frame -> cam-a original -> world
    # x_rect = R1 @ x_cam  =>  x_cam = x_rect @ R1        (row vectors)
    # x_cam = R @ x_w + t  =>  x_w = (x_cam - t) @ R
    pts_cam_a = pts @ R1
    pts_world = (pts_cam_a - va.t) @ va.R
    return pts_world, cols


def sor_mask(points: np.ndarray, k: int = 10, sigma: float = 2.0,
             iters: int = 2) -> np.ndarray:
    """Statistical outlier removal: boolean keep-mask."""
    keep = np.ones(len(points), dtype=bool)
    for _ in range(iters):
        idx = np.flatnonzero(keep)
        if len(idx) <= k + 1:
            break
        sub = np.ascontiguousarray(points[idx])
        d, _ = cKDTree(sub).query(sub, k=k + 1, workers=-1)
        mean_d = d[:, 1:].mean(axis=1)
        thr = mean_d.mean() + sigma * mean_d.std()
        keep[idx[mean_d > thr]] = False
    return keep


def voxel_downsample(points: np.ndarray, colors: np.ndarray, voxel: float):
    keys = np.floor(points / voxel).astype(np.int64)
    _, inv, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    n = len(counts)
    summed = np.zeros((n, 3), np.float64)
    np.add.at(summed, inv, points.astype(np.float64))
    centers = summed / counts[:, None]
    col_sum = np.zeros((n, 3), np.float64)
    np.add.at(col_sum, inv, colors.astype(np.float64))
    return centers, np.clip(np.rint(col_sum / counts[:, None]), 0, 255).astype(np.uint8)


def _camera_plane_up(views: dict, sparse: np.ndarray) -> tuple[np.ndarray, float]:
    """"Up" from the plane fitted through the camera centers, plus how
    collinear the cameras are (2nd/1st singular value ratio of their spread:
    near 0 means the path is a straight line and the in-plane axis picked by
    SVD is arbitrary).

    Cameras sit roughly on a horizontal arc above the scene, so the plane's
    normal is close to vertical. Sign: up points from the scene (sparse cloud
    centroid) toward the cameras. The mean-viewing-direction trick fails for
    an arc of cameras because horizontal components don't cancel.
    """
    centers = np.array([v.center for v in views.values()])
    c = centers.mean(axis=0)
    _, S, Vt = np.linalg.svd(centers - c, full_matrices=False)
    up = Vt[2]
    scene = np.asarray(sparse).mean(axis=0)
    if up @ (c - scene) < 0:
        up = -up
    collinearity = float(S[1] / S[0]) if S[0] > 0 else 0.0
    return up / np.linalg.norm(up), collinearity


def _scene_plane_up(sparse: np.ndarray, cam_centroid: np.ndarray) -> np.ndarray | None:
    """Ground-normal candidate from the dominant plane of the SCENE itself
    (the sparse cloud), oriented so cameras sit above it. None when the cloud
    is too small or has no clear dominant plane (fit_plane_ransac degenerate).
    """
    from .volume import fit_plane, fit_plane_ransac

    sparse = np.asarray(sparse, np.float64)
    if len(sparse) < 30:
        return None
    inliers = fit_plane_ransac(sparse)
    if inliers is None or int(inliers.sum()) < 30:
        return None
    c, n, _ = fit_plane(sparse[inliers])
    if n @ (cam_centroid - sparse.mean(axis=0)) < 0:
        n = -n
    return n / np.linalg.norm(n)


def estimate_up(views: dict, sparse: np.ndarray, log: Log | None = None) -> np.ndarray:
    """Scene-vertical "up" vector, robust to the shape of the camera path.

    Two candidates: the normal of the dominant plane of the SPARSE CLOUD
    (the ground itself, via the same RANSAC consensus the volume datum
    uses) and the normal of the plane through the CAMERA centers (tied to
    the photographer's path, not the scene — degenerate for a straight or
    descending walk, and can pick the wrong sign for a road-above-a-pit
    shot). The scene plane is trusted whenever the cameras are collinear or
    the two candidates roughly agree; on genuine disagreement, whichever
    normal more of the cloud's own local surfaces call "ground-like" wins.
    """
    centers = np.array([v.center for v in views.values()])
    cam_up, collinearity = _camera_plane_up(views, sparse)
    scene_up = _scene_plane_up(sparse, centers.mean(axis=0))
    if scene_up is None:
        return cam_up
    if collinearity < 0.10:
        if log:
            log("[up] collinear camera path — using the scene's ground plane")
        return scene_up
    agree_deg = float(np.degrees(np.arccos(np.clip(abs(scene_up @ cam_up), -1, 1))))
    if agree_deg <= 20.0:
        return scene_up
    n_scene = int(surface_filter(sparse, scene_up).sum())
    n_cam = int(surface_filter(sparse, cam_up).sum())
    if log:
        log(f"[up] scene-plane vs camera-plane vertical disagree by "
            f"{agree_deg:.0f}° — ground-like vote: scene {n_scene}, "
            f"camera-path {n_cam}")
    return scene_up if n_scene >= n_cam else cam_up


def surface_filter(points: np.ndarray, up: np.ndarray, k: int = 16,
                   min_cos: float = 0.25, chunk: int = 300_000) -> np.ndarray:
    """Keep points whose local surface normal is within ~75° of vertical.

    Drops near-vertical structures (marker boards, walls, tree trunks) that
    would otherwise pollute the ground-volume integral. The k-NN query runs
    in chunks: the neighbor/index temporaries cost ~200+ bytes per point, a
    multi-GB spike on a multi-million-point cloud in one shot; chunking
    bounds peak RAM without changing the result.
    """
    if len(points) < k + 1:
        return np.ones(len(points), dtype=bool)
    pts = points.astype(np.float32)
    tree = cKDTree(pts)
    upv = up.astype(np.float32)
    keep = np.empty(len(pts), dtype=bool)
    for s in range(0, len(pts), chunk):
        sub = pts[s:s + chunk]
        _, idx = tree.query(sub, k=k, workers=-1)
        nb = pts[idx]                                # (n, k, 3)
        nb -= nb.mean(axis=1, keepdims=True)
        cov = np.einsum("nki,nkj->nij", nb, nb) / k
        _, vecs = np.linalg.eigh(cov)                # ascending eigenvalues
        normals = vecs[:, :, 0]                      # smallest = surface normal
        cos = np.abs(normals @ upv)
        keep[s:s + chunk] = cos > min_cos
    return keep


# Hard ceiling on the fused cloud. Downstream stages (SOR, normal filter,
# ortho render, prism integration, every server ctx holding this cloud) scale
# with point count; 2.5M points is ~2 cm spacing on a 30 m scene — far finer
# than photogrammetric volume accuracy needs, and keeps peak RAM ~1 GB.
MAX_FUSED_POINTS = 2_500_000


def _cap_voxel(n_points: int, voxel: float, max_points: int = MAX_FUSED_POINTS):
    """Voxel size that brings a surface-like cloud under the point cap.

    A ground cloud is a 2-D manifold, so point count scales as voxel^-2:
    growing the voxel by sqrt(N/Nmax) lands just under the cap.
    """
    if n_points <= max_points:
        return voxel
    return voxel * float(np.sqrt(n_points / max_points))


def dense_cloud(ctx: ReconCtx, log: Log = print, max_pairs: int = 30,
                force: bool = False, stereo_width: int = 1280,
                cfg: "StereoConfig | None" = None) -> dict:
    """Build (or load cached) semi-dense cloud; stored on ctx.dense.

    stereo_width sets the SGBM working resolution (see stereo_pair): 1280
    for finals, 640 for ~5x-faster previews at reduced accuracy.
    """
    cfg = cfg or StereoConfig(max_pairs=max_pairs)
    cache = ctx.workdir / f"dense_{stereo_width}_{ctx.fingerprint}.npz"
    if ctx.dense is not None and not force:
        return ctx.dense
    if cache.exists() and not force:
        z = np.load(cache)
        if "fingerprint" in z and str(z["fingerprint"]) == ctx.fingerprint:
            ctx.dense = {"points": z["points"], "colors": z["colors"]}
            log(f"[dense] loaded cache: {len(ctx.dense['points'])} points")
            return ctx.dense
        log("[dense] cached cloud is from a different reconstruction, rebuilding")

    views_sorted = sorted(ctx.views.values(), key=lambda v: v.name)
    if not views_sorted:
        log("[dense] no registered views; keeping sparse cloud only")
        ctx.dense = {"points": np.zeros((0, 3)), "colors": np.zeros((0, 3))}
        return ctx.dense

    by_id = {v.image_id: v for v in ctx.views.values()}
    cnt = covisibility_pairs(ctx.rec)
    scene_centroid = np.asarray(ctx.sparse, np.float64).mean(axis=0)
    depth_med = {}
    for v in ctx.views.values():
        st = _view_depth_stats(v, ctx.sparse)
        if st is not None:
            depth_med[v.image_id] = st

    # voxel size is fixed up front from the sparse extent (the dense cloud is
    # support-clipped to the sparse one, so their extents track closely) —
    # this lets each reference image's fused points be downsampled the
    # moment they're produced instead of concatenating raw points first
    extent = float(np.ptp(ctx.sparse, axis=0).max())
    voxel = max(extent / 900.0, 1e-6)

    # T1.4: for each registered image, fuse depth against up to
    # cfg.fusion_k geometry-gated neighbours (cross-neighbour consensus,
    # not a naive per-pair union) instead of the old fixed global pair list.
    log(f"[dense] multi-view depth fusion: {len(views_sorted)} reference "
        f"images, up to {cfg.fusion_k} neighbours each, at {stereo_width}px")
    all_pts, all_cols = [], []
    n_ref_with_depth = 0
    for i, ref in enumerate(views_sorted):
        neighbors = _neighbors_for_view(ref, by_id, cnt, depth_med,
                                        scene_centroid, cfg)
        if not neighbors:
            log(f"[dense] view {i + 1}/{len(views_sorted)} ({ref.name}): "
                "no usable neighbour")
            continue
        pts, cols = _depth_map_for_view(ref, neighbors, ctx.sparse,
                                        stereo_width, cfg, log=log)
        if len(pts):
            n_ref_with_depth += 1
            pts, cols = voxel_downsample(pts, cols, voxel)
            all_pts.append(pts)
            all_cols.append(cols)
        log(f"[dense] view {i + 1}/{len(views_sorted)} ({ref.name}, "
            f"{len(neighbors)} neighbours): {len(pts)} pts")
    if not all_pts:
        log("[dense] multi-view fusion produced nothing; keeping sparse cloud only")
        ctx.dense = {"points": np.zeros((0, 3)), "colors": np.zeros((0, 3))}
        return ctx.dense
    log(f"[dense] {n_ref_with_depth}/{len(views_sorted)} reference images "
        "contributed fused depth")

    pts = np.concatenate(all_pts)
    cols = np.concatenate(all_cols)
    pts, cols = voxel_downsample(pts, cols, voxel)   # merge per-pair voxels

    keep = sor_mask(pts)
    pts, cols = pts[keep], cols[keep]

    # clip to the sparse cloud's support: SfM saw the whole scene, so dense
    # points far from any sparse point are stereo junk at bogus depths. The
    # radius must respect the sparse cloud's own point spacing (it is much
    # coarser than the dense one).
    d_self, _ = cKDTree(ctx.sparse).query(ctx.sparse, k=2, workers=-1)
    sparse_spacing = float(np.median(d_self[:, 1]))
    sup_radius = max(5.0 * voxel, 2.0 * sparse_spacing, 0.02 * extent)
    d_sup, _ = cKDTree(ctx.sparse).query(pts, k=1, workers=-1)
    sup = d_sup <= sup_radius
    log(f"[dense] support filter (r={sup_radius:.3g}): {int(sup.sum())}/{len(pts)} "
        f"within sparse scene support")
    pts, cols = pts[sup], cols[sup]

    # bounded cloud: cap before the memory-hungry stages so every later step
    # (normal filter, ortho, prism integration, server-held ctx) has a hard
    # upper bound on working-set size
    voxel = _cap_voxel(len(pts), voxel)
    if voxel > max(extent / 900.0, 1e-6):
        pts, cols = voxel_downsample(pts, cols, voxel)
        log(f"[dense] capped cloud to {len(pts)} points (voxel {voxel:.4g})")

    up = estimate_up(ctx.views, ctx.sparse, log=log)
    ground = surface_filter(pts, up)
    log(f"[dense] surface-normal filter: keeping {int(ground.sum())}/{len(pts)} "
        f"(dropped {int((~ground).sum())} near-vertical/non-ground points, "
        f"up={np.round(up, 2).tolist()})")
    pts, cols = pts[ground], cols[ground]
    # float32 halves RAM and cache size; at ~meters-per-model-unit scale the
    # ~1e-7 relative error is microns — invisible to volume integration
    pts = pts.astype(np.float32)
    ctx.dense = {"points": pts, "colors": cols}
    np.savez_compressed(cache, points=pts, colors=cols, fingerprint=ctx.fingerprint)
    log(f"[dense] fused cloud: {len(pts)} points "
        f"(voxel {voxel:.4g}, scene extent {extent:.3g})")
    return ctx.dense

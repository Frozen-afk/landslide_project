"""Semi-dense point cloud by rectified SGBM stereo on well-connected pairs.

COLMAP's CUDA dense pipeline is unavailable (CPU wheel), so we densify the
sparse cloud ourselves: pick stereo pairs from the covisibility graph with a
sane baseline, rectify with the known SfM poses, run SGBM, and lift the
disparity map to world coordinates. Points from all pairs are fused, voxel
downsampled and outlier-filtered.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .sfm import ImageView, Log, ReconCtx, covisibility_pairs


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


def select_pairs(ctx: ReconCtx, min_covis: int = 25, per_image: int = 2,
                 max_pairs: int = 30) -> list[tuple[ImageView, ImageView, int]]:
    """Greedy selection of covisible image pairs with a usable baseline."""
    by_id = {v.image_id: v for v in ctx.views.values()}
    cnt = covisibility_pairs(ctx.rec)
    depth_med = {}
    for v in ctx.views.values():
        st = _view_depth_stats(v, ctx.sparse)
        if st is not None:
            depth_med[v.image_id] = st

    pairs = []
    for (a, b), c in cnt.items():
        if c < min_covis or a not in by_id or b not in by_id:
            continue
        va, vb = by_id[a], by_id[b]
        base = float(np.linalg.norm(va.center - vb.center))
        med = depth_med.get(a, depth_med.get(b))
        if med is None:
            continue
        # too small a baseline -> noisy depths; too large -> rectification breaks
        if not (0.10 * med <= base <= 1.5 * med):
            continue
        pairs.append((va, vb, c))

    pairs.sort(key=lambda p: -p[2])
    chosen, per_img = [], {}
    for va, vb, c in pairs:
        if len(chosen) >= max_pairs:
            break
        if per_img.get(va.image_id, 0) >= per_image or \
           per_img.get(vb.image_id, 0) >= per_image:
            continue
        chosen.append((va, vb, c))
        per_img[va.image_id] = per_img.get(va.image_id, 0) + 1
        per_img[vb.image_id] = per_img.get(vb.image_id, 0) + 1
    return chosen


def stereo_pair(va: ImageView, vb: ImageView, sparse: np.ndarray,
                max_width: int = 1280, _swapped: bool = False):
    """One rectified SGBM stereo reconstruction; returns world points+colors."""
    img_a, Ka = _load_scaled(va, max_width)
    img_b, Kb = _load_scaled(vb, max_width)
    h = min(img_a.shape[0], img_b.shape[0])
    w = min(img_a.shape[1], img_b.shape[1])
    img_a, img_b = img_a[:h, :w], img_b[:h, :w]
    Ka, Kb = Ka.copy(), Kb.copy()
    Ka[0, 0] *= w / (2.0 * Ka[0, 2]); Ka[1, 1] *= h / (2.0 * Ka[1, 2])
    Kb[0, 0] *= w / (2.0 * Kb[0, 2]); Kb[1, 1] *= h / (2.0 * Kb[1, 2])

    R_rel = vb.R @ va.R.T
    t_rel = (vb.t - R_rel @ va.t).reshape(3, 1)   # OpenCV 5 wants a column
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        Ka, va.dist, Kb, vb.dist, (w, h), R_rel, t_rel,
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=0.0)

    fx = P1[0, 0]
    baseline = -P2[0, 3] / fx
    if baseline <= 0 and not _swapped:
        # cameras are order-swapped for stereo; retry with a/b exchanged
        return stereo_pair(vb, va, sparse, max_width, _swapped=True)
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
        minDisparity=min_disp, numDisparities=num_disp, blockSize=5,
        P1=8 * 25, P2=32 * 25 * 4,
        disp12MaxDiff=1, uniquenessRatio=10,
        speckleWindowSize=300, speckleRange=3, preFilterCap=63,
    )
    try:
        sgbm.setMode(cv2.STEREO_SGBM_MODE_HH4)
    except Exception:
        pass
    disp = sgbm.compute(ra, rb).astype(np.float32) / 16.0

    # left/right consistency: kill matches that don't survive a reverse match
    sgbm_r = cv2.StereoSGBM_create(
        minDisparity=-(min_disp + num_disp), numDisparities=num_disp,
        blockSize=5, P1=8 * 25, P2=32 * 25 * 4,
        disp12MaxDiff=1, uniquenessRatio=10,
        speckleWindowSize=300, speckleRange=3, preFilterCap=63,
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


def estimate_up(views: dict, sparse: np.ndarray) -> np.ndarray:
    """"Up" from the plane fitted through the camera centers.

    Cameras sit roughly on a horizontal arc above the scene, so the plane's
    normal is close to vertical. Sign: up points from the scene (sparse cloud
    centroid) toward the cameras. The mean-viewing-direction trick fails for
    an arc of cameras because horizontal components don't cancel.
    """
    centers = np.array([v.center for v in views.values()])
    c = centers.mean(axis=0)
    _, _, Vt = np.linalg.svd(centers - c, full_matrices=False)
    up = Vt[2]
    scene = np.asarray(sparse).mean(axis=0)
    if up @ (c - scene) < 0:
        up = -up
    return up / np.linalg.norm(up)


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
                force: bool = False) -> dict:
    """Build (or load cached) semi-dense cloud; stored on ctx.dense."""
    cache = ctx.workdir / "dense.npz"
    if ctx.dense is not None and not force:
        return ctx.dense
    if cache.exists() and not force:
        z = np.load(cache)
        ctx.dense = {"points": z["points"], "colors": z["colors"]}
        log(f"[dense] loaded cache: {len(ctx.dense['points'])} points")
        return ctx.dense

    pairs = select_pairs(ctx, max_pairs=max_pairs)
    if not pairs:
        log("[dense] no usable stereo pairs; keeping sparse cloud only")
        ctx.dense = {"points": np.zeros((0, 3)), "colors": np.zeros((0, 3))}
        return ctx.dense

    # voxel size is fixed up front from the sparse extent (the dense cloud is
    # support-clipped to the sparse one, so their extents track closely) —
    # this lets each stereo pair be downsampled the moment it is produced
    # instead of concatenating 10-30M raw points before any downsampling
    extent = float(np.ptp(ctx.sparse, axis=0).max())
    voxel = max(extent / 900.0, 1e-6)

    log(f"[dense] running SGBM on {len(pairs)} stereo pairs")
    all_pts, all_cols = [], []
    for i, (va, vb, c) in enumerate(pairs):
        pts, cols = stereo_pair(va, vb, ctx.sparse)
        if len(pts):
            pts, cols = voxel_downsample(pts, cols, voxel)
            all_pts.append(pts)
            all_cols.append(cols)
        log(f"[dense] pair {i + 1}/{len(pairs)} ({va.name} + {vb.name}): "
            f"{len(pts)} pts")
    if not all_pts:
        log("[dense] SGBM produced nothing; keeping sparse cloud only")
        ctx.dense = {"points": np.zeros((0, 3)), "colors": np.zeros((0, 3))}
        return ctx.dense

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

    up = estimate_up(ctx.views, ctx.sparse)
    ground = surface_filter(pts, up)
    log(f"[dense] surface-normal filter: keeping {int(ground.sum())}/{len(pts)} "
        f"(dropped {int((~ground).sum())} near-vertical/non-ground points, "
        f"up={np.round(up, 2).tolist()})")
    pts, cols = pts[ground], cols[ground]
    # float32 halves RAM and cache size; at ~meters-per-model-unit scale the
    # ~1e-7 relative error is microns — invisible to volume integration
    pts = pts.astype(np.float32)
    ctx.dense = {"points": pts, "colors": cols}
    np.savez_compressed(cache, points=pts, colors=cols)
    log(f"[dense] fused cloud: {len(pts)} points "
        f"(voxel {voxel:.4g}, scene extent {extent:.3g})")
    return ctx.dense

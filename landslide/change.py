"""Multi-temporal change monitoring: two epochs of the same site.

Both clouds are marker-scaled metric models. Epoch B is rigidly registered
onto epoch A with trimmed ICP (the changed debris is rejected from the
correspondence cut, so the pile itself cannot bias the alignment), then
epoch A becomes the prior surface for epoch B via dem_volume — the same
LoD/slope/bridging machinery as a single-epoch measurement decides what is
real change.

Use the CLI:  python -m landslide.cli change JOB_A_DIR JOB_B_DIR
(each job dir needs photos/, work/, and a set scale in state.json)
"""
from __future__ import annotations

import numpy as np

from .dem import DemSurface, icp_rigid
from .sfm import Log, ReconCtx
from .volume import dem_volume


def _marker_transform(info_a: dict, info_b: dict, log: Log):
    """Rigid B->A transform from the metric ArUco corners of both epochs.

    If the same physical marker was placed at the same spot for both
    surveys, each reconstruction holds its four metric corners; Kabsch on
    four points (scale fixed at 1 — both models are already metric) gives
    the exact registration, with no ICP sliding on planar roads and no
    dependence on scene overlap. Returns (R, t, max_corner_err_m) or None.
    """
    ca = info_a.get("marker_corners_m")
    cb = info_b.get("marker_corners_m")
    if not ca or not cb or len(ca) != 4 or len(cb) != 4:
        return None
    A = np.asarray(ca, np.float64)
    B = np.asarray(cb, np.float64)
    # same physical marker => same side lengths; a big mismatch means the
    # user re-scaled with a different reference and the anchor is suspect
    sa = np.mean([np.linalg.norm(A[i] - A[(i + 1) % 4]) for i in range(4)])
    sb = np.mean([np.linalg.norm(B[i] - B[(i + 1) % 4]) for i in range(4)])
    if abs(sa - sb) / sa > 0.1:
        log(f"[change] marker side differs between epochs "
            f"({sa:.3f} vs {sb:.3f} m) — the marker may have moved or been "
            "rescaled; falling back to ICP")
        return None
    # corners are ordered, but the in-plane start index may differ; try all
    # four cyclic shifts. Four points are coplanar -> planar-Kabsch
    # degeneracy (the rotation about the plane normal is pinned but the
    # component flipping the normal is not): add the marker's face normal
    # as a virtual 5th point, both signs, and keep the best fit
    from .geo import umeyama
    n_a = np.cross(A[1] - A[0], A[3] - A[0])
    n_a = n_a / max(np.linalg.norm(n_a), 1e-12) * sa
    n_b = np.cross(B[1] - B[0], B[3] - B[0])
    n_b = n_b / max(np.linalg.norm(n_b), 1e-12) * sb
    best = None
    for sign in (1.0, -1.0):
        A5 = np.vstack([A, A.mean(0) + sign * n_a])
        B5 = np.vstack([B, B.mean(0) + sign * n_b])
        s, R, t = umeyama(B5, A5, fixed_scale=1.0)
        err = float(np.linalg.norm(B5 @ R.T + t - A5, axis=1).max())
        if best is None or err < best[2]:
            best = (R, t, err)
    R, t, err = best
    if err > 0.25 * max(sa, 1e-9):
        log(f"[change] marker-corner registration residual is large "
            f"({err:.3f} m) — corners may be mis-triangulated; falling "
            "back to ICP")
        return None
    log(f"[change] marker-anchored registration: max corner residual "
        f"{err * 1000:.0f} mm — exact transform, no ICP needed")
    return R, t, err


def change_volume(ctx_a: ReconCtx, ctx_b: ReconCtx, log: Log = print,
                  max_pts: int = 60_000, up=None) -> dict:
    """Epoch-B-minus-epoch-A change; positive net = material added.

    Registration priority: shared physical ArUco marker (exact rigid
    transform from the persisted metric corners) -> gravity-seeded trimmed
    point-to-plane ICP (fallback when no shared anchor exists).
    """
    from .densify import estimate_up
    if not ctx_a.scale_info.get("applied") or not ctx_b.scale_info.get(
            "applied"):
        raise RuntimeError("both epochs need their metric scale set "
                           "(marker / manual reference) before comparison")
    A, _ = ctx_a.cloud(dense=True)
    B, _ = ctx_b.cloud(dense=True)
    A = np.asarray(A, np.float64) * ctx_a.scale
    B = np.asarray(B, np.float64) * ctx_b.scale
    for tag, pts in (("A", A), ("B", B)):
        if len(pts) < 200:
            raise RuntimeError(f"epoch {tag} has only {len(pts)} points — "
                               "build the dense cloud first")

    marker = _marker_transform(ctx_a.scale_info, ctx_b.scale_info, log)
    if marker is not None:
        R_full, t, m_err = marker
        rms = m_err
        method = "marker"
    else:
        if up is None:
            up = estimate_up(ctx_b.views, ctx_b.sparse)
        from .dem import _gravity_R
        R0 = _gravity_R(up)
        src = B @ R0.T
        if len(src) > max_pts:
            rng = np.random.default_rng(0)
            src = src[rng.choice(len(src), max_pts, replace=False)]
        from scipy.spatial import cKDTree
        R, t, rms = icp_rigid(src, cKDTree(A),
                              init_t=A.mean(0) - src.mean(0), log=log)
        R_full = R @ R0
        method = "icp"

    # epoch B region = its own cloud, mapped into epoch A's frame
    B_in_A = B @ R_full.T + t
    surf = DemSurface(A)
    res = dem_volume(B_in_A, surf, log=log)
    res["datum"] = "prior_epoch"
    res["icp_rms_m"] = rms
    res["registration"] = method
    return res

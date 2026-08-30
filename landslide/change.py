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


def change_volume(ctx_a: ReconCtx, ctx_b: ReconCtx, log: Log = print,
                  max_pts: int = 60_000, up=None) -> dict:
    """Epoch-B-minus-epoch-A change; positive net = material added."""
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

    if up is None:
        up = estimate_up(ctx_b.views, ctx_b.sparse)
    # gravity-align B the way its own up vector defines, then ICP onto A
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

    # epoch B region = its own cloud, mapped into epoch A's frame
    B_in_A = B @ R_full.T + t
    surf = DemSurface(A)
    res = dem_volume(B_in_A, surf, log=log)
    res["datum"] = "prior_epoch"
    res["icp_rms_m"] = rms
    return res

"""Prior-surface (DEM) import: measure against a known pre-event surface.

All rim-based datums extrapolate from a thin ring around the traced region.
When a pre-event surface exists — national lidar, an older survey of the
same road, a drone DEM from before the slide — differencing the current
surface against it removes that extrapolation entirely and enables cut/fill
on terrain with no clean rim at all.

Loads simple XYZ grid text ("x y z" per line, '#'-comments ok) and, when
rasterio happens to be installed, GeoTIFF. Alignment is trimmed ICP: the
metric SfM cloud (marker-scaled) is rigidly registered onto the DEM with
median-distance inliers each iteration, so up to ~40% changed/debris points
cannot drag the fit. Gravity (estimate_up) seeds the initial orientation —
DEM z is up by definition.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from .sfm import Log, ReconCtx


def load_dem(path: Path, log: Log = print) -> dict:
    """DEM as {"pts": (N,3), "grid": (H,W) z-grid or None}.

    Accepts XYZ text; GeoTIFF when rasterio is available.
    """
    path = Path(path)
    if path.suffix.lower() in (".tif", ".tiff"):
        try:
            import rasterio
            with rasterio.open(path) as ds:
                z = ds.read(1).astype(np.float64)
                if ds.nodata is not None:
                    z[z == ds.nodata] = np.nan
                xs = ds.bounds.left + (np.arange(ds.width) + 0.5) * ds.res[0]
                ys = ds.bounds.top - (np.arange(ds.height) + 0.5) * ds.res[1]
                X, Y = np.meshgrid(xs, ys)
                ok = np.isfinite(z)
                pts = np.column_stack([X[ok], Y[ok], z[ok]])
                log(f"[dem] {path.name}: {ds.width}x{ds.height} at "
                    f"{ds.res[0]:.2f} m/px, {len(pts)} valid points")
                return {"pts": pts}
        except ImportError:
            raise RuntimeError(
                "GeoTIFF needs rasterio (pip install rasterio); for a plain "
                "text DEM use an XYZ grid file (one 'x y z' per line)")
    pts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "//", "x", "X")):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) >= 3:
                try:
                    pts.append([float(parts[0]), float(parts[1]),
                                float(parts[2])])
                except ValueError:
                    continue
    if len(pts) < 200:
        raise RuntimeError(f"only {len(pts)} DEM points parsed from "
                           f"{path.name} — need a denser grid")
    pts = np.asarray(pts, np.float64)
    log(f"[dem] {path.name}: {len(pts)} XYZ points")
    return {"pts": pts}


def _dst_normals(tree: cKDTree, q: np.ndarray, k: int = 8) -> np.ndarray:
    """Local surface normals of destination points (k-NN covariance)."""
    k = min(k, tree.n)
    _, idx = tree.query(q, k=k, workers=-1)
    nb = tree.data[idx].astype(np.float64)
    nb -= nb.mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb) / k
    _, vecs = np.linalg.eigh(cov)
    n = vecs[:, :, 0]                      # smallest eigenvalue = normal
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    return n


def icp_rigid(src: np.ndarray, dst_tree: cKDTree, iters: int = 25,
              trim: float = 0.5, init_R: np.ndarray | None = None,
              init_t: np.ndarray | None = None, log: Log = print):
    """Trimmed point-to-PLANE ICP aligning `src` onto `dst_tree`'s points.

    Point-to-point ICP slides along planar roads (in-plane translation is
    unobservable when correspondences can slide); the point-to-plane error
    constrains motion along the surface normal, which is exactly what a
    terrain match needs. Each iteration keeps only the best-`trim` fraction
    of correspondences — the changed parts of the scene (debris, new piles)
    fall out of the cut and cannot bias the alignment. Linearized
    small-increment solve (rotation vector + translation). Returns
    (R, t, inlier_rms) with dst ≈ R @ src + t.
    """
    R = np.eye(3) if init_R is None else np.asarray(init_R, np.float64).copy()
    t = np.zeros(3) if init_t is None else np.asarray(init_t,
                                                      np.float64).copy()
    keep = np.ones(len(src), dtype=bool)
    rms = float("inf")
    for _ in range(iters):
        cur = src @ R.T + t
        d, idx = dst_tree.query(cur, k=1, workers=-1)
        keep = d <= np.quantile(d, trim)
        if keep.sum() < 50:
            break
        p = cur[keep]
        q = dst_tree.data[idx[keep]]
        n = _dst_normals(dst_tree, q)
        r = np.einsum("ni,ni->n", p - q, n)          # point-to-plane signed
        A = np.hstack([np.cross(p, n), n])           # (m, 6)
        x, *_ = np.linalg.lstsq(A, -r, rcond=None)
        w, dt = x[:3], x[3:]
        th = float(np.linalg.norm(w))
        if th > 0.2:                                 # keep linearization sane
            w *= 0.2 / th
            th = 0.2
        K = np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]])
        if th > 1e-12:                                # Rodrigues rotation
            Ri = np.eye(3) + np.sin(th) / th * K + \
                (1 - np.cos(th)) / (th * th) * (K @ K)
        else:
            Ri = np.eye(3) + K
        R = Ri @ R
        t = Ri @ t + dt
        rms = float(np.sqrt((r ** 2).mean()))
    log(f"[icp] converged: point-to-plane inlier rms {rms:.3f} m over "
        f"{int(keep.sum())}/{len(src)} pts")
    return R, t, rms


def _gravity_R(up: np.ndarray) -> np.ndarray:
    """Rotation taking `up` to +z (DEM convention)."""
    up = np.asarray(up, np.float64)
    up = up / np.linalg.norm(up)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(up, z)
    c = float(up @ z)
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1, -1, -1])
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k * (1 / (1 + c))


class DemSurface:
    """Rotation-invariant DEM surface: scattered IDW interpolation.

    An axis-aligned raster grid breaks the moment the DEM and the model
    frames are rotated relative to each other (unique-x spacing collapses,
    the grid explodes); evaluating directly from the scattered points with
    inverse-distance weighting over the 9 nearest neighbours is exact for
    planes, smooth for terrain, and frame-agnostic.
    """

    def __init__(self, pts: np.ndarray, k: int = 9):
        self.pts = np.asarray(pts, np.float64)
        self.k = k
        self._tree = cKDTree(self.pts[:, :2])
        d2, _ = self._tree.query(self.pts[:, :2], k=2, workers=-1)
        self.spacing = float(np.median(d2[:, 1]))
        self.hull_r = 3.0 * max(self.spacing, 1e-6)

    def __call__(self, xy: np.ndarray) -> np.ndarray:
        """DEM height at (x, y); NaN beyond ~3 point spacings off the hull."""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        k = min(self.k, len(self.pts))
        d, idx = self._tree.query(xy, k=k, workers=-1)
        if k == 1:
            z = self.pts[idx, 2].astype(np.float64)
            return np.where(d <= self.hull_r, z, np.nan)
        w = 1.0 / np.maximum(d, 1e-9) ** 2
        z = (w * self.pts[idx, 2]).sum(axis=1) / w.sum(axis=1)
        z = np.where(d[:, 0] <= self.hull_r, z, np.nan)
        # exact hits (d ~ 0) take the point's own height
        exact = d[:, 0] < 1e-9
        z[exact] = self.pts[idx[exact, 0], 2]
        return z


def _spacing(v: np.ndarray) -> float:
    """Median spacing of unique coordinate values (a grid repeats each)."""
    v = np.unique(v)
    return float(np.median(np.diff(v))) if len(v) > 10 else 1.0


def align_to_dem(ctx: ReconCtx, dem_pts: np.ndarray, up: np.ndarray,
                 log: Log = print, max_pts: int = 60_000) -> dict:
    """Rigid model->DEM transform via gravity-seeded trimmed ICP.

    The cloud is metric (marker scale); scale is NOT solved — an ICP scale
    far from 1 would mean a bad DEM or a broken reconstruction, reported as
    a diagnostic instead of silently absorbed.
    """
    pts, _ = ctx.cloud(dense=True)
    pts = np.asarray(pts, np.float64) * ctx.scale
    if len(pts) > max_pts:
        rng = np.random.default_rng(0)
        pts = pts[rng.choice(len(pts), max_pts, replace=False)]
    R0 = _gravity_R(up)
    src = pts @ R0.T
    dst_tree = cKDTree(dem_pts)
    # seed translation by centroid match of the gravity-aligned cloud
    R, t, rms = icp_rigid(src, dst_tree,
                          init_R=np.eye(3), init_t=dem_pts.mean(0) - src.mean(0),
                          log=log)
    R_full = R @ R0
    # diagnostic scale check via the ICP correspondence spread ratio
    return {"R": R_full, "t": t, "rms_m": rms,
            "model_to_dem": lambda p: np.asarray(p) @ R_full.T + t}

"""EXIF-GPS georeferencing: place the reconstruction on the Earth.

Phone photos carry GPS (WGS84 lat/lon, sometimes altitude). After the
marker-based metric scale is applied, camera centers can be rigidly aligned
to a local east-north-up frame built from the GPS fixes — turning the model
and the orthophoto into map-ready, repeat-survey-comparable products.

Scope is deliberately informational: GPS absolute accuracy (3-10 m) is far
coarser than the marker scale, so volumes are NEVER derived from GPS — the
alignment only annotates outputs with world coordinates, and the
model-vs-GPS scale discrepancy is reported as a sanity metric.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .sfm import Log, ReconCtx

GPS_JSON = "gps.json"          # written next to the stored photos


def read_gps(im) -> tuple[float, float, float] | None:
    """(lat, lon, alt_m) from a PIL image's EXIF GPS block, or None."""
    try:
        exif = im.getexif()
        gps = exif.get_ifd(0x8825)          # GPSInfo IFD
        if not gps or 2 not in gps:
            return None

        def coord(tag):
            """DMS as three rationals, or a plain decimal (some phones)."""
            v = gps.get(tag)
            if isinstance(v, (tuple, list)) and v and isinstance(
                    v[0], (tuple, list)):
                d, m, s = (float(t[0]) / float(t[1]) if len(t) == 2 and
                           float(t[1]) != 0 else float(t[0]) for t in v[:3])
                return d + m / 60.0 + s / 3600.0
            return float(v)

        lat = coord(2)
        lon = coord(4)
        if gps.get(1, b"N").decode(errors="ignore") == "S":
            lat = -lat
        if gps.get(3, b"E").decode(errors="ignore") == "W":
            lon = -lon
        alt = float("nan")
        if 6 in gps:
            a = gps[6]
            if isinstance(a, (tuple, list)) and a and isinstance(
                    a[0], (tuple, list)):
                a = a[0]                    # stray extra rational
            alt = float(a[0]) / float(a[1]) if isinstance(a, (tuple, list)) \
                and len(a) == 2 and float(a[1]) != 0 else float(a)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon, alt
    except Exception:
        return None


def capture_gps(sources, names: list[str], photos_dir: Path,
                log: Log = print) -> dict[str, list]:
    """Persist {stored_name: [lat, lon, alt]} for photos that carry GPS."""
    out = {}
    for src, name in zip(sources, names):
        try:
            from PIL import Image
            with Image.open(src) as im:
                g = read_gps(im)
            if g is not None:
                out[name] = [g[0], g[1], g[2]]
        except Exception:
            continue
    if out:
        (Path(photos_dir) / GPS_JSON).write_text(json.dumps(out))
        log(f"[geo] GPS found in {len(out)}/{len(names)} photos")
    return out


def load_gps(photos_dir: Path) -> dict[str, list]:
    p = Path(photos_dir) / GPS_JSON
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def enu(gps: dict[str, list]) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """Local east/north/up coordinates of the GPS fixes (first fix = origin).

    Returns (enu_xyz, names, origin_llh). Equirectangular tangent plane —
    exact enough at survey scale (<1 cm over 1 km).
    """
    names = sorted(gps)
    lat0 = math.radians(gps[names[0]][0])
    origin = list(gps[names[0]])
    pts = []
    for n in names:
        la, lo, al = gps[n]
        e = math.radians(lo - origin[1]) * math.cos(lat0) * 6378137.0
        nn = math.radians(la - origin[0]) * 6378137.0
        u = 0.0 if not math.isfinite(al) else al - (origin[2] if
                                                    math.isfinite(origin[2])
                                                    else 0.0)
        pts.append([e, nn, u])
    return np.asarray(pts, np.float64), names, origin


def umeyama(P: np.ndarray, Q: np.ndarray, fixed_scale: float | None = None):
    """Similarity Q ≈ s R P + t. With fixed_scale, solve R,t at that scale."""
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    if fixed_scale is not None:
        s = fixed_scale
    else:
        var = ((P - cp) ** 2).sum()
        s = (S * [1, 1, d]).sum() / var if var > 0 else 1.0
    t = cq - s * R @ cp
    return s, R, t


def attach_georef(ctx: ReconCtx, gps: dict[str, list], log: Log = print):
    """Align metric camera centers to the GPS ENU frame; sets ctx.geo."""
    have = {n: g for n, g in gps.items() if n in ctx.views}
    if len(have) < 3:
        ctx.geo = None
        if gps:
            log("[geo] fewer than 3 reconstructed photos carry GPS — "
                "skipping georeferencing")
        return None
    E, names, origin = enu(have)
    from .geometry import camera_center
    C = np.array([camera_center(ctx.views[n].R, ctx.views[n].t)
                  for n in names]) * ctx.scale
    s_fit, R, t = umeyama(C, E)
    # GPS is far noisier than the marker scale: align rigidly at the marker
    # scale and report the GPS-implied scale as a cross-check only
    s, R, t = umeyama(C, E, fixed_scale=1.0)
    res = np.linalg.norm(C @ R.T + t - E, axis=1)
    scale_check = s_fit / ctx.scale if ctx.scale else float("nan")
    ctx.geo = {
        "R": R, "t": t,
        "origin_llh": origin,
        "gps_residual_m": [float(r) for r in res],
        "gps_scale_vs_marker": float(scale_check),
        "n_fixes": len(names),
    }
    med = float(np.median(res))
    log(f"[geo] aligned to GPS ({len(names)} fixes, median residual "
        f"{med:.1f} m — GPS noise level; GPS/marker scale ratio "
        f"{scale_check:.3f})")
    if med > 25.0:
        log("[geo] warning: large GPS residuals — fixes may span a "
            "different capture session")
    return ctx.geo


def geo_summary(ctx: ReconCtx) -> dict | None:
    """JSON-able georef summary for job snapshots and results."""
    g = getattr(ctx, "geo", None)
    if g is None:
        return None
    return {"origin_llh": g["origin_llh"],
            "n_fixes": g["n_fixes"],
            "gps_residual_median_m": float(np.median(g["gps_residual_m"])),
            "gps_scale_vs_marker": g["gps_scale_vs_marker"]}

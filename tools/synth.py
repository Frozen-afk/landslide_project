"""Synthetic validation dataset generator.

Renders a textured terrain (gentle noise) with a known cosine "landslide"
bowl and a large ArUco-style checker marker, from 21 overlapping viewpoints
left->right, using a pinhole camera. Writes photos + ground_truth.json +
a ready-to-run spec.json for the CLI.

Ground truth volume = integral of the bowl inside the GT polygon.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

# ---- scene ---------------------------------------------------------------
L = 36.0          # terrain extent (m)
N = 170           # heightfield grid
BOWL = {"x": 17.5, "y": 18.5, "R": 6.0, "depth": 2.0}
# ArUco marker on a vertical board on the far side of the bowl (7.5 m from
# centre), facing the camera arc — a ground marker would be too foreshortened.
# nsub must be 10*j+1 so grid lines land exactly on every pattern bit boundary
# (unit = side/8 = 0.25 m): j=4 -> spacing 6.25 cm, sharp enough that Gouraud
# interpolation doesn't blur the bits, exact enough that edges aren't quantized.
MARKER = {"x": 17.5, "y": 26.0, "side": 2.0, "mat": 2.5, "nsub": 41}
POLY_R = 5.6      # polygon radius around bowl centre

W_IMG, H_IMG = 1200, 900
F_PX = 1600.0     # narrow-ish FOV so the marker resolves well
N_VIEWS = 21
YAW_SPAN = 96.0   # degrees, -48..48
EYE_RADIUS = 26.0
EYE_HEIGHT = 7.0

EXTS_OK = (".jpg", ".jpeg", ".png")


def value_noise(n: int, cells: int, rng) -> np.ndarray:
    r = rng.random((cells, cells)).astype(np.float32)
    return cv2.resize(r, (n, n), interpolation=cv2.INTER_CUBIC)


def build_terrain(seed: int = 7):
    rng = np.random.default_rng(seed)
    n = N
    h = np.zeros((n, n), np.float64)
    for cells, amp in [(6, 0.35), (12, 0.15), (24, 0.08), (48, 0.04)]:
        h += amp * value_noise(n, cells, rng)
    xs = np.linspace(0, L, n)
    X, Y = np.meshgrid(xs, xs, indexing="ij")
    h += 0.10 * X + 0.03 * Y

    r = np.sqrt((X - BOWL["x"]) ** 2 + (Y - BOWL["y"]) ** 2)
    bowl = np.where(r < BOWL["R"],
                    BOWL["depth"] * 0.5 * (1 + np.cos(np.pi * r / BOWL["R"])),
                    0.0)
    z = h - bowl

    # colors: earthy multi-octave texture + per-vertex speckle (stereo needs it)
    tex = np.zeros((n, n), np.float64)
    for cells, amp in [(8, 0.5), (20, 0.3), (60, 0.2)]:
        tex += amp * value_noise(n, cells, rng)
    lum = 95 + 65 * tex / tex.std() * 0.35 + rng.normal(0, 13, (n, n))
    lum = np.clip(lum, 25, 235)
    colors = np.stack([lum, lum * 0.9, lum * 0.72], axis=-1)

    dx = L / (n - 1)
    vol_true_full = float(bowl.sum() * dx * dx)
    inside_poly = r < POLY_R
    vol_true_poly = float(bowl[inside_poly].sum() * dx * dx)
    return X, Y, z, colors, dict(vol_true_full=vol_true_full,
                                  vol_true_poly=vol_true_poly)


def add_marker(X, Y, z, colors):
    """ArUco bit pattern on a vertical white board facing the camera arc."""
    ar = cv2.aruco
    dictionary = ar.getPredefinedDictionary(ar.DICT_6X6_250)
    pattern = ar.generateImageMarker(dictionary, 0, 96, borderBits=1)

    cx, cy = MARKER["x"], MARKER["y"]
    side, mat, nsub = MARKER["side"], MARKER["mat"], MARKER["nsub"]
    zb = terrain_height(X, Y, z, cx, cy) + 0.05      # board bottom
    xs = np.linspace(cx - mat / 2, cx + mat / 2, nsub)
    zs = np.linspace(zb, zb + mat, nsub)

    mverts, mcolors = [], []
    for x in xs:
        for zv in zs:
            mverts.append((x, cy, zv))
            fx = (x - (cx - side / 2)) / side
            fy = ((zb + mat / 2 + side / 2) - zv) / side   # row 0 = top
            if 0.0 <= fx < 1.0 and 0.0 <= fy < 1.0:
                lum = float(pattern[min(int(fy * 96), 95), min(int(fx * 96), 95)])
                c = 30.0 if lum < 128 else 232.0
            else:
                c = 235.0                          # white quiet zone
            mcolors.append((c, c, c))
    mtris = []
    for i in range(nsub - 1):
        for j in range(nsub - 1):
            a = i * nsub + j
            mtris += [(a, a + 1, a + nsub + 1), (a, a + nsub + 1, a + nsub)]
    # corners of the ArUco pattern itself (not the mat), TL/TR/BR/BL as seen
    zt, zbl = zb + mat / 2 + side / 2, zb + mat / 2 - side / 2
    corners = np.array([(cx - side / 2, cy, zt),
                        (cx + side / 2, cy, zt),
                        (cx + side / 2, cy, zbl),
                        (cx - side / 2, cy, zbl)])
    return (np.asarray(mverts), np.asarray(mtris), np.asarray(mcolors), corners)


def terrain_height(X, Y, z, x, y) -> float:
    n = z.shape[0]
    fx = np.clip(x / L * (n - 1), 0, n - 2)
    fy = np.clip(y / L * (n - 1), 0, n - 2)
    i, j = int(fx), int(fy)
    tx, ty = fx - i, fy - j
    return float((z[i, j] * (1 - tx) * (1 - ty) + z[i + 1, j] * tx * (1 - ty) +
                  z[i, j + 1] * (1 - tx) * ty + z[i + 1, j + 1] * tx * ty))


def grid_tris(n: int) -> np.ndarray:
    idx = np.arange(n * n).reshape(n, n)
    t0 = np.stack([idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:]], -1)
    t1 = np.stack([idx[:-1, :-1], idx[1:, 1:], idx[:-1, 1:]], -1)
    return np.concatenate([t0.reshape(-1, 3), t1.reshape(-1, 3)])


def look_at(eye, target, up=(0, 0, 1)):
    """CV convention: x right, y down, z forward (x_cam = R (X - eye))."""
    zc = np.asarray(target) - np.asarray(eye)
    zc = zc / np.linalg.norm(zc)
    xc = np.cross(zc, up)
    xc = xc / np.linalg.norm(xc)
    yc = np.cross(zc, xc)
    R = np.stack([xc, yc, zc])
    t = -R @ np.asarray(eye)
    return R, t


def render(verts, tris, vcolors, R, t, K):
    h, w = H_IMG, W_IMG
    img = np.zeros((h, w, 3), np.uint8)
    zbuf = np.full(h * w, -1e18, np.float64)

    Xc = verts @ R.T + t
    z = Xc[:, 2]
    ok = z > 0.3
    u = K[0, 0] * Xc[:, 0] / z + K[0, 2]
    v = K[1, 1] * Xc[:, 1] / z + K[1, 2]
    iz = 1.0 / z
    px = np.stack([u, v], axis=1)

    for i0, i1, i2 in tris:
        if not (ok[i0] and ok[i1] and ok[i2]):
            continue
        p0, p1, p2 = px[i0], px[i1], px[i2]
        x0 = int(np.floor(max(0.0, min(p0[0], p1[0], p2[0]))))
        x1 = int(np.ceil(min(w - 1.0, max(p0[0], p1[0], p2[0])))) + 1
        y0 = int(np.floor(max(0.0, min(p0[1], p1[1], p2[1]))))
        y1 = int(np.ceil(min(h - 1.0, max(p0[1], p1[1], p2[1])))) + 1
        if x0 >= x1 or y0 >= y1:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        gx = gx.ravel()
        gy = gy.ravel()
        w0 = (p1[0] - p0[0]) * (gy - p0[1]) - (p1[1] - p0[1]) * (gx - p0[0])
        w1 = (p2[0] - p1[0]) * (gy - p1[1]) - (p2[1] - p1[1]) * (gx - p1[0])
        w2 = (p0[0] - p2[0]) * (gy - p2[1]) - (p0[1] - p2[1]) * (gx - p2[0])
        m = ((w0 >= 0) & (w1 >= 0) & (w2 >= 0)) | ((w0 <= 0) & (w1 <= 0) & (w2 <= 0))
        if not m.any():
            continue
        area_inv = 1.0 / (w0[m] + w1[m] + w2[m])
        izv = (w0[m] * iz[i0] + w1[m] * iz[i1] + w2[m] * iz[i2]) * area_inv
        colv = (w0[m, None] * vcolors[i0] + w1[m, None] * vcolors[i1] +
                w2[m, None] * vcolors[i2]) * area_inv[:, None]
        idxs = (gy[m].astype(np.int64)) * w + (gx[m].astype(np.int64))
        upd = izv > zbuf[idxs]
        sel = idxs[upd]
        zbuf[sel] = izv[upd]
        img.reshape(-1, 3)[sel] = np.clip(colv[upd], 0, 255).astype(np.uint8)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="synth")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = Path(args.out)
    img_dir = out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    X, Y, z, colors, gt_vol = build_terrain(args.seed)
    verts = np.stack([X.ravel(), Y.ravel(), z.ravel()], axis=1)
    vcolors = colors.reshape(-1, 3)
    tris = grid_tris(N)
    mv, mt, mc, marker_corners = add_marker(X, Y, z, colors)
    verts = np.concatenate([verts, mv])
    vcolors = np.concatenate([vcolors, mc])
    tris = np.concatenate([tris, mt + N * N])

    K = np.array([[F_PX, 0, W_IMG / 2], [0, F_PX, H_IMG / 2], [0, 0, 1.0]])
    target = np.array([BOWL["x"], BOWL["y"],
                       terrain_height(X, Y, z, BOWL["x"], BOWL["y"])])
    zc_mean = z.mean()
    eye_base = np.array([BOWL["x"], BOWL["y"], zc_mean + EYE_HEIGHT])

    poses = {}
    middle = N_VIEWS // 2
    for k in range(N_VIEWS):
        yaw = np.deg2rad(-YAW_SPAN / 2 + YAW_SPAN * k / (N_VIEWS - 1))
        eye = np.array([BOWL["x"] + EYE_RADIUS * np.sin(yaw),
                        BOWL["y"] - EYE_RADIUS * np.cos(yaw),
                        eye_base[2]])
        R, t = look_at(eye, target)
        img = render(verts, tris, vcolors, R, t, K)
        name = f"IMG_{k:02d}.png"
        cv2.imwrite(str(img_dir / name), img)
        poses[name] = {"R": R.tolist(), "t": t.tolist(), "eye": eye.tolist()}

    # GT polygon: circle of radius POLY_R around the bowl, in the middle view
    ang = np.linspace(0, 2 * np.pi, 72, endpoint=False)
    poly3d = np.stack([BOWL["x"] + POLY_R * np.cos(ang),
                       BOWL["y"] + POLY_R * np.sin(ang)], axis=1)
    poly3d = np.stack([poly3d[:, 0], poly3d[:, 1],
                       [terrain_height(X, Y, z, x, y) for x, y in poly3d]], 1)
    Rm, tm = np.array(poses[f"IMG_{middle:02d}.png"]["R"]), \
        np.array(poses[f"IMG_{middle:02d}.png"]["t"])
    Pc = poly3d @ Rm.T + tm
    poly_px = np.stack([K[0, 0] * Pc[:, 0] / Pc[:, 2] + K[0, 2],
                        K[1, 1] * Pc[:, 1] / Pc[:, 2] + K[1, 2]], 1)
    assert poly_px[:, 0].min() > 10 and poly_px[:, 0].max() < W_IMG - 10
    assert poly_px[:, 1].min() > 10 and poly_px[:, 1].max() < H_IMG - 10

    gt = {
        "volume_true_full_m3": gt_vol["vol_true_full"],
        "volume_true_polygon_m3": gt_vol["vol_true_poly"],
        "bowl": BOWL, "polygon_radius_m": POLY_R,
        "marker": {**MARKER, "corners_world": marker_corners.tolist()},
        "K": K.tolist(), "image_size": [W_IMG, H_IMG],
        "poses": poses,
        "polygon_image": f"IMG_{middle:02d}.png",
        "polygon_px": poly_px.tolist(),
    }
    (out / "ground_truth.json").write_text(json.dumps(gt, indent=1))

    spec = {
        "photos_dir": str(img_dir.resolve()),
        "out_dir": str((out / "output").resolve()),
        "save_cloud": True,
        "scale": {"method": "aruco", "side_m": MARKER["side"],
                  "dict": "DICT_6X6_250", "id": 0},
        "region": {"image": f"IMG_{middle:02d}.png",
                   "polygon": poly_px.tolist(), "dense": True, "rim_px": 14},
    }
    (out / "spec.json").write_text(json.dumps(spec, indent=1))
    print(f"true bowl volume (full / inside polygon): "
          f"{gt_vol['vol_true_full']:.1f} / {gt_vol['vol_true_poly']:.1f} m^3")
    print(f"wrote {N_VIEWS} photos to {img_dir}")


if __name__ == "__main__":
    main()

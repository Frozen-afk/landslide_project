"""Generates the S2 per-photo (T1) check fixtures used by tests/test_capture.py
(GUIDED_CAPTURE_PLAN.md S2, criterion 2). Re-run after any threshold change
in landslide/capture.py:

    .venv/bin/python -m tests.fixtures.capture_t1.make_fixtures

Sources, all synthetic (same convention as tests/fixtures/capture/, S1 — no
real phone JPEG exists anywhere in this repo):
- overlap/marker fixtures: crops of the already-rendered `data/bench/{arc,
  nadir,sparse8}` presets (tools/synth.py --preset ..., run once for
  GUIDED_CAPTURE_PROGRESS.md's S0/S0b/S0c passes) — real 3-D geometry, real
  ArUco board, so the marker/overlap math is exercised on realistic content,
  not hand-drawn shapes. If those directories are missing, this script
  re-renders just the handful of frames it needs directly from tools.synth,
  at the same seed.
- pivot fixtures: a from-scratch synth render, two frames from the exact
  same eye position with only a 10 deg pan between them (a pure turn-in-
  place — no preset in tools/synth.py models this, every existing path
  moves the eye).
- texture fixtures: plain PIL images (no 3-D content at all — "water, snow,
  sky" per SS4.1's own instruction text) rather than tools/synth.py's
  tex_scale=0.25 lowtex preset, which was measured to leave SIFT's keypoint
  count on this synthetic scene *unchanged* from tex_scale=1.0 (contrast
  scaling alone doesn't move SIFT's scale-invariant extrema here) and so
  does not exercise the FEATURES_FAIL branch at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import cv2
import numpy as np
from PIL import Image, ImageFilter

from tools import synth

HERE = Path(__file__).resolve().parent
BENCH = Path("data/bench")
SEED = 7


def _save(img_bgr: np.ndarray, name: str, max_side: int = 1000) -> None:
    im = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    s = min(1.0, max_side / max(im.size))
    if s < 1.0:
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    im.save(HERE / name, "JPEG", quality=90)


def _from_bench(preset: str, idx: int, name: str) -> None:
    src = BENCH / preset / "images" / f"IMG_{idx:02d}.png"
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(
            f"{src} missing — run "
            f"`.venv/bin/python -m tools.synth --preset {preset} --out {BENCH / preset}` first")
    _save(img, name)


def _build_arc_scene():
    p = synth.PRESETS["arc"]
    X, Y, z, colors, _gt = synth.build_terrain(SEED, tex_scale=p["tex_scale"])
    verts = np.stack([X.ravel(), Y.ravel(), z.ravel()], axis=1)
    vcolors = colors.reshape(-1, 3)
    tris = synth.grid_tris(synth.N)
    mv, mt, mc, _corners = synth.add_marker(X, Y, z, colors)
    verts = np.concatenate([verts, mv])
    vcolors = np.concatenate([vcolors, mc])
    tris = np.concatenate([tris, mt + synth.N * synth.N])
    K = np.array([[p["f_px"], 0, synth.W_IMG / 2],
                  [0, p["f_px"], synth.H_IMG / 2], [0, 0, 1.0]])
    return p, X, Y, z, verts, vcolors, tris, K


def _make_pivot_pair() -> None:
    """Two frames from one fixed eye, panned 10 deg apart — a pure turn-in-
    place. The camera translation between them is exactly zero, unlike any
    existing tools/synth.py preset (every path moves the eye), so this is
    built directly rather than sourced from data/bench."""
    p, X, Y, z, verts, vcolors, tris, K = _build_arc_scene()
    zc_mean = z.mean()
    bx, by = synth.BOWL["x"], synth.BOWL["y"]
    eye = np.array([bx, by - p["radius"], zc_mean + p["height"]])
    base_target = np.array([bx, by, synth.terrain_height(X, Y, z, bx, by)])
    up = np.array([0.0, 0.0, 1.0])

    def panned_target(deg: float) -> np.ndarray:
        # rotate (target - eye) about the vertical axis through eye by `deg`
        v = base_target - eye
        a = np.deg2rad(deg)
        c, s = np.cos(a), np.sin(a)
        rot = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        return eye + rot @ v

    for deg, name in [(0.0, "pivot_a.jpg"), (10.0, "pivot_b.jpg")]:
        R, t = synth.look_at(eye, panned_target(deg), up=up)
        img = synth.render(verts, tris, vcolors, R, t, K, k1=p["k1"])
        _save(img, name)


def _make_texture_fixtures() -> None:
    # "water, snow, sky" (SS4.1's own instruction text): near-uniform content
    # with no exploitable local structure for SIFT.
    rng = np.random.default_rng(3)
    blank = np.full((900, 1200, 3), 210, np.uint8)
    blank += rng.integers(-2, 3, blank.shape, dtype=np.int16).astype(np.uint8)
    Image.fromarray(blank).save(HERE / "texture_blank.jpg", "JPEG", quality=90)
    # the textured comparison fixture: an arc frame, real 3-D terrain + marker
    _from_bench("arc", 10, "texture_arc.jpg")


def main() -> None:
    _from_bench("arc", 10, "marker_arc.jpg")     # face-on marker -> ok
    _from_bench("nadir", 8, "marker_nadir.jpg")  # grazing marker -> edge-on fail
    _from_bench("sparse8", 0, "overlap_a.jpg")   # consecutive pair -> not-fail
    _from_bench("sparse8", 1, "overlap_b.jpg")
    _from_bench("sparse8", 3, "overlap_far.jpg")  # 3 steps from overlap_a -> fail
    _make_pivot_pair()
    _make_texture_fixtures()

    a = Image.open(HERE / "overlap_a.jpg")
    a.filter(ImageFilter.GaussianBlur(3)).save(HERE / "blurry.jpg", "JPEG", quality=90)

    manifest = {
        "marker_side_m": 0.5,
        "notes": "see this file's own docstring for provenance",
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote fixtures to {HERE}")


if __name__ == "__main__":
    main()

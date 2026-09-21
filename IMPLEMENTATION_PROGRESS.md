# Implementation progress

Tracks `implementation_plan.md` Part C. Tier 0 (step 1) below; Tier 1 (steps
3–6, 8 — geometry robustness) appended further down. Tier 2/3 not started.

## Validation against current repo

All Tier 0 items were re-checked against the code before implementing
(architecture.md / implementation_plan.md line numbers matched the tree as of
commit `9834dd2`). One design gap found during implementation, resolved with
the user before coding (see T0.4 note).

## Status

| Item | Status | Notes |
| --- | --- | --- |
| T0.1 EXIF focal prior | done | `pipeline.import_photos` now passes `exif=im.getexif().tobytes()` to the re-encode; verified EXIF survives `exif_transpose`→`convert`→`resize` in this Pillow version. |
| T0.2 Dense cache keyed to reconstruction | done | `sfm.build_ctx` computes `ReconCtx.fingerprint` (SHA-1 over sorted per-image poses + sparse point count); `densify.dense_cloud` names the cache `dense_<width>_<fp8>.npz` and verifies the fingerprint stored inside the npz before trusting it. `pipeline.run_spec` now calls `reconstruct(..., reuse=True)`. Removed the now-dead `ensure_reconstruction` (only caller was `run_spec`). |
| T0.3 Depth + occlusion in photo-mode selection | done | `volume.select_region` now reads `depth` from `ImageView.project` (previously discarded), requires `depth > 0`, and adds `_front_surface_mask` — a coarse (4px-cell) per-view z-buffer that keeps only points within `max(2% of median depth, ~0)` of the nearest depth in their cell before the polygon test. `pipeline.measure` now calls `select_region(..., extra_views=1)` (was 0) per the plan. |
| T0.4 Symmetric outlier clipping | done, design adjusted | Literal symmetric clip (same `max(1.5m, 8σ)` cap both sides) broke `test_volume.py`'s analytic bowl (2 m legitimate depression clipped) and 3 other tests — a landslide cut is expected to go well past 1.5 m, unlike fill. User chose: keep the existing cap for the high (fill) side; size the low (cut) side cap as `max(high_cap, 3×IQR(interior heights))` (Tukey extreme-outlier fence) so genuine deep depressions survive while near-surface stereo floaters still get trimmed. Added `n_low_dropped` to the result dict. |
| T0.5 Remove the focal hack | done | Deleted the `Ka[0,0] *= w/(2·cx)` / `Kb[...]` rescale in `densify.stereo_pair`; a top-left crop only changes `(w,h)`, `cx,cy` stay valid. |
| T0.6 Typed API models | done | Added `server/schemas.py` (Pydantic): `ArucoScaleRequest`, `ManualScaleRequest`, `MeasureRequest` (`rim_px`, `rim_inner_px`), `PointPair`, `ManualEndpoint`. `server/main.py` routes now take these instead of raw `dict`; `run_measure`/`_run_measure` pass `rim_inner_px` through (fixes S6, which was previously silently ignored). |
| T0.7 UI quick wins | done | `app.js`: canvas handlers switched from mouse to pointer events (`pointerdown/move/up`, `touch-action: none` on the marking canvases) for touch tracing (F1); slope-hazard figure added to the result section (F3); `datumNames` completed with `rim_tps`, `dem`, `prior_epoch` (F4); canvas resize moved out of `draw()` into `loadURL()`/`load()` so it no longer reallocates every freehand mousemove (F5). |
| T0.8 Regression tests | done | `tests/test_pipeline_regressions.py`: EXIF retention, stale dense-cache rejection by fingerprint, occlusion (background plane behind a ridge is excluded from a photo-mode polygon), symmetric low-side clipping, and a schema-validation-rejects-bad-polygon test. |

## Test results (final)

- `pytest -q -k "not e2e"`: 107 passed (102 pre-existing + 5 new in
  `tests/test_pipeline_regressions.py`), 0 failed.
- `pytest -q tests/test_e2e_synth.py`: 5 passed (~70–106 s) — confirms
  T0.1–T0.5 together don't regress the full photo/ortho pipeline end to end.
- `architecture.md` updated to match: dense-cache filename/fingerprint,
  `select_region` occlusion + `extra_views=1`, symmetric clip + `n_low_dropped`,
  typed request bodies, pointer-event tracing, slope-map figure, `datumNames`.

## Not done (out of scope for this pass)

- Tier 1+ (T1.x onward): not started at the time, per instructions. (Now
  addressed — see the Tier 1 section below.)
- T0.6's full scope per the plan also suggested typing `JobSnapshot` and
  `MeasureResult` (and running `pipeline.measure`'s return through
  `.model_dump()`). Skipped: the concrete bug (S1 — untyped request bodies
  causing a 500 on bad input) is fixed by the request-side models alone;
  typing the response would touch `measure()`'s return contract and
  `Job.snapshot()` for no behavior change, which is more than "small diff."

---

# Tier 1 implementation progress (geometry robustness)

Tracks `implementation_plan.md` Part C, steps 3–6 and 8 (T1.1, T1.2, T1.3,
T1.4, T1.5). Steps 2 (T3.3 preset harness), 7 (T2.1/T2.2) and 9+ (Tier 2/3)
are **not** part of this pass — per instructions, Tier 1 geometry robustness
only. That means every T1 acceptance number below is validated against the
single existing synthetic scene (`tools/synth.py`'s 21-view horizontal arc),
not the oblique/collinear/descending/sparse/low-texture presets T3.3 would
add — those don't exist yet.

## Validation against current repo (post-Tier-0)

Re-checked all five items against the code as it stood after Tier 0 (commit
`0f25372`) before implementing:

- **G3/T1.2**: confirmed `volume.select_region` (T0.3) already has the
  z-buffer occlusion mask and `extra_views=1`; it does not do ground-frame
  ray-casting, so T1.2 was still fully applicable as "the accurate photo-mode
  path," with T0.3's projection method becoming the fallback.
- **G4/T1.1**: confirmed `densify.estimate_up` was still exactly the
  camera-centre-plane method described in the plan (unchanged since before
  Tier 0).
- **G6/T1.3**: confirmed `densify.select_pairs` had only the baseline/depth
  ratio check (`0.10–1.5× median depth`), no convergence/ray-angle/shear
  checks, no `StereoConfig`.
- **G7/T1.4**: confirmed `dense_cloud` was a straight union of independent
  per-pair `stereo_pair` outputs (SOR/support/normal-filtered together at
  the end) — no cross-pair or cross-neighbour consistency requirement on any
  individual point.
- **G9/T1.5**: confirmed `scaling.aruco_scale` triangulated corners jointly
  by DLT with no per-view outlier check, used the plain mean of the four
  triangulated edge lengths, and had no independent (PnP) cross-check.

No plan-vs-code mismatches found; all five designs were implementable as
specified, with the simplifications noted per-item below.

## Status

| Item | Status | Notes |
| --- | --- | --- |
| T1.1 Scene-based up vector | done | `densify.estimate_up` now compares the sparse cloud's own dominant-plane normal (`_scene_plane_up`, via `volume.fit_plane_ransac`) against the old camera-centre-plane normal (`_camera_plane_up`). Scene plane wins outright when the camera path is collinear (2nd/1st singular value < 0.10) or the two agree within 20°; on genuine disagreement, a `surface_filter`-based "ground-like point count" vote between the two candidates decides. Falls back to the camera-plane estimate when the cloud has no clear dominant plane (`fit_plane_ransac` returns `None`). Kept the exact function signature (`estimate_up(views, sparse)`) plus an added optional `log` param, so every call site (`densify.dense_cloud`, `ortho.render_orthophoto`, `pipeline.measure`) needed only a one-line `log=log` addition. |
| T1.2 Unified ground-frame region selection | done | New `landslide/ground.py`: `build_dsm` rasters the metric cloud's highest point per cell; `cast_polygon_to_ground` densifies each polygon edge (≤25 px segments) and ray-marches a world-space camera ray per vertex to the first height crossing at-or-below the DSM surface. Design deviation from the plan's literal wording ("resampled with the inverse rectification map"): instead of un-rectifying a raster disparity map, points are lifted straight to world coordinates and the RAY intersection is computed analytically against the DSM — reuses `ImageView.project`'s convention nowhere directly, but the ray-cast math was verified to recover a known ground square from an oblique synthetic camera to ~1e-15 m (`tests/test_ground.py`). `ortho.select_region_ortho`'s core was factored out into `select_region_world(ctx, e1, e2, world_polygon, ...)`, shared by both the orthophoto path and `ground.select_region_ground`. `pipeline.measure`'s photo branch tries the ground-frame path first, falls back to the original `volume.select_region` when the ray-cast resolves <70% of the (densified) polygon vertices. **Found and fixed during implementation**: `select_region_ortho`/`select_region_world` and `ground.select_region_ground` all hard-coded `ctx.cloud(dense=True)`, which would silently index-mismatch against `pipeline.measure`'s own `pts = ctx.cloud(dense=dense)` fetch whenever a caller passed `dense=False` (a pre-existing latent bug in the ortho path since before this pass, now fixed by threading `dense` through both functions). |
| T1.3 Geometry-aware stereo pair selection | done | New `StereoConfig` dataclass centralizes every pair-geometry threshold and SGBM knob. `_pair_geometry_ok` adds convergence-angle (`[4°, 35°]`), ray-angle-at-scene-centroid (`[3°, 30°]`) and rectification-shear (`≤40°`, from `cv2.stereoRectify`'s `R1` rotation angle, computed without touching any pixel data) gates on top of the existing baseline/depth ratio. `select_pairs` keeps its greedy global pair list plus a baseline-only rescue pass for images left with zero pairs. This gate is shared with T1.4's per-reference-image neighbour picker (`_pair_geometry_ok` is the one place both consumers call). |
| T1.4 Multi-view depth-map fusion | done, one deliberate simplification | `_neighbors_for_view` picks up to `fusion_k` (4) geometry-gated covisible neighbours per reference image (relaxed rescue when none pass, same idea as T1.3's). `_depth_map_for_view` runs `stereo_pair` against each neighbour, re-projects each neighbour's resulting world points through the REFERENCE camera's own distortion model (`ImageView.project`) onto the reference's native pixel grid, and `_fuse_depth_candidates` takes a per-pixel consensus (mean of the largest cluster of candidates agreeing within `max(1%, 2×one-disparity-depth-step)`, requiring ≥2 agreeing candidates unless the reference has only one usable neighbour). `dense_cloud` now iterates every registered image as a reference instead of a fixed 30-pair global budget. **Deliberate simplification vs. the plan's literal design**: "re-project through the reference's own camera model" replaces "un-rectify the disparity map by inverting the rectification remap" — mathematically equivalent (both put a neighbour's depth estimate onto the reference's pixel grid) and reuses code (`ImageView.project`) already trusted by every other occlusion/selection path in this codebase, rather than hand-deriving an inverse of `cv2.initUndistortRectifyMap`'s maps. |
| T1.5 Scale hardening | done, partial per plan's own split | `scaling.aruco_scale`: (1) per-view reprojection outlier rejection — a view whose mean corner-reprojection residual exceeds 3× the median (only checked when ≥3 views, only drops if ≥2 remain) triggers a refit on the surviving views, logged and recorded as `dropped_views`; (2) `_fit_square_side` replaces the mean of four triangulated edge lengths with a similarity-Procrustes fit of a unit square onto the corners in their own best-fit plane — less sensitive to one noisy corner (each edge shares 2 of the 4 corners, the fit uses all four against a rigid template); (3) `_pnp_scale_estimate` gives a per-view `cv2.solvePnP`-based scale cross-check sharing no computation with the DLT path, reported as `pnp_scale_estimates`/`pnp_scale_spread` (warning above 10%). **Descoped** (per plan's own item 3, "UI adds 'add another reference'"): multiple markers / multiple manual references, weighted by inverse variance, `scale_info["references"]` list — this needs new API/UI surface (accepting more than one reference), not just geometry, so it's left for whoever picks up the UI-facing half of T1.5. |

## Test results (final)

- `pytest -q -k "not e2e"`: 117 passed (107 pre-existing + 10 new: 3 in
  `tests/test_up.py`, 3 in `tests/test_ground.py`, 4 in
  `tests/test_densify.py`), 0 failed.
- `pytest -q tests/test_e2e_synth.py`: 5 passed. On this run: ArUco scale
  1.9% off the camera-centre-Umeyama reference (unchanged order of magnitude
  from Tier 0); photo-mode volume error ~7.7% vs the 67.2 m³ truth (README
  previously reported ~14%); ortho-mode ~1.0% (previously ~4–6%). The
  `n_points > 5000` thresholds in `test_volume_end_to_end` /
  `test_volume_ortho_end_to_end` were lowered to `> 500`: T1.4's consensus
  fusion produces a smaller but cross-neighbour-confirmed cloud (~6.3k points
  vs ~11.7k measured for the T1.1–T1.3-only pipeline on the same cached
  reconstruction, itself down from an earlier ~208k measured before T1.3's
  geometry gates narrowed pair selection) — the volume accuracy improved
  even though the point count fell, so the count assertion was the stale
  part, not the algorithm.
  Confirmed the fusion_k default isn't starving the cloud: reran with
  `fusion_k=6` and `8` on the cached reconstruction and got the same final
  point count (±0.03%) as `fusion_k=4` — the geometry gate, not `fusion_k`,
  is what bounds each reference image's usable neighbour count on this
  scene, so 4 (mid of the plan's "3–4") was kept as the default.
- `architecture.md` updated to match: `StereoConfig`, geometry-gated
  neighbour selection shared by `select_pairs`/`_neighbors_for_view`,
  multi-view depth-consensus fusion, scene-based `estimate_up`, ground-frame
  photo-mode selection with fallback, `ortho.select_region_world`, ArUco
  per-view rejection/squareness/PnP cross-check, `region_method` result key.
- `README.md` benchmark numbers updated to the re-measured photo/ortho
  tracing gap; the single-pair 1280px/640px stereo-width numbers are flagged
  as not yet re-benchmarked under the new multi-view fusion path (footnote).

## Not done (out of scope for this pass)

- T3.3 (preset synthetic harness: `oblique60`, `descending`, `collinear`,
  `nadir`, `sparse8`, `lowtex`, `distorted`) and `tools/benchmark.py`: not
  built. Every Tier 1 acceptance number above is single-scene (`arc`); the
  plan's own execution order lists T3.3 as a prerequisite for T1.1–T1.4, and
  it wasn't run first. This is the highest-value next step to actually prove
  "forgiving of bad camera angle" rather than "improves the one arc scene."
- T1.5's multi-reference workflow (`scale_info["references"]`, UI "add
  another reference"): descoped, see the status table above.
- Tier 2 (T2.1–T2.5) and Tier 3 (T3.1, T3.2, T3.4): not started.
- `n_consistent` (cross-neighbour agreement count) is computed internally by
  `_fuse_depth_candidates` during T1.4 fusion but not persisted on the fused
  cloud (`ctx.dense` is still `{"points", "colors"}` — adding a weights array
  would also mean updating the `.npz` cache read/write, which is T2.1's job
  per the plan ("weights n_consistent from T1.4") and out of scope here.

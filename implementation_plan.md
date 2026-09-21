# SlopeLens — Implementation Plan

Companion to `architecture.md`. Part A lists verified defects and weak points with the
exact code location and the scenario that triggers them. Part B is the upgrade roadmap:
a tiered overhaul aimed at (1) markedly better volume accuracy and (2) tolerance of poor
camera geometry (oblique views, uneven paths, few photos, low texture). Part C gives the
execution order, effort, and acceptance criteria.

Constraints that shaped the recommendations: CPU only (Intel iGPU, no CUDA; pycolmap's
`patch_match_stereo` binding hard-fails), 12 cores, 23 GB RAM, Python 3.14 (PyTorch wheels
for 3.14 are not guaranteed — anything torch-based is an optional extra), pycolmap 4.1.1,
OpenCV 5.0.

---

## Part A — Technical debt, bottlenecks, points of failure

Severity: **H** = produces a wrong number silently or blocks a real use-case;
**M** = degrades accuracy/robustness; **L** = maintainability / UX.

### A.1 Geometry and accuracy

| # | Sev | Location | Problem | Failure scenario |
| --- | --- | --- | --- | --- |
| G1 | H | `landslide/pipeline.py:130` | `im.save(..., "JPEG", quality=…)` re-encodes without `exif=`, so **all EXIF (focal length, sensor, orientation, GPS) is stripped** before COLMAP sees the photo. COLMAP then initialises every camera with the default focal prior (1.2 × max dimension). | Wide or tele phone lenses (0.5×/3×), mixed zoom, or oblique sets: incremental mapping starts from a focal that is 30–60 % off; more attempts fail the gate, per-image intrinsics drift, scale-bearing geometry (marker triangulation) inherits the bias. |
| G2 | H | `landslide/pipeline.py:295` → `sfm.reconstruct(reuse=False)`; `landslide/densify.py:281-285` | The CLI re-runs SfM into a **new, arbitrary model frame** on every run, but `dense_cloud` reloads `work/dense.npz` from the previous frame whenever it exists. Cache is keyed only on `stereo_width`, not on the reconstruction. | Second `landslide.cli run spec.json` with the same `out_dir`: dense cloud is in the old frame, polygon selection returns garbage, volume is wrong with no warning. Same hazard whenever the retry ladder changes the winning attempt on a reload. |
| G3 | H | `landslide/volume.py:498` (`u, _ = v.project(pts)`) | Photo-mode region selection discards the depth returned by `project`; `cv2.projectPoints` maps points **behind the camera** to mirrored pixel positions, and nothing tests occlusion. `extra_views` carving exists (`:476`) but `pipeline.measure` never passes it. | Oblique photo of a slope with terrain behind the polygon: far background points project inside the polygon and are integrated as if they were the debris surface (README already measures 14 % vs 6 % between photo and ortho tracing — this is the mechanism). |
| G4 | H | `landslide/densify.py:205-220` (`estimate_up`) | "Up" = normal of the plane through camera centres. Degenerate for near-collinear camera paths (SVD third axis is arbitrary in the plane orthogonal to the path); wrong for paths that climb or descend a slope, for shots taken from a road above a pit, and for nadir/drone-like sets where cameras form a plane that *is* roughly horizontal but the sign test can flip. | Everything downstream inherits it: orthophoto axes, `surface_filter` (drops real ground), rim steepness filter, datum normal orientation, cut/fill sign, gravity seed for ICP. A sweep walking downhill along a road gives an "up" tilted by the road grade. |
| G5 | M | `landslide/densify.py:93-94` | `Ka[0,0] *= w/(2·cx)` rescales the focal length by the ratio of the cropped width to twice the principal point. This is not a valid intrinsics update after a top-left crop (only the size changes; `cx, cy` stay valid) and silently alters `f` when COLMAP estimated an off-centre principal point. | Off-centre principal point (common after per-image refinement): depth bias per pair, mismatched scale between pairs, thicker fused surface. |
| G6 | M | `landslide/densify.py:98-100, 124-130` | Rectification `alpha=0` with no convergence-angle test in `select_pairs`; `numDisparities` clipped to 320. Only the baseline/depth ratio is checked. | Strongly convergent pairs (cameras pointing at the same spot from 40°+ apart, typical of an arc around a small slide) rectify into heavily sheared images; SGBM quality collapses. Close-range pairs exceed 320 px disparity and lose the near field. |
| G7 | M | `landslide/densify.py:316-349` | Fusion is a union of per-pair point sets followed by SOR, sparse-support clip and normal filter. No **multi-view geometric consistency** (a point is never required to be seen by ≥2 pairs at a consistent depth). | Systematic per-pair artefacts (repetitive texture, specular wet mud, sky/edge fattening) survive as sheets a few cm off the surface and enter the TIN. |
| G8 | M | `landslide/volume.py:662-674` | Outlier cap is one-sided: only `h > max(1.5 m, 8σ)` above the datum is removed. | Stereo floaters *below* the surface (common on dark, low-texture ground) inflate `cut` and `max_depth_m`. |
| G9 | M | `landslide/scaling.py:118-131` | Corners of the winning marker are triangulated by a single joint DLT across every view that detected it. No per-view reprojection outlier rejection, no coplanarity/squareness constraint, only one marker supported. | A single false-positive or mis-ID'd detection (motion blur, partial occlusion, `auto` dictionary picking a spurious id) shifts all four corners; `side_spread_rel` only warns (>5 %). |
| G10 | M | `landslide/pipeline.py:155-156`, `volume.py:475` | Photo-mode rim band defaults to 12 px starting 6 px outside the line on photos stored at up to 3000 px. | On a 3000 px photo of a 20 m slope one pixel ≈ 1–3 cm; the "undisturbed ground" sample is a 12–36 cm ribbon whose points are mostly the edge of the debris. |
| G11 | M | `landslide/densify.py:297-298` | Voxel size = sparse extent / 900 regardless of ground sample distance or metric scale. | Large scenes with a small slide: the slide is represented by a few hundred voxels; small scenes waste points. |
| G12 | L | `landslide/volume.py:234` | TPS builds a dense `n×n` distance matrix (n ≤ 4000): ~130 MB for `D2` plus `K`, `A` (n+3)², ≈ 400 MB peak, `np.linalg.solve` O(n³). | Acceptable today, but blocks raising `max_pts` and runs on every measurement with a curved rim. |
| G13 | L | `landslide/volume.py:589, 604, 609` (`datum_pts is rim`) | Identity comparisons to decide whether the datum came from the rim; `rim` is rebound at `:572`, keeping it consistent only by care. | Fragile under refactor; any copy breaks the branch silently. |

**Tier 1 resolution status** (see `IMPLEMENTATION_PROGRESS.md` for detail):
G3 fixed by T1.2 (ground-frame ray-cast selection, `landslide/ground.py`, with the old
projection kept as a documented fallback). G4 fixed by T1.1 (`densify.estimate_up` now scene-
plane-first). G6 fixed by T1.3 (`_pair_geometry_ok` convergence/ray-angle/shear gates).
G7 fixed by T1.4 (`_depth_map_for_view`/`_fuse_depth_candidates`: cross-neighbour depth
consensus, ≥2-of-k agreement required). G9 fixed by T1.5 (per-view outlier rejection,
squareness Procrustes fit, PnP cross-check in `scaling.aruco_scale`). G10 (rim-band pixel
width) is unaffected in the image-plane fallback path but no longer the primary photo-mode
mechanism now that T1.2's ground-frame selection uses a metric rim band by default. G13 not
touched (Tier 2 scope, `datum_source` refactor is T2.3).

### A.2 Robustness to camera geometry (why "bad angle" hurts today)

* **Pose recovery** (`sfm.py`): SIFT + exhaustive/sequential matching is fine for the
  canonical sweep. It struggles with large viewpoint changes (>30° between neighbours),
  low texture, and repeated structures — exactly the "bad angle" cases. The ladder retries
  parameters but never changes the feature type.
* **Depth** (`densify.py`): rectified two-view SGBM assumes fronto-parallel-ish surfaces
  and modest convergence; grazing views of a slope foreshorten texture and break the
  block matcher. No plane-sweep / multi-baseline aggregation.
* **Frame orientation** (`estimate_up`): tied to the camera path, not to the scene (G4).
* **Region selection** (`select_region`): perspective + no occlusion (G3). Ortho mode
  avoids it but requires the dense cloud first and still depends on `up`.
* **Validation**: the only end-to-end test uses an ideal 21-view horizontal arc at
  constant height, so none of the above is measured.

### A.3 Server and platform

| # | Sev | Location | Problem |
| --- | --- | --- | --- |
| S1 | M | `server/main.py:408, 424, 496` | Request bodies are raw `dict`; no schema, no type coercion, no field validation (a string polygon crashes inside numpy with a 500). |
| S2 | M | `server/main.py:75, 322` | pycolmap runs in threads of the web process. A native crash (bad image, OOM in COLMAP) kills the whole server and every in-flight job. |
| S3 | M | `server/main.py:346, 359, 497, 584` | `JOBS.get` and `job.status` reads/writes without `JOBS_LOCK`/`job.lock` in several handlers; `set_status` writes state outside the lock. Benign today because Python dict ops are atomic, but the busy checks (`:500`, `:589`) are check-then-act races between two requests. |
| S4 | L | `server/main.py:353-354` | `GET /api/jobs/{id}` performs the (multi-second) COLMAP reload synchronously inside the request. |
| S5 | L | `app.js:132-159` | 1.2 s polling; no SSE/WebSocket. Log tail is re-sent on every poll. |
| S6 | L | `server/main.py:519-527` | `rim_inner_px` from the request is ignored (`measure` accepts it). |
| S7 | L | repo | No `CLAUDE.md`/contributor doc; `README.md` is the only spec; no typed result model shared between server, CLI and UI. |

### A.4 Frontend

| # | Sev | Location | Problem |
| --- | --- | --- | --- |
| F1 | H | `app.js:572, 631-670` | Mouse events only (`click`, `mousedown`, `mousemove`, `mouseup`). No pointer/touch events → tracing is impossible on a phone or tablet, the device the photos come from. |
| F2 | M | `app.js:40-85` | No zoom/pan; a 3000 px photo is traced at 940 px display width; no vertex drag/insert/delete after placement. |
| F3 | M | `app.js:515-517` | `slopemap.png` is generated (`pipeline.py:255`) but never shown. |
| F4 | L | `app.js:474-478` | `datumNames` lacks `rim_tps`, `dem`, `prior_epoch`; falls back to the raw key. |
| F5 | L | `app.js:50-51` | `canvas.draw` reassigns `canvas.width/height` on every redraw (full reset + reallocation per mouse move in freehand mode). |
| F6 | L | `app.js` | 717-line single file, global `state`, string-built HTML, no build step or tests. |

### A.5 Testing gaps

* One synthetic scene, one camera path. No oblique, descending, collinear, nadir, sparse
  (5–8 photo) or low-texture scenes; no real-photo regression set.
* No test for stale dense cache (G2), EXIF retention (G1), depth sign in selection (G3),
  `estimate_up` degeneracy (G4).
* API has no tests (no `TestClient`).

---

## Part B — Upgrade roadmap

Each item lists: goal → design → files → reuse → test → acceptance. Items are grouped in
tiers; within a tier they are independent unless noted.

### Tier 0 — Correctness fixes (1–2 days total, small diffs, no new dependencies)

**T0.1 Keep EXIF focal prior (G1).**
Pass `exif=im.getexif().tobytes()` (after `exif_transpose`, which already rewrites the
orientation tag) in `import_photos`, or — more robust — read `FocalLength`,
`FocalLengthIn35mmFilm` and sensor model with Pillow and write the focal prior into the
COLMAP database (`pycolmap.Database` → `camera.params[0]`, `has_prior_focal_length=True`)
after `import_images`. Files: `pipeline.py`, `sfm.py::_run_attempt`.
Test: import a JPEG with known EXIF, assert the COLMAP camera's prior focal ≈ EXIF-derived
focal. Acceptance: on the synthetic set (no EXIF) behaviour unchanged; on real phone sets
attempt 1 succeeds more often (log `[sfm] reconstruction done (default)`).

**T0.2 Dense cache keyed to the reconstruction (G2).**
Compute a fingerprint of the sparse model (e.g. SHA-1 of sorted `image_id → cam_from_world`
plus `points3D` count) in `build_ctx`; name the cache `dense_<width>_<fp8>.npz` and store
the fingerprint inside the npz; ignore mismatching caches. Also make `run_spec` pass
`reuse=True`. Files: `sfm.py`, `densify.py`, `pipeline.py`. Test: build ctx, save cache,
mutate a pose, assert cache is rejected.

**T0.3 Depth and occlusion in photo-mode selection (G3).**
In `select_region`: require `depth > 0`; rasterise a z-buffer of the cloud into the marked
view at ~1/4 resolution (nearest depth per cell) and keep only points within a depth
tolerance (`max(3·voxel, 2 % of median depth)`) of the front surface; enable
`extra_views=1` by default from `measure`. Files: `volume.py`, `pipeline.py`. Reuse:
`ImageView.project`. Test: synthetic cloud with a plane behind a hill; polygon over the
hill must not select the plane. (Superseded by T1.2 for accuracy but cheap and immediately
protective.)

**T0.4 Symmetric outlier clipping (G8).**
Clip `h < −max(1.5 m, 8σ)` as well; report `n_low_dropped`. `volume.py`.

**T0.5 Remove the focal hack (G5).**
Delete `densify.py:93-94`; crop only changes `(w, h)`. Verify the synthetic e2e volume does
not regress (expect equal or better).

**T0.6 Typed API models (S1).**
Add `server/schemas.py` with Pydantic models (FastAPI already depends on Pydantic):
`ArucoScaleRequest`, `ManualScaleRequest`, `MeasureRequest` (polygon as `list[tuple[float,float]]`,
`mode: Literal["photo","ortho"]`, `rim_px: float = 12`, `rim_inner_px: float | None`),
`JobSnapshot`, `MeasureResult`. Use the same `MeasureResult` to type `pipeline.measure`'s
return (via `.model_dump()`). Fixes S6 as a side effect.

**T0.7 UI quick wins (F1, F3, F4, F5).**
Switch canvas handlers to Pointer Events (`pointerdown/move/up`, `touch-action: none`);
add the slope map figure to the result section; complete `datumNames`; only resize the
canvas in `loadURL`, not on every draw. `app.js`, `index.html`, `style.css`.

**T0.8 Regression tests for the above** in `tests/test_pipeline_regressions.py`.

### Tier 1 — Geometry robustness ("forgiving of bad camera angle")

**T1.1 Scene-based up vector (G4).**
Design: estimate `up` from the *scene*, not the camera path.
1. RANSAC dominant plane on the sparse cloud (reuse `volume.fit_plane_ransac`, it is
   already deterministic and bounded) → candidate normal `n_ground`.
2. Sign: cameras must be above ground (`mean(centres − centroid)·n > 0`).
3. Consistency check against the camera-plane estimate; if they agree within 20° use
   `n_ground`; if the cameras are collinear (second singular value < 10 % of first) use
   `n_ground` unconditionally; else pick the candidate under which more cloud normals are
   "ground-like" (`surface_filter` statistics).
4. Persist `up` and an `up_confidence` in `scale_info`/`ortho` meta so every consumer
   (`ortho`, `surface_filter`, rim filter, ICP seed) uses the same vector.
Files: `densify.py` (`estimate_up`), `ortho.py`, `dem.py`, `pipeline.py`.
Test: synthetic presets with collinear and descending camera paths (see T3.3): `up` within
5° of truth. Acceptance: ortho and cut/fill sign correct on all presets.

**T1.2 Unified ground-frame region selection (G3, G10).**
Design: make photo tracing as accurate as ortho tracing by converting the photo polygon to
ground coordinates instead of selecting in the image.
1. Build a ground DSM once per measurement (raster of the cloud in the `(e1, e2, up)`
   frame — the same raster T2.1 uses; cell = 2–3 × point spacing).
2. For each polygon vertex, cast the camera ray through the DSM (march along the ray,
   first cell where ray height ≤ DSM height, bilinear refine). Vertices whose ray misses
   the DSM are dropped with a warning.
3. Densify the polygon along edges (project intermediate image points too) so curved
   perspective edges map correctly.
4. Hand the ground polygon to `select_region_ortho` — rim band in metres, no parallax,
   no occlusion problem.
Keep the old projection path as a fallback when the ray-cast fails for >30 % of vertices.
Files: new `landslide/ground.py` (DSM + ray-cast), `volume.py`, `pipeline.py`. Reuse:
`ortho.ground_basis`, `ortho.select_region_ortho`, `geometry.ring_distance`.
Test: synthetic scene, GT circle projected into an oblique view (60° off nadir) → ground
polygon within 1 cell of the GT circle; volume error in photo mode ≈ ortho mode.

**T1.3 Geometry-aware stereo pair selection (G6).**
Add to `select_pairs`: convergence angle between optical axes in `[4°, 35°]`, ray-angle
at the scene centroid in `[3°, 30°]`, rectification shear check (`|R1 − I|` Frobenius
below a threshold); when an image ends up with 0 pairs, relax to `per_image=3` for its
best neighbours. Expose the thresholds as a `StereoConfig` dataclass (one place for all
SGBM knobs). Test: synthetic arc with 21 views — same pairs as today; a 5-photo set with
strong convergence — no pair with >35°.

**T1.4 Multi-view depth-map fusion (G7) — the main accuracy lever.**
Design (COLMAP `stereo_fusion` logic on CPU, without PatchMatch):
1. For each reference image `i`, choose `k = 3–4` neighbours (T1.3 rules).
2. For each `(i, j)`: rectify, SGBM both directions (existing code), then **un-rectify**
   the disparity into a depth map in the *reference camera* (`depth_ref = z_rect` mapped
   through `R1ᵀ`, resampled with the inverse rectification map). Result: `k` depth maps
   and confidence (L/R consistency, uniqueness margin) per reference pixel.
3. Per-pixel fusion: median of depths that agree within `max(1 %, 2 × disparity-step
   depth)`; require ≥2 agreeing neighbours (≥1 when only one pair exists but flag as
   low-confidence). Record `n_consistent` as a per-point weight.
4. Lift fused depth maps to world points; downsample per image; concatenate; run the
   existing SOR/support/normal filters (reuse `voxel_downsample`, `sor_mask`,
   `surface_filter`). Store per-point `n_consistent` alongside colours for the LoD step
   (T2.1).
5. Tie voxel size to ground sample distance: `voxel = median depth / f × 2` (≈ 2 px), not
   `extent / 900` (fixes G11).
Cost: `k×` SGBM runs versus today's ≤2 per image — bounded by the same 1280 px working
size; expected 2–3× dense-stage time (currently 1–3 min), parallelisable with a
`ThreadPoolExecutor` since OpenCV releases the GIL.
Files: `densify.py` (new `depth_map_for_view`, `fuse_depth_maps`, keep `stereo_pair` for
tests), `StereoConfig`. Test: synthetic bowl — fused cloud RMS distance to the GT surface
drops (target: <2 cm at 1280 px vs today's cm-level noise); volume error ≤ 8 % in photo
mode after T1.2. Acceptance: no regression in memory (`MAX_FUSED_POINTS` still enforced).

**T1.5 Scale hardening (G9).**
1. Per-view robust check: after joint DLT, reproject corners into each detecting view and
   drop views with mean residual > 3 × median; refit. RANSAC over view subsets when ≥4 views.
2. Per-view PnP cross-check: `cv2.solvePnP` on the 4 corners with the marker's metric
   geometry gives a per-view marker distance; the ratio to the triangulated model distance
   is an independent scale estimate; report spread.
3. Multiple markers / multiple manual segments: `scale_info["references"]` list, final
   scale = inverse-variance weighted mean, `scale_rel_error` from the spread.
4. Enforce squareness: fit a rigid square to the four triangulated corners (Procrustes) and
   use its side, not the mean of four noisy sides.
Files: `scaling.py`; UI adds "add another reference". Test: inject one wrong detection into
a synthetic set → scale unchanged within 1 %.

### Tier 2 — Accuracy core

**T2.1 Raster DSM cut/fill (replaces TIN as the primary integrator).**
Design: bin interior points (in the datum `(u,v)` frame) to cells of `2–3 × spacing`;
per-cell height = weighted median (weights `n_consistent` from T1.4), per-cell
`σ_cell` = MAD; fill holes ≤ 2 cells by bilinear interpolation, leave larger holes empty
(explicit `unmeasured_area_m2`); `cut/fill = Σ cell_area × h_cell`. Keep `prism_volume`'s
TIN as a cross-check reported as `volume_tin_m3`; warn when they differ by >10 %.
Feed `σ_cell` straight into the LoD field (reuse `_lod_per_triangle` grid logic) — a
cleaner noise model than k-NN roughness. Files: `volume.py` (new `raster_volume`),
`pipeline.py`. Test: analytic bowl with injected noise and 5 % outliers → raster error
< TIN error.

**T2.2 Volume uncertainty by bootstrap.**
Replace `σ_datum × area + 2·scale_rel·|net|` with a bootstrap: resample rim points (B=50),
refit the datum ladder, recompute the raster volume; report the 2.5/97.5 percentiles plus
scale term. Cost is dominated by the datum fit (fast for plane/quad; skip TPS in the
bootstrap and use its fixed fit). `volume.py`.

**T2.3 Datum ladder hygiene (G12, G13).**
Replace `datum_pts is rim` with an explicit `datum_source` enum; factor the ladder into a
`fit_datum(rim, up) → Datum` object with `.height(uv)` and `.sigma`; TPS via
`scipy.interpolate.RBFInterpolator(kernel="thin_plate_spline", smoothing=…, neighbors=…)`
which supports local neighbourhoods (no dense `n×n`). `volume.py`.

**T2.4 SfM improvements within pycolmap 4.1.**
* Use `SiftExtractionOptions.estimate_affine_shape` + `domain_size_pooling` for the
  low-contrast attempt (better under viewpoint change).
* Enable `guided_matching` in `FeatureMatchingOptions` for all attempts.
* Sequential matcher: turn `loop_detection` on for sets ≥30 photos (closed sweeps).
* `IncrementalPipelineOptions`: `ba_refine_principal_point=False` for phone sets,
  `min_num_matches=15`, `init_num_trials` up.
* Optional extra (`pip install slopelens[learned]`): ALIKED/DISK + LightGlue via
  `kornia` writing keypoints/matches into the COLMAP DB (`pycolmap.Database`), used as a
  ladder attempt before "enhanced low-contrast". Gate on `importlib.util.find_spec("torch")`
  and document that Python 3.14 wheels may lag. Files: `sfm.py`, new `sfm_learned.py`.

**T2.5 Optional dense backend interface.**
Define `DenseBackend` protocol (`build(ctx, cfg) → {"points","colors","weights"}`);
implementations: `SGBMFusionBackend` (T1.4, default) and `OpenMVSBackend` (calls
`DensifyPointCloud` binary if present, via COLMAP → MVS export). Only worth doing if T1.4
does not reach the accuracy target. `densify.py`.

### Tier 3 — Platform, UI, validation

**T3.1 Process isolation and progress streaming (S2–S5).**
Run SfM and dense stages in a `ProcessPoolExecutor(max_workers=2)` worker with the job
directory as the only shared state (log lines via a `multiprocessing.Queue` appended to
`Job.log`); the parent survives a native crash and marks the job `error`. Replace polling
with `GET /api/jobs/{id}/events` (SSE, `StreamingResponse`); keep the snapshot endpoint
for resume. Take `job.lock` around every status transition; make `ensure_ctx` return
`202` while reloading instead of blocking. Files: `server/main.py` → split into
`server/jobs.py`, `server/routes.py`, `server/schemas.py`. Tests: FastAPI `TestClient`
covering each route with a tiny fixture job.

**T3.2 Frontend modernisation (F1–F6).**
Split `app.js` into ES modules (`api.js`, `canvas.js`, `state.js`, `steps/*.js`) served
statically (no bundler). Canvas: pointer events, wheel/pinch zoom, drag pan, vertex
drag/insert/delete, magnifier for manual-scale clicks, keyboard shortcuts. Show all
artifacts (overlay, heightmap, slopemap) and the uncertainty band. Add a minimal
Playwright or `node:test` + jsdom check for the coordinate chain.

**T3.3 Validation harness — synthetic camera-path presets.**
Extend `tools/synth.py` with `--preset`: `arc` (current), `oblique60` (cameras 60° off
nadir, low height), `descending` (path drops 6 m along the sweep), `collinear` (straight
walk parallel to the slope), `nadir` (drone-like), `sparse8` (8 photos, 45 % overlap),
`lowtex` (texture amplitude ÷4), `distorted` (k1 = −0.15 radial). Add
`tools/benchmark.py` running SfM → scale → dense → measure per preset and emitting a
Markdown table (registered views, scale error, cloud RMS to GT surface, volume error photo
vs ortho, runtime, peak RSS). Pin regression thresholds per preset in
`tests/test_e2e_presets.py` (marked slow). This harness is what proves "forgiving of bad
camera angle"; build it right after Tier 0 so every Tier 1/2 step has a number.

**T3.4 Real-photo regression set.**
A `data/real/` folder (git-ignored, downloadable) with 2–3 field sets and a reference volume
(measured on-site or from a drone survey) to keep the synthetic numbers honest.

---

## Part C — Execution order, effort, acceptance

| Order | Item | Effort | Depends on | Acceptance metric |
| --- | --- | --- | --- | --- |
| 1 | T0.1–T0.8 | 1–2 d | — | all existing tests pass; new regression tests pass; UI usable on touch |
| 2 | T3.3 harness | 1–2 d | T0 | **not done** — only the existing single `arc` preset was used to validate Tier 1; no `oblique60`/`collinear`/`descending`/`sparse8`/etc. presets exist yet, so those per-preset acceptance numbers below are unverified |
| 3 | T1.1 up vector | 1 d | T3.3 | **done** — verified on synthetic collinear/arc cases (`tests/test_up.py`), not on the full preset set (no harness) |
| 4 | T1.2 ground-frame selection | 2 d | T1.1 | **done** — photo-mode e2e error 7–8% vs ortho's ~1% on the `arc` scene (within target); ray-cast geometry unit-verified exactly (`tests/test_ground.py`); `oblique60` untested |
| 5 | T1.3 pair selection + `StereoConfig` | 0.5 d | — | **done** — gate unit-tested (`tests/test_densify.py`); `sparse8`/`oblique60` untested (no harness) |
| 6 | T1.4 multi-view fusion | 3–4 d | T1.3 | **done**, target partially met — volume error ≤8% on `arc` (photo 7–8%, ortho ~1%, both under target); dense-stage time ~1.4× (well under the 3× budget); cloud-RMS-to-GT not directly measured (no per-point GT distance check in the harness); consensus fusion trades raw point count for confidence (~6.3k vs ~11.7k pre-T1.4 points on this scene) |
| 7 | T2.1 raster volume + T2.2 bootstrap | 2 d | T1.4 | not started (Tier 2) |
| 8 | T1.5 scale hardening | 1–2 d | — | **done**, partially — per-view outlier rejection, squareness fit and PnP cross-check implemented and unit-tested; multi-reference/multi-marker support (`scale_info["references"]`, UI "add another reference") explicitly out of scope for this pass (new API/UI surface, not just geometry) |
| 9 | T2.3 datum refactor | 1 d | T2.1 | identical volumes on `tests/test_volume.py` fixtures; TPS peak RAM < 100 MB |
| 10 | T2.4 SfM options (+ optional learned features) | 1–3 d | T3.3 | `sparse8`, `lowtex` register ≥ 90 % of views |
| 11 | T3.1 process isolation + SSE | 2 d | T0.6 | server survives a killed worker; API tests green |
| 12 | T3.2 frontend modules | 2–3 d | T0.7 | coordinate-chain test; vertex editing |
| 13 | T2.5 OpenMVS backend | 2 d | T1.4 | only if step 6 misses its target |
| 14 | T3.4 real-photo set | ongoing | — | documented reference volumes |

Total for the accuracy/robustness core (steps 1–8): roughly 12–16 working days.

### Target end state

* Volume error on the synthetic benchmark: ≤ 5 % (ortho/ground-frame tracing) and ≤ 8 %
  (photo tracing) at 1280 px, with a reported 95 % interval that contains the truth.
* Correct sign, orientation and orthophoto on descending, collinear and oblique camera
  paths; explicit warnings (not wrong numbers) when geometry is genuinely insufficient.
* No silent stale-cache or behind-camera selection failures.
* Typed API, crash-isolated workers, touch-capable tracing.

### Non-goals (deliberately out of scope)

* GPU MVS (no CUDA on the target hardware).
* Replacing COLMAP: pycolmap remains the SfM core; the changes are around it.
* Full 3D (overhang-capable) volumes: the 2.5D height-field model stays; it matches the
  cut/fill definition practitioners use.

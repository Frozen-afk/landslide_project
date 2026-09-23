# SlopeLens — Architecture

Landslide volume from overlapping smartphone photos. This document describes the system
as it exists in the repository today (verified against source; line numbers refer to the
current tree). For the upgrade roadmap see `implementation_plan.md`.

---

## 1. High-level architecture

```
 ┌──────────────────────────┐        ┌──────────────────────────────┐
 │  Browser (vanilla JS)    │  HTTP  │  FastAPI server              │
 │  server/static/app.js    │◀──────▶│  server/main.py              │
 │  index.html / capture.html│ JSON  │  Job registry, persistence,  │
 │  <canvas> click tracing  │        │  ThreadPoolExecutor(2)       │
 └──────────────────────────┘        └──────────────┬───────────────┘
                                                    │ direct Python calls
                                                    ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  landslide/  (core library, no web dependencies)                         │
 │                                                                          │
 │  pipeline.py ── orchestration: import_photos · measure · run_spec        │
 │      │                                                                   │
 │      ├─▶ sfm.py       COLMAP SfM via pycolmap, retry ladder, ReconCtx    │
 │      ├─▶ scaling.py   ArUco / manual metric scale                        │
 │      ├─▶ densify.py   multi-view SGBM depth fusion → semi-dense cloud   │
 │      ├─▶ ground.py    ray-cast photo polygon → ground-frame selection   │
 │      ├─▶ ortho.py     top-down orthophoto + ground-coordinate selection  │
 │      ├─▶ volume.py    rim datum (plane/quad/TPS) + 2.5D prism integral   │
 │      ├─▶ dem.py       prior-DEM import, trimmed point-to-plane ICP       │
 │      ├─▶ change.py    two-epoch change volume                            │
 │      ├─▶ geo.py       EXIF GPS → ENU annotation                          │
 │      ├─▶ segment.py   Roboflow SegFormer auto-detect (optional)          │
 │      ├─▶ enhance.py   CLAHE + unsharp copies for low-contrast SfM retry  │
 │      ├─▶ viz.py       overlay / heightmap / slope-hazard artifacts       │
 │      └─▶ geometry.py  pure math helpers (DLT, polygon tests)             │
 │  cli.py · mkmarker.py                                                    │
 └──────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼
 data/jobs/<job_id>/
   photos/           EXIF-normalised JPEGs (000_name.jpg …), gps.json
   work/             database.db, sparse/0/, dense_<w>_<fingerprint>.npz, enhanced/
   artifacts/        ortho.jpg, ortho.json, overlay.jpg, heightmap.png, slopemap.png, pointcloud.ply
   state.json        status, error, log tail, scale_info, result, ortho meta, dem_info
   dem.xyz           uploaded prior DEM (optional)
```

Everything runs in one Python process. Heavy stages (pycolmap, OpenCV SGBM) release the
GIL in C++, so two jobs run concurrently on the `ThreadPoolExecutor(max_workers=2)`
(`server/main.py:75`). The browser polls `GET /api/jobs/{id}` every 1.2 s for status.

### 1.1 End-to-end data flow

| Step | Trigger | Code | Output |
| --- | --- | --- | --- |
| 1. Upload | `POST /api/jobs` | `pipeline.import_photos` | `photos/*.jpg`, `gps.json` |
| 2. Reconstruct | auto after upload (executor) | `sfm.reconstruct` | `work/database.db`, `work/sparse/`, `ReconCtx` |
| 3. Scale | `POST …/scale/aruco` or `…/scale/manual` | `scaling.aruco_scale` / `manual_scale` | `ctx.scale`, `scale_info` |
| 4a. Ortho (optional) | `POST …/ortho` | `densify.dense_cloud` → `ortho.render_orthophoto` | `dense_<w>_<fp>.npz`, `ortho.jpg`, `ortho.json` |
| 4b. Prior DEM (optional) | `POST …/dem` | `dem.load_dem` → `dem.align_to_dem` | `dem.xyz`, `dem_info` (R, t, rms) |
| 5. Mark | browser canvas | `app.js` polygon state | polygon in stored-photo px or ortho px |
| 6. Measure | `POST …/measure` | `pipeline.measure` → `volume.prism_volume` or `volume.dem_volume` | result dict, artifacts |
| 7. Change (CLI only) | `landslide.cli change A B` | `change.change_volume` | change result JSON |

### 1.2 Job state machine (`server/jobs.py`, `server/routes.py`)

```
 created ──▶ reconstructing ──▶ ready ◀──▶ measuring
                 │                │  ▲
                 │                │  └──▶ orthorectifying
                 ▼                ▼
               error            error (measure/ortho failures return to `ready`
                                       with `job.error` set)
```

Persistence: every status change writes `state.json` atomically (`Job.save_state`,
`server/jobs.py`). On startup `_load_persisted_jobs` reattaches job directories;
jobs interrupted mid-SfM become `error`, finished models become `ready`. The `ReconCtx`
is rebuilt lazily from the COLMAP cache on first touch (`Job.ensure_ctx`); `GET
/api/jobs/{id}` never blocks on this reload — it kicks off `Job.start_ctx_reload()` in a
background thread (idempotent, guarded so a second poll doesn't start a second reload)
and returns immediately with `ctx_loading: true/false` in the snapshot (T3.1/S4).

**Process isolation (T3.1/S2, `server/worker.py` + `server/executor.py`).** The three
heavy/crash-prone stages — reconstruction, dense+measure, dense+ortho — run in a shared
`ProcessPoolExecutor(max_workers=2)` (`server/executor.py`), not the request/event-loop
process. Each worker function is a plain top-level, picklable call (`job_id`, paths, and
plain dicts for `scale_info`/`dem_info`/the measure spec — never the live `Job` or
`ReconCtx`, which aren't picklable); it reloads its own `ReconCtx` from the on-disk COLMAP
cache (`reconstruct(reuse=True)`) and re-applies scale/DEM before doing its work — the job
directory is the only shared state, per the plan's own framing. Workers push log lines to
a `multiprocessing.Manager().Queue()`; a background thread in the parent drains it into
the right `Job.log`. If a worker process dies outright (segfault, `os._exit`), that job's
future raises/`BrokenProcessPool`; the executor is recreated (lock-guarded) so the pool
recovers for the next submission, and the dead job is marked `error` instead of losing the
whole server. `tests/test_server.py::test_worker_crash_is_isolated_and_pool_recovers`
drives this with a worker that calls `os._exit(1)` for real — not mocked.

**Progress streaming (T3.1/S5).** `GET /api/jobs/{id}/events` (SSE, `text/event-stream`)
tails `Job.log` as it grows and closes once status reaches `ready`/`error`, replaying the
last few lines on connect. Additive — the polling snapshot endpoint is unchanged and still
the resume-after-refresh path; the frontend does not yet consume SSE (T3.2 kept the
existing poll loop, see §6).

---

## 2. Module map

| Module | Lines | Responsibility |
| --- | --- | --- |
| `landslide/sfm.py` | 505 | pycolmap wrapper; retry ladder; `ImageView`/`ReconCtx` data classes; covisibility graph |
| `landslide/densify.py` | 691 | `StereoConfig`; geometry-gated neighbour selection; per-reference multi-view depth-consensus fusion; outlier/normal filters; scene-based up-vector |
| `landslide/ground.py` | 181 | ray-cast a photo-mode polygon onto a ground DSM for parallax-free region selection |
| `landslide/scaling.py` | 394 | ArUco multi-view (with per-view outlier rejection, squareness fit, PnP cross-check) and manual two-view metric scale, quality gates |
| `landslide/volume.py` | 825 | region selection (photo mode), robust datum fitting, prism volume, DEM differencing, LoD, slope stats |
| `landslide/ortho.py` | 150 | top-down raster render; region selection in ground coordinates (shared `select_region_world`) |
| `landslide/dem.py` | 212 | DEM loaders, IDW surface, trimmed point-to-plane ICP |
| `landslide/change.py` | 124 | two-epoch registration (marker Kabsch or ICP) + change volume |
| `landslide/geo.py` | 175 | EXIF GPS parsing, ENU frame, Umeyama alignment (annotation only) |
| `landslide/geometry.py` | 71 | camera center, undistortion, DLT triangulation, point-in-polygon, ring distance |
| `landslide/enhance.py` | 59 | CLAHE + unsharp radiometric enhancement |
| `landslide/segment.py` | 234 | Roboflow REST client, mask → polygon extraction |
| `landslide/viz.py` | 132 | overlay, height map, slope hazard map |
| `landslide/pipeline.py` | 349 | photo import + culling, `measure` (ground-frame photo selection with image-plane fallback), `run_spec` |
| `landslide/cli.py` | 120 | `run`, `marker`, `spec-template`, `change` |
| `landslide/mkmarker.py` | 61 | printable ArUco marker PNG + exact-size HTML |
| `server/main.py` | ~40 | thin entrypoint: `FastAPI` app, lifespan, no-cache middleware, static mount, includes `routes.router` |
| `server/jobs.py` | ~250 | `Job` class, `JOBS`/`_photo_cache` LRU registries, persistence, lazy ctx reload |
| `server/routes.py` | ~420 | all `@router` HTTP handlers (T3.1 split out of the old `server/main.py`) |
| `server/worker.py` | ~90 | picklable top-level functions run inside the process-pool workers (T3.1/S2) |
| `server/executor.py` | ~80 | lazy `ProcessPoolExecutor` + log-queue drain thread + `BrokenProcessPool` recovery (T3.1/S2) |
| `server/schemas.py` | 33 | Pydantic request models (scale, measure) |
| `server/static/js/main.js` + `coords.js`/`state.js`/`api.js`/`canvas.js`/`steps/*.js` | ~1.1k | ES-module frontend (T3.2, replaces the old monolithic `app.js`): `coords.js` is dependency-free pixel/zoom/pan math (unit-tested from Node, see `tests/test_coords.mjs`); `canvas.js` adds wheel/pinch zoom, pan, vertex drag/insert/delete over T0.7's pointer-event tracing |
| `server/static/capture.html` | 123 | client-side live capture quality helper |
| `tools/synth.py` | ~430 | synthetic ground-truth scene generator; `--preset` (T3.3): `arc` (default, unchanged), `oblique60`, `descending`, `collinear`, `nadir`, `sparse8`, `lowtex`, `distorted` |
| `tools/benchmark.py` | ~190 | T3.3 harness: SfM→scale→dense→measure per preset, Markdown table (registered views, scale/volume error, cloud RMS, runtime, peak RSS) |
| `tests/` | ~2.7k | unit + slow e2e (`-k "not e2e"` for the fast set) plus `test_server.py` (API, TestClient), `test_e2e_presets.py` (`@pytest.mark.slow`, T3.3 presets), `test_coords.mjs` (Node, frontend coordinate math) |

Dependencies (`requirements.txt`): fastapi, uvicorn, python-multipart, pycolmap 4.1.1,
opencv-python-headless 5.0, numpy 2.5, scipy 1.18, pillow, matplotlib, requests, pytest.
Python 3.14. No CUDA (pycolmap CPU wheel; `pycolmap.has_cuda == False` on this machine).

---

## 3. Core algorithms

### 3.1 Photo import and quality gate — `pipeline.py:18-146`

* `ImageOps.exif_transpose`, RGB conversion, downscale to ≤3000 px, JPEG q=92 re-encode.
  Output names `{index:03d}_{stem}.jpg` so lexical order equals capture order (sequential
  matching depends on this).
* Per-photo metrics on a ≤900 px grey copy (`_photo_metrics`): Laplacian variance
  (sharpness), fraction of pixels ≤2 or ≥253 (exposure clipping), 64-bit dHash.
* `_cull` (`:44`): drop clipped ≥50 %, blur below `max(6, 0.25·median)` (capped at half the
  set), near-duplicates with Hamming ≤3 vs previous kept frame; always keep ≥3.
* GPS from originals persisted to `photos/gps.json` (the re-encode strips EXIF).

### 3.2 Structure-from-Motion — `sfm.py`

Conventions: world→camera `x_cam = R·x_world + t`; pixels in stored-photo resolution.

**Retry ladder** (`_build_attempts`, `:80`), cheapest first, stop at first attempt that
passes the gate:

| # | Label | Matcher | Notes |
| --- | --- | --- | --- |
| 1 (or 2) | `default` | exhaustive if n≤45 else sequential(overlap 12) | per-image camera, max 2400 px |
| 2 (or 1 if n≥25) | `global (GLOMAP)` | same | `pycolmap.global_mapping` one-shot |
| 3 | `dense sequential @3200px` | sequential overlap min(25,n−1) | 2 threads |
| 4 | `enhanced low-contrast` | sequential | CLAHE copies, SIFT peak 0.0035, edge 15; skipped when median keypoints ≥2500 (`_median_keypoints` reads the SQLite DB) |
| 5 | `shared intrinsics` | exhaustive if n≤60 | `CameraMode.SINGLE` |

Gate (`_attempt_score`, `:197`): usable ⇔ `points3D ≥ 30 × registered`; done ⇔ usable and
registered ≥ max(3, 0.9n). Final rejection if registered < 50 %. Each attempt wipes
`database.db` and `sparse/`. Threads capped at 4 (2 for the 3200 px attempt) because
pycolmap runs one SIFT extractor per thread; `MALLOC_ARENA_MAX=4` is set before import.

**T2.4 matching/extraction options**: `guided_matching = True` on every attempt's
`FeatureMatchingOptions` (re-verify matches via an estimated local affine/homography — more
survivors under repeated structure and moderate viewpoint change, at extra matching cost).
The `enhanced` (low-contrast) attempt also sets `sift.estimate_affine_shape` and
`sift.domain_size_pooling` (several× slower extraction, confined to the already-most-expensive
fallback). Both are `False` by default in pycolmap 4.1.1. Validated against the plan: this
version's `IncrementalPipelineOptions` defaults already match the plan's recommended
`min_num_matches=15` / `ba_refine_principal_point=False` (and `init_num_trials=200`, already
generous) — no code change made there, since passing an options object that reproduces the
defaults changes nothing. `loop_detection` for ≥30-photo sets (closed sweeps) was **not**
enabled: it needs a vocabulary-tree file this repo doesn't bundle or fetch, untestable against
the one 21-photo synthetic scene, and a bad enable would silently break large real sets rather
than degrade gracefully.

**`build_ctx`** (`:414`) converts the `pycolmap.Reconstruction` into `ReconCtx`:
`views: {name → ImageView(R, t, K, dist, w, h, path)}`, `sparse (N,3)`, `sparse_colors`.
`dist_coeffs` maps COLMAP models (SIMPLE_RADIAL, RADIAL, OPENCV, FULL_OPENCV) to OpenCV vectors.

`covisibility_pairs` (`:465`) counts shared tracks per image pair — used by stereo pair
selection and photo-mode neighbour lookup.

### 3.3 Metric scale — `scaling.py`

**ArUco** (`aruco_scale`, `:89`): decode every registered photo once at ≤2200 px, run
`cv2.aruco.ArucoDetector` for each dictionary in `["DICT_6X6_250","DICT_5X5_100","DICT_4X4_50","DICT_7X7_250","DICT_ARUCO_ORIGINAL"]`
(or the one requested), `cornerSubPix` refine, pick the `(dict, id)` seen in most views
(need ≥2). Corners are undistorted to normalised coordinates and triangulated jointly by
DLT over all observing views (`geometry.triangulate_dlt`). **Per-view outlier rejection**
(T1.5, needs ≥3 views): reproject the triangulated corners into every detecting view; a
view whose mean residual exceeds 3× the median across views is dropped and the
triangulation is refit on the rest (`dropped_views` in `scale_info`). **Squareness fit**
(`_fit_square_side`): rather than the mean of the four (independently noisy) triangulated
edge lengths, a similarity-Procrustes fit of a unit square onto the corners projected into
their own best-fit plane gives `scale = side_m / square_side` — one bad corner drags this
less than it drags a raw edge (each edge shares 2 of the 4 corners; the fit uses all four
against a rigid template). **PnP cross-check** (`_pnp_scale_estimate`): per detecting view,
`cv2.solvePnP` with the marker's known metric geometry recovers an independent camera-to-marker
scale estimate that shares no computation with the triangulation path; `pnp_scale_estimates`
and `pnp_scale_spread` are reported (warning above 10%) as a second opinion, not folded into
the primary scale. Relative error = `clip(max(side_spread, mean_reproj_px / mean_px_side), 0.5 %, 50 %)`.
`scale_info` also stores `marker_corners_m` (metric corners) for later two-epoch registration.

**Manual** (`manual_scale`, `:190`): two endpoints clicked in two different photos →
DLT triangulation → gates: reprojection mean ≤15 px and max ≤40 px, ray angle ≥0.3°,
positive depth in both cameras; warnings at 8 px, 1.5°, <50 px reference length.

### 3.4 Semi-dense stereo — `densify.py`

`StereoConfig` (dataclass) holds every pair-geometry threshold and SGBM knob in one place.

1. **Geometry-gated neighbour selection** (T1.3, `_pair_geometry_ok`): beyond the baseline
   ratio `[0.10, 1.5] × median sparse depth`, a candidate pair must have a convergence angle
   between optical axes in `[4°, 35°]`, a ray angle at the scene centroid in `[3°, 30°]`, and
   a `cv2.stereoRectify` shear (rotation angle of `R1` from identity) ≤40° — too small either
   angle gives noisy/ill-conditioned depth, too large breaks the block matcher's
   fronto-parallel assumption or forces a heavy rectification warp. Shared by two consumers:
   `select_pairs` (a global greedy pair list with a `per_image` cap and a `max_pairs` budget,
   plus a baseline-only rescue pass for images the gate leaves with zero pairs — kept as a
   standalone, independently-testable utility) and `_neighbors_for_view` (T1.4's per-reference
   top-`fusion_k` neighbour list, with its own relaxed rescue when the strict gate finds none).
2. **Multi-view depth fusion** (T1.4, `dense_cloud` → `_depth_map_for_view`, the main
   accuracy lever): for every registered image acting as reference, up to `fusion_k` (4)
   geometry-gated neighbours each produce an independent depth estimate via rectified SGBM
   (`stereo_pair`: `cv2.stereoRectify(alpha=0, CALIB_ZERO_DISPARITY)`, disparity window from
   the 1st/99th percentile of the reference view's sparse depths, `StereoSGBM` block 5,
   P1=200, P2=3200, uniqueness 10, speckle 300/3, mode HH4, run left→right and right→left,
   keep pixels with `|d_R(x−d_L) + d_L| ≤ 1.5`). Each neighbour's resulting world points are
   **re-projected through the reference camera's own distortion model** (`ImageView.project`)
   onto the reference's native pixel grid — equivalent to, and simpler than, inverting the
   rectification remap. `_fuse_depth_candidates` then takes a per-pixel consensus over the
   `fusion_k` candidate depths: two agree within `max(1% of depth, 2× the one-disparity depth
   step Z²/(f·B))`; a pixel's value is the mean of its largest mutually-agreeing cluster,
   kept only when that cluster has ≥2 members (≥1 — self-agreement — when the reference has
   only one usable neighbour at all). This replaces the old fixed-global-pair-list union with
   depth that two independent viewpoints actually agree on.
3. **Per-reference/global fusion** (`dense_cloud`, unchanged mechanics): each reference
   image's fused points are voxel-downsampled immediately (voxel = `sparse extent / 900`,
   model units) as they're produced, then all merged and downsampled again; statistical
   outlier removal (k=10, 2σ, 2 iterations); **support clip** — drop points farther than
   `max(5·voxel, 2·sparse spacing, 2 % extent)` from any sparse point; cap at 2.5 M points by
   growing the voxel (`_cap_voxel`); **surface-normal filter** (`surface_filter`, k=16 PCA
   normals in 300 k chunks) keeps points whose normal is within ~75° of `up`; store float32;
   cache to `work/dense_<w>_<fp>.npz`, keyed to a fingerprint of the sparse reconstruction
   (sorted per-image poses + point count) so a rerun that lands on a different SfM attempt or
   pose set can't silently reuse a cloud from the old frame. Consensus-gated fusion trades
   raw point count for per-point confidence — the fused cloud is smaller than the old
   per-pair union but its points are cross-neighbour-confirmed.
4. **Up vector** (T1.1, `estimate_up`): two candidates — the normal of the dominant plane of
   the sparse cloud itself (`_scene_plane_up`, via `volume.fit_plane_ransac`) and the normal
   of the plane through camera centres (`_camera_plane_up`, the old method, tied to the
   photographer's path rather than the scene). The scene plane wins when the cameras are
   collinear (2nd/1st singular value of their spread < 0.10 — the camera-plane normal is
   otherwise arbitrary within the path's null space) or the two candidates agree within 20°;
   on genuine disagreement, whichever normal more of the cloud's own local surfaces call
   "ground-like" (`surface_filter` vote) wins. Degenerate scenes (no clear dominant plane)
   fall back to the camera-plane estimate.

### 3.5 Region selection

**Photo mode, ground-frame (T1.2, primary path)** — `ground.select_region_ground`: the traced
polygon is cast OUT of the photo onto a top-down DSM instead of projecting the cloud INTO the
photo. `ground.build_dsm` rasters the metric cloud's MEDIAN point height per cell (RC2/A2 —
was max-height, which sat 0.4-0.6 m above real ground on any slope/texture, shifting the
ray-cast hit 1.2-1.7 m toward the camera at a typical 19° elevation); the cell size
(`estimate_cell_size`) is `clip(4×median point spacing, 0.1, 0.5)` m (RC2/A2 — the earlier
"grow the cell until the DSM's own occupied-cell fraction clears 50%" design measured
occupancy over the cloud's BOUNDING BOX, not its real footprint, so the cell always grew to
its 2 m hard cap on every 21-view preset regardless of the cloud's actual 3-5 cm point
spacing). `ground.fill_dsm_holes` then fills each empty cell from its nearest cell WITH data
(`scipy.ndimage.distance_transform_edt`), but only within 2 m — a gap wider than that is a
genuine coverage hole and stays a hole so the ray-cast still reports it as a miss.
`cast_polygon_to_ground` densifies each polygon edge into ≤25 px segments, builds a
world-space camera ray per (densified) vertex (`undistort_normalized` → direction →
`d_cam @ R`), and marches it outward from the (scale-corrected) camera center in `cell / 2`
steps (tied to the DSM's own resolution) to find where its height first crosses at-or-below
the DSM's surface height at the ray's own (u, v) — a linear-interpolated crossing between the
two bracketing valid samples, tolerant of a residual NaN run of up to 8 cells between them.
Returns `(ground_polygon, hit_frac, longest_miss_run)` — `longest_miss_run` (G5) is the
longest CIRCULAR run of consecutive missed vertices, which a flat `hit_frac` hides (e.g.
descending misses in two runs of 6 and 5 rather than scattered singletons — one whole stretch
of the boundary closes across a gap). Vertices whose ray never crosses (open sky, off the
reconstructed footprint, or a gap wider than the tolerance) are dropped; the ground polygon is
handed to `ortho.select_region_world` (shared with ortho-mode tracing) for the interior/rim
masks in true ground coordinates — no parallax, same rim-annulus-in-metres treatment as ortho
mode. Falls back to the legacy image-plane method below when the ray-cast resolves under 50%
of the (densified) vertices — a steep/oblique capture path genuinely caps the achievable hit
fraction below that on some geometry, and the partial-but-parallax-free ground-frame selection
still measurably beats the image-plane fallback there; `pipeline.measure`'s G5 gate (§3.9)
separately reports the achieved `hit_frac`/`longest_miss_run` as `status="indicative"` even
when ground-frame selection was used successfully.

**Photo mode, image-plane fallback** (`volume.select_region`, `:497`): project the whole
cloud into the marked view (`ImageView.project` uses `cv2.projectPoints` with distortion,
returning depth too); points with `depth <= 0` are dropped, and a coarse per-view z-buffer
(`_front_surface_mask`, 4px raster cells, keeps points within 2% of median depth of the
nearest depth in their cell) excludes points that project inside the polygon but sit behind
the visible surface (terrain behind a ridge, a marker board behind the debris) before
`interior` = `points_in_polygon` (matplotlib `Path.contains_points`) is evaluated; `rim` =
ring distance to polygon edges in `[inner, inner + rim_px]` (default inner = rim_px/2 = 6 px,
rim_px = 12 px) and not interior. `extra_views=1` (set by `measure`) ANDs the mask across the
marked view and its most-covisible neighbour (poor-man's space carving).

**Ortho mode** (`ortho.py`): `render_orthophoto` projects the metric cloud onto the ground
basis `(e1, e2) ⟂ up`, `res = span / 1400 px`, keeps the highest point per pixel, draws a
scale bar, writes `ortho.json` with `u0, v0, res, e1, e2, up`. `select_region_ortho` maps
polygon pixels to ground metres and calls the shared `select_region_world` (also T1.2's
ground-frame photo path), which builds the rim annulus in metres:
`inner = clip(6·spacing, 0.08, 0.5)`, `outer = clip(30·spacing, 0.4, 2.5)`.

### 3.6 Datum fitting — `volume.py`

Applied to rim points (falls back to the interior itself with a warning when <15 rim points).

1. **Rim steepness filter** (`:553`): k=10 local PCA normal; drop rim points with
   `|n·up| < 0.57` (>~55° off vertical) if ≥15 remain; warn if rim height range >0.6 m.
2. **Robust plane** (`fit_plane_robust`, `:102`): two candidates —
   (a) iterative 2.5σ clipping from an all-points TLS fit (`_clip_loop`);
   (b) MSAC consensus (`fit_plane_ransac`, 250 iterations on ≤20 k subsample, threshold
   0.5 % of extent, refined at 2.5×MAD) then clipped. Candidate (b) wins only if its median
   absolute residual over **all** rim points is < 0.5× candidate (a)'s. Normal oriented by `up`.
3. **Paraboloid upgrade** (`fit_quadratic`, `:135`): 6-term ridge least squares in the
   plane's (u,v) basis, adopted when `σ_plane − σ_quad > max(0.02 m, 0.25·σ_plane)`.
4. **TPS membrane** (`fit_tps_membrane`, `:211`): smoothing thin-plate spline
   (λ=1e-6, ≤4000 support points, saddle system with `Pᵀw = 0`). Considered when
   `σ > 0.03 m` and ≥200 rim points; adopted only if 3-fold CV RMSE beats the incumbent by
   `max(0.02 m, 15 %)` **and** its maximum deviation from the incumbent over the interior
   is `≤ max(0.10 m, 1.5σ)`.

Datum labels: `rim_plane`, `rim_quad`, `rim_tps`, `surface_plane`, `dem`, `prior_epoch`.

### 3.7 Volume integration — `volume.prism_volume` (`:517`)

* Heights `h = (p − c)·n − datum(u,v)`; drop points with `h > max(1.5 m, 8σ)` (floaters
  above); drop points with `h < −max(high_cap, 3×IQR(h))` (floaters below — the low-side cap
  widens to the region's own height spread so a genuine deep cut isn't clipped like a stray
  stereo point would be).
* `scipy.spatial.Delaunay` on (u,v); per triangle `V = area × mean(h_vertices)`.
* **Bridging cull** (RC1/A1): drop triangles with any edge `> max(20 × median spacing, 0.5 m)`
  — the old `0.5 × region diameter` term let the TIN silently bridge an entire 30-60 m²
  unobserved back-facing slope with a handful of long triangles and report one confident
  number for ground nobody actually measured (measured: 40-62% real occupancy inside the
  traced polygon on every synthetic preset, including the "accurate" ones — the low point
  error was interpolation luck on a smooth synthetic terrain, not real measurement); warn when
  >5 % of area is dropped. `area_measured_m2`/`bridged_area_m2`/`cut_measured_m3` report what
  survived the cull; `cut_upper_m3 = cut_measured + (polygon_area − area_measured) ×
  max_depth_measured` is the upper bound if the unmeasured part were as deep as the deepest
  measured point — the honest range in place of a single silently-interpolated number.
* `fill = Σ V(h>0)`, `cut = −Σ V(h<0)`, `net = fill − cut`, `area = area_measured = Σ kept area`.
* **Coverage gate** (G6, RC1/A1, `_coverage_gate`): independent of the TIN — when the caller
  passes `polygon_ground` (the traced polygon in metric ground coordinates, available for
  ground-frame photo mode and ortho mode), a 0.25 m occupancy grid of the polygon interior
  reports `coverage_frac` (share of in-polygon cells holding ≥1 point) and `largest_void_m2`
  (biggest connected empty patch, via `scipy.ndimage.label`) — the number the bridging cull
  was silently hiding, now measured directly on the traced footprint rather than inferred from
  which triangles got dropped.
* **Slope stats** (`slope_stats`, `:163`): bin to cells of `clip(2.5·spacing, 0.05, 1.0) m`
  (≥3 points), `np.gradient`, report max/mean slope and area >35°.
* **LoD** (`_lod_per_triangle`, `:429`): per-point k=9 local-plane residual
  (`local_roughness`) binned to the same grid → `σ_local`;
  `LoD(x,y) = 1.96·√(σ_datum² + σ_local²)`; `sig_area_frac` = share of area with
  `|h_tri| > LoD`; warning when `|net| < 1.96·σ·area`.
* **Raster DSM cross-check** (T2.1, `_raster_bin`/`_fill_small_holes`, `:461`): the same
  `(u,v,h)` points are also binned to a per-cell median/MAD grid (cell from the cloud's own
  average density, `sqrt(6 / (n/area))`, clipped `[0.05, 1.0] m`) and integrated independently
  (`volume_raster_m3`). Gaps are filled only when enclosed by data on the SAME row or column
  within the TIN's own `20×spacing` bridging radius (checked as 1-D row/column scans, not a
  window sum — a window sum "sees" data on a wide solid block's far side without ever having
  data past the gap, which reopens exactly the bridging bug this exists to catch); unfilled
  gaps are `unmeasured_area_m2`, and a >10% disagreement with the TIN net is a warning. The
  plan's original design made this raster the PRIMARY integrator; validated against this
  codebase's real (multi-view-fused) synthetic benchmark, that regressed volume error from the
  TIN's established 7–8% to 33–40% — real stereo clouds have locally sparse-but-continuous
  patches (foreshortened terrain gets fewer T1.4 depth-consensus votes) that this raster
  correctly refuses to bridge on principle, while the TIN's linear interpolation happens to
  track a smooth natural surface well there. So the TIN stays primary; the raster is kept as
  an independent diagnostic and as the (cheap, no-hole-fill `_raster_net`) resampling proxy
  for the bootstrap CI below.
* **Uncertainty** (T2.2, `bootstrap_volume_ci`, `:598`): when a rim datum was used, 50
  bootstrap resamples of the rim points each refit the plane (and quadratic, if adopted —
  never the TPS membrane, too expensive to refit 50×) and recompute the fast raster net; the
  2.5/97.5 percentile spread **around that resample distribution's own median** (so the
  raster's systematic gap from the TIN cancels out) becomes `(lo_offset, hi_offset)`, applied
  around the primary TIN `net` as `net_volume_ci95_m3`. A coverage term is added in
  quadrature (A4): `(polygon_area − area_measured) × mean|h|` when the G6 coverage gate ran,
  else the older `unmeasured_area_m2 × max|h|` raster proxy — the G6 gap is the more honest
  figure (the raster's `unmeasured_area` only flags cells with literally no nearby data,
  missing the much larger "bridged, not observed" gap the tighter RC1 cull now excludes from
  `area_measured`); `mean|h|` instead of the old `max|h|` so one deep outlier point doesn't
  dominate a term meant to bound a plausible unseen patch. `pipeline.measure` widens the result
  symmetrically by the scale error and sets `est_volume_error_m3 = max(net−lo, hi−net)`; falls
  back to the old `σ_datum·area + 2·scale_rel_error·|net|` heuristic when no CI was computed
  (surface-fallback datum, <15 rim points, or too few valid resamples).

`dem_volume` (`:303`) shares the TIN / bridging / slope / LoD machinery but uses
`h = z_surface − DEM(x,y)`; requires ≥60 % of the region on the DEM. No raster cross-check or
bootstrap CI (no rim to resample).

### 3.8a Quality gates and status — `gates.py` (A3)

`pipeline.measure` calls `gates.evaluate_gates(ctx, res, region_method)` after every other
field is computed and sets `res["status"] ∈ {ok, indicative, rejected}` (worst gate wins) and
`res["reasons"]` (one string per gate that fired). Gates are detectors surfacing a risk the
plan's own algorithmic mitigations would otherwise reduce further (A7 EXIF focal-lock, A8
vertical-baseline stereo, A6 per-camera deregistration are **not implemented** — see
REMAINING_ACCURACY_PROGRESS.md for why), not the mitigations themselves:

* **G1 dense cloud used**: `rejected` when `res["cloud"] != "dense"` or the dense cloud has
  <20 000 points (nadir's stereo-pair baseline-swap bug, RC4, still returns an empty dense
  cloud and silently falls back to the sparse one — G1 is what turns that into a visible
  rejection instead of a confident number on 3k sparse points).
* **G2 marker/scale quality**: `scaling.aruco_scale` now itself **raises** when the four
  triangulated marker sides disagree by >10% (`side_spread_rel`) or the reprojection-implied
  scale uncertainty exceeds 10% — a grazing-angle or blurred marker detection no longer
  silently produces an applied-but-wrong scale (nadir: 33% spread, was silently applied and
  ~40% wrong). The 5–10% band is a soft warning (`scale_info["warnings"]`, also added to
  `manual_scale`'s existing pattern); G2 turns any such warning into `indicative`.
* **G3 per-camera sanity** (detection only): `indicative` when `sfm.reconstruct`'s own
  best-attempt focal-spread check (`_focal_spread` > `FOCAL_SPREAD_RATIO_BAD`, already logged
  to `ctx.warnings` before this gate exists) fired.
* **G4 focal constraint**: `indicative` when `sfm._camera_center_collinearity(ctx.rec) < 0.10`
  — a (near-)collinear capture path (`collinear`, `nadir`) leaves per-camera focal
  self-calibration geometrically underconstrained regardless of how any one run happened to
  converge.
* **G5 ray-cast integrity** (photo mode): `indicative` when the image-plane fallback was used
  at all, or ground-frame selection's `hit_frac < 0.85` or `max_miss_run > 3`;`rejected` below
  `hit_frac < 0.5`. `hit_frac`/`max_miss_run` are always included in the result when
  ground-frame selection ran.
* **G6 coverage**: from `volume.prism_volume`'s `coverage_frac`/`largest_void_m2` (§3.7) —
  `ok` ≥0.85 coverage and ≤2 m² void, `indicative` ≥0.6, else `rejected`.
* **G7 scale honesty floor** (`pipeline.measure`): `scale_rel_error = max(reported, 0.03)`
  unless the ArUco per-view PnP cross-check spread is ≤2% (an actual second, independent scale
  estimate agreeing closely) — a single marker/click measurement rarely earns <3% honestly.
* **G8 UI** (`server/static/js/steps/result.js`): `status`, `reasons`, `region_method`,
  `hit_frac`, `coverage_frac`/`largest_void_m2`, `cut_upper_m3` are shown in the result table;
  gate reasons are folded into the existing warnings panel when `status != "ok"`.

### 3.8 Prior DEM and change monitoring — `dem.py`, `change.py`

* `load_dem`: XYZ text (≥200 points) or GeoTIFF via optional rasterio.
* `DemSurface`: IDW over 9 nearest neighbours in (x,y); NaN beyond 3× point spacing.
* `align_to_dem`: gravity rotation `_gravity_R(up)` → +z, then a 12-start yaw sweep about
  `up` (30° increments, each probed with a short 4-iteration ICP; the best-RMS heading is
  refined to full convergence) — gravity alone leaves heading unconstrained, and a single
  seed only converges within ICP's ~30° basin. Aligns on the cached dense cloud when one
  already exists on disk (`densify.load_cached_dense`, a pure lookup — never builds one;
  building is a worker-process job) instead of the sparse cloud, whose outliers otherwise
  pull the centroid seed; falls back to sparse when no dense cache is present. Centroid
  translation seed, `icp_rigid` (25 iterations, keep best 50 % correspondences,
  point-to-plane linearised solve with rotation step clamped to 0.2 rad). Scale is fixed
  (cloud is already metric).
* `change_volume`: registration priority (1) shared ArUco marker — Kabsch on the four
  metric corners plus a virtual normal point, both signs, all cyclic shifts, accepted if
  max residual ≤ 25 % of side; (2) gravity-seeded trimmed ICP. Epoch A becomes the
  `DemSurface`; epoch B is differenced with `dem_volume`.

### 3.9 Memory and cache management

| Mechanism | Where | Bound |
| --- | --- | --- |
| SfM threads | `sfm.SFM_THREADS = 4` (2 at 3200 px) | ~2–3 GB extraction |
| glibc arenas | `MALLOC_ARENA_MAX=4` (`sfm.py:16`, `server/main.py:13`, `run_server.sh`) | RSS growth |
| Per-pair voxel downsample | `densify.dense_cloud` | no raw 10–30 M point accumulation |
| Fused cloud cap | `MAX_FUSED_POINTS = 2_500_000`, float32 | ~1 GB working set |
| Chunked k-NN | `surface_filter(chunk=300_000)`, `eval_tps(chunk=100_000)` | temporaries |
| Context LRU | `MAX_LOADED_CTX = 2`, `_evict_ctx` skips busy jobs, `gc.collect()` | 2 clouds in RAM |
| Photo thumbnail LRU | `_photo_cache`, `MAX_PHOTO_CACHE = 150` | encoded JPEG bytes |
| Executor | `ThreadPoolExecutor(2)` | 2 concurrent heavy jobs |
| Dense cache | `work/dense_<w>_<fp>.npz` (compressed) | rebuilt only with `force=True` or a fingerprint mismatch |
| Log tail | `Job.log[-400:]` persisted, `[-60:]` in snapshots | |

---

## 4. HTTP API — `server/routes.py` (mounted by `server/main.py`)

All bodies are JSON unless noted; scale and measure bodies are typed Pydantic models
(`server/schemas.py`: `ArucoScaleRequest`, `ManualScaleRequest`, `MeasureRequest`) so
malformed input is rejected with a 422 instead of crashing inside numpy. Errors return
`{"detail": "<message>"}`.

| Method & path | Body / params | Response | Errors |
| --- | --- | --- | --- |
| `GET /` | — | `index.html` | |
| `POST /api/jobs` | multipart `files[]` (3–200 images, ≤80 MB each, ext in `IMAGE_EXTS`) | `{"id": "<YYYYMMDD-HHMMSS-hex6>"}`; SfM starts in background | 400 count/type/size/import |
| `GET /api/jobs` | — | `[{id, status, created, n_photos, has_result}]` newest first | |
| `GET /api/jobs/{id}` | — | job snapshot (see §5.6); starts a non-blocking background ctx reload when `ready` and not yet loaded (`ctx_loading` in the snapshot) | 404 |
| `GET /api/jobs/{id}/events` | — | SSE (`text/event-stream`) tail of `job.log`, closes at terminal status (T3.1/S5) | 404 |
| `GET /api/jobs/{id}/photo/{name}?w=1400` | `w` max width | `image/jpeg` (LRU cached) | 404, 500 decode |
| `POST /api/jobs/{id}/scale/aruco` | `{"side_m": 0.25, "dict": "auto", "id": null}` | `scale_info` (aruco keys, without `marker_px`) | 400 (no marker, <2 views, degenerate), 409 job error |
| `POST /api/jobs/{id}/scale/manual` | `{"length_m": 1.0, "a": {"image", "p1":[x,y], "p2":[x,y]}, "b": {…}}` | `scale_info` (manual keys) | 400 gates |
| `POST /api/jobs/{id}/dem` | multipart `file` (XYZ / GeoTIFF) | `{"aligned": true, "rms_m", "n_points"}` | 400 scale unset / parse / ICP |
| `DELETE /api/jobs/{id}/dem` | — | `{"removed": true}` | 404 |
| `POST /api/jobs/{id}/measure` | `{"polygon": [[x,y],…], "mode": "photo"|"ortho", "image": "<name>", "dense": true, "rim_px": 12}` | `{"queued": true}`; result arrives in snapshot | 400 polygon/scale/ortho, 409 busy |
| `GET /api/jobs/{id}/auto-detect?image=<name>` | optional photo name; omit → `artifacts/ortho.jpg` | `{"frame": "photo"|"ortho", "image", "regions": [{polygon, confidence, class, area_px}], "message"?}` | 400 no key / no ortho, 404, 502 upstream |
| `POST /api/jobs/{id}/ortho` | `{}` | `{"queued": true}` or `{"ready": true}` | 404, 409 busy |
| `GET /api/jobs/{id}/artifact/{name}` | — | file (`png`, `jpg`, `ply`, `json`) | 404 |
| `DELETE /api/jobs/{id}` | — | `{"deleted": id}`; removes directory | 404 |

Path-traversal protection: photo and artifact paths are resolved and checked to be inside
the job directory. A middleware sets `Cache-Control: no-cache` on `/` and `/static/*`.

---

## 5. Data models

### 5.1 `ReconCtx` (`sfm.py:170`)
```
rec            pycolmap.Reconstruction
views          {image_name: ImageView}
sparse         (N,3) float64 model units      sparse_colors (N,3)
photos_dir     Path                            workdir Path
scale          float (m per model unit, 1.0 until set)
scale_info     dict (see 5.2); .scaled ⇔ scale_info["applied"]
dense          {"points": (M,3) float32, "colors": (M,3) uint8} | None
_covis         Counter (lazily attached by volume._get_covis)
geo            dict | None (attached by geo.attach_georef)
cloud(dense=True) → (points, colors) falling back to sparse
```

### 5.2 `scale_info`
ArUco: `applied, method="aruco", dict, marker_id, side_m, views_used, model_sides[4],
side_spread_rel, scale, reproj_px_mean, scale_rel_error, marker_px{view:[4×2]} (not persisted),
marker_corners_m[4×3]`.
Manual: `applied, method="manual", length_m, model_len, images[2], scale, reproj_px_mean,
reproj_px_max, angle_deg, scale_rel_error, warnings[]`.

### 5.3 Ortho metadata (`ortho.json`)
`u0, v0, res (m/px), width, height, up[3], e1[3], e2[3]`; pixel `(col,row)` ↔ ground
`(u0 + col·res, v0 + row·res)` along `(e1, e2)`.

### 5.4 `dem_info`
`{"file": "dem.xyz", "R": 3×3, "t": 3, "rms_m": float}` with `dem ≈ R·(model·scale) + t`.

### 5.5 Result dict (`prism_volume` / `dem_volume` + `measure` additions)
```
net_volume_m3 cut_volume_m3 fill_volume_m3 area_m2
volume_raster_m3? (T2.1 cross-check, prism_volume only) unmeasured_area_m2 (prism_volume only)
net_volume_ci95_m3? [lo, hi] (T2.2, prism_volume only, rim datum + ≥15 rim points)
datum ∈ {rim_plane, rim_quad, rim_tps, surface_plane, dem, prior_epoch}
datum_rms_m est_volume_error_m3 n_points n_rim_points n_rim_outliers n_high_dropped n_low_dropped
mean_height_m max_depth_m max_height_m
max_slope_deg mean_slope_deg area_steep_m2
lod_m lod_max_m sig_area_frac warnings[]
mode ∈ {photo, ortho}  image?  region_method ∈ {ground_frame, image_projection}?
rim_band_px[2]?  rim_band_m[2]?
polygon_px scale scale_method scale_rel_error cloud ∈ {dense, sparse} n_cloud_points
artifacts: [overlay.jpg, heightmap.png, slopemap.png, pointcloud.ply?]
(change_volume adds icp_rms_m, registration ∈ {marker, icp})
```

### 5.6 Job snapshot (`GET /api/jobs/{id}`)
`id, status, error, created, log[-60:], reconstructable, ctx_loading, ortho,
images[{name,width,height,points}],
scale, geo{origin_llh, n_fixes, gps_residual_median_m, gps_scale_vs_marker}|null, result?`.

### 5.7 CLI spec (`cli.TEMPLATE`, `pipeline.run_spec`)
```json
{"photos_dir": "...", "out_dir": "...", "save_cloud": true,
 "scale": {"method": "aruco", "side_m": 0.25, "dict": "DICT_6X6_250", "id": 0}
        | {"method": "manual", "length_m": 2.0, "a": {...}, "b": {...}},
 "region": {"mode": "photo"|"ortho", "image": "...", "polygon": [[x,y],...],
            "dense": true, "rim_px": 12, "rim_inner_px": 6}}
```

---

## 6. Frontend — `server/static/js/` (T3.2, ES modules, no bundler)

Replaces the old monolithic `app.js` (717 lines, global state, no tests) with plain
browser-native ES modules loaded via `<script type="module" src="js/main.js">`:

* `state.js` — the module-scoped equivalent of the old global `state` object
  (`jobId, images, scale, ortho, manual{a,b}, markImg, traceMode, polygon[],
  polygonClosed, lastResult`).
* `api.js` — fetch wrappers for every backend route in §4.
* `coords.js` — **dependency-free** pixel/zoom/pan/rotation math (no DOM, no `window`,
  no `canvas`): image px ↔ display px round-trips under an arbitrary zoom/pan state.
  Imported by `canvas.js` for real interaction and directly by
  `tests/test_coords.mjs` (`node --test`, 8 cases) — the coordinate-chain test the plan
  asked for, without pulling in jsdom/Playwright for a repo that has no other JS
  dependency.
* `canvas.js` — drawing + interaction on the marking canvases: T0.7's Pointer Events
  tracing (click-to-add vertex, Undo, Close, Clear, Freehand, touch-capable) plus T3.2's
  additions — mouse-wheel/pinch zoom, drag-to-pan, vertex drag, edge-click insert,
  Delete/Backspace to remove the selected vertex, Escape to deselect/cancel. Canvas
  backing store still only resizes on image load (T0.7), not per redraw.
* `steps/{upload,scale,mark,result}.js` — one module per workflow step, mirroring the
  original file's own section structure.
* Auto-detect fills `state.polygon` from the largest returned region (unchanged).
* Polling loop (unchanged behavior, moved into `steps/`): 1.2 s `setTimeout` until status
  leaves `reconstructing|measuring|orthorectifying`. SSE (`GET …/events`, §1.2/§4) exists
  server-side but is **not yet adopted client-side** — left as the obvious next step,
  not done in this pass.
* Result panel: volumes, area, depth, datum name, swell factor, warnings, and now (T3.2)
  the T2.2 bootstrap `net_volume_ci95_m3` as a 95% CI next to net volume when present
  (falling back to the flat `est_volume_error_m3` otherwise, mirroring `pipeline.py`'s
  own fallback) plus the T2.1 `volume_raster_m3`/`unmeasured_area_m2` cross-check row;
  all three artifacts (`overlay.jpg`, `heightmap.png`, `slopemap.png`), cache-busted.
* Job list chips, delete, resume via `localStorage["lsv-last-job"]` (unchanged).
* `capture.html`: independent page running Laplacian variance, clip fraction and dHash
  on the live camera at ~5 fps to coach capture quality (unchanged, not part of T3.2).
* **Not done**: a magnifier for manual-scale point placement (plan's optional item) —
  skipped, zoom/pan on the manual-scale canvas already gives precise click placement.
  None of the new zoom/pan/vertex-edit interactions have been exercised in a real
  browser — verified only via `node --check` on every module, the `node --test`
  coordinate suite, and an HTTP smoke test (every module path serves 200).

---

## 7. Validation baseline

`tools/synth.py` renders a 36 m textured terrain with a cosine bowl (R 6 m, depth 2 m,
truth 67.2 m³ inside a 5.6 m polygon) and a 2 m ArUco board, from 21 pinhole views on a
horizontal arc (radius 26 m, height 7 m, yaw −48°…48°), 1200×900 px, f = 1600 px.
`tests/test_e2e_synth.py` asserts ≥15 registered views, ArUco scale within 3 % of the
camera-centre Umeyama scale, manual scale within 2 % of ArUco, cut volume within 30 % of
truth in both photo and ortho modes. README-reported results: ~14 % (photo tracing),
~4–6 % (ortho tracing), 1280 px stereo ~18 %, 640 px ~33 %.

### 7.1 Camera-path preset harness (T3.3)

`tools/synth.py --preset {arc,oblique60,descending,collinear,nadir,sparse8,lowtex,
distorted}` parameterizes the camera path, terrain texture amplitude, and (for
`distorted`) per-vertex Brown-Conrady radial distortion (k1=−0.15) applied before
rasterization; `arc` is the default and reproduces the original single-scene generator
exactly (poses/K/polygon match the pre-existing cached `ground_truth.json` to 1e-12).
`tools/benchmark.py --presets <names> --out <dir>` runs SfM→scale→dense→measure per
preset and emits a Markdown table (registered views, scale error vs. camera-centre
Umeyama, photo/ortho volume error, cloud RMS to the analytic GT surface, runtime, peak
RSS via `resource.getrusage`), degrading a single column to `n/a: <reason>` rather than
failing the whole row when one preset misbehaves.

`tests/test_e2e_presets.py` (excluded from the default `-k "not e2e"` fast run, same as
`test_e2e_synth.py`) covers all seven non-`arc` presets. Since the RC1/A1 bridging-cull
fix (see REMAINING_ACCURACY_PROGRESS.md), `cut_volume_m3` is a measured-only lower bound
on every preset (40-62% real occupancy inside the traced polygon on this benchmark scene),
so thresholds are no longer a tight single-number tolerance against truth; they assert the
plan's own range criterion (`cut_measured_m3 ≤ truth ≤ cut_upper_m3`) plus the `status`
gate (`gates.py`, §3.8a) each preset's measured coverage implies:

| preset | registered | status | coverage_frac | note |
| --- | --- | --- | --- | --- |
| arc, lowtex, distorted | 21/21 | indicative | 60-62% | clean SfM (focal spread ~1.0×); measured/upper-bound range contains truth |
| sparse8 | 8/8 | rejected | 45-48% | partial ray-cast (`hit_frac` 0.86-0.91); small-N reconstruction shows some run-to-run coverage variance |
| collinear | 21/21 | rejected | ~58% | collinear camera path (G4) + coverage just under G6's 0.6 rejection floor |
| descending | 20/21 | rejected | ~47% | degenerate low cameras (RC3); `test_descending_focal_spread_is_tight` can fail on a re-built reconstruction — see REMAINING_ACCURACY_PROGRESS.md §6 (pre-existing `sfm.py` ladder gap, not fixed by this pass) |
| nadir | 21/21 | rejected | — | `aruco_scale` now raises outright (marker side spread >10%, G2) before any volume is computed |
| oblique60 | 21/21 | — | — | unchanged: `aruco_scale` still raises (marker visible in only one photo) |

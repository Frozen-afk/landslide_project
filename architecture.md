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
 │      ├─▶ densify.py   pairwise rectified SGBM → fused semi-dense cloud   │
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
   work/             database.db, sparse/0/, dense.npz (or dense_<w>.npz), enhanced/
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
| 4a. Ortho (optional) | `POST …/ortho` | `densify.dense_cloud` → `ortho.render_orthophoto` | `dense.npz`, `ortho.jpg`, `ortho.json` |
| 4b. Prior DEM (optional) | `POST …/dem` | `dem.load_dem` → `dem.align_to_dem` | `dem.xyz`, `dem_info` (R, t, rms) |
| 5. Mark | browser canvas | `app.js` polygon state | polygon in stored-photo px or ortho px |
| 6. Measure | `POST …/measure` | `pipeline.measure` → `volume.prism_volume` or `volume.dem_volume` | result dict, artifacts |
| 7. Change (CLI only) | `landslide.cli change A B` | `change.change_volume` | change result JSON |

### 1.2 Job state machine (`server/main.py`)

```
 created ──▶ reconstructing ──▶ ready ◀──▶ measuring
                 │                │  ▲
                 │                │  └──▶ orthorectifying
                 ▼                ▼
               error            error (measure/ortho failures return to `ready`
                                       with `job.error` set)
```

Persistence: every status change writes `state.json` atomically (`Job.save_state`,
`server/main.py:106`). On startup `_load_persisted_jobs` reattaches job directories;
jobs interrupted mid-SfM become `error`, finished models become `ready`. The `ReconCtx`
is rebuilt lazily from the COLMAP cache on first touch (`Job.ensure_ctx`, `:142`).

---

## 2. Module map

| Module | Lines | Responsibility |
| --- | --- | --- |
| `landslide/sfm.py` | 480 | pycolmap wrapper; retry ladder; `ImageView`/`ReconCtx` data classes; covisibility graph |
| `landslide/densify.py` | 357 | stereo pair selection, rectified SGBM, fusion, outlier/normal filters, up-vector |
| `landslide/scaling.py` | 296 | ArUco multi-view and manual two-view metric scale with quality gates |
| `landslide/volume.py` | 775 | region selection (photo mode), robust datum fitting, prism volume, DEM differencing, LoD, slope stats |
| `landslide/ortho.py` | 137 | top-down raster render; region selection in ground coordinates |
| `landslide/dem.py` | 212 | DEM loaders, IDW surface, trimmed point-to-plane ICP |
| `landslide/change.py` | 124 | two-epoch registration (marker Kabsch or ICP) + change volume |
| `landslide/geo.py` | 175 | EXIF GPS parsing, ENU frame, Umeyama alignment (annotation only) |
| `landslide/geometry.py` | 71 | camera center, undistortion, DLT triangulation, point-in-polygon, ring distance |
| `landslide/enhance.py` | 59 | CLAHE + unsharp radiometric enhancement |
| `landslide/segment.py` | 234 | Roboflow REST client, mask → polygon extraction |
| `landslide/viz.py` | 132 | overlay, height map, slope hazard map |
| `landslide/pipeline.py` | 325 | photo import + culling, `measure`, `run_spec` |
| `landslide/cli.py` | 120 | `run`, `marker`, `spec-template`, `change` |
| `landslide/mkmarker.py` | 61 | printable ArUco marker PNG + exact-size HTML |
| `server/main.py` | 642 | FastAPI routes, Job class, LRU caches, persistence |
| `server/static/app.js` | 717 | UI state, canvas tracing, polling, result rendering |
| `server/static/capture.html` | 123 | client-side live capture quality helper |
| `tools/synth.py` | 268 | synthetic ground-truth scene generator |
| `tests/` | ~1.9k | 73 tests: unit (geometry, volume, scaling, dem, densify, enhance, segment, geo, import, ortho) + slow e2e |

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
DLT over all observing views (`geometry.triangulate_dlt`). `scale = side_m / mean(4 sides)`.
Relative error = `clip(max(side_spread, mean_reproj_px / mean_px_side), 0.5 %, 50 %)`.
`scale_info` also stores `marker_corners_m` (metric corners) for later two-epoch registration.

**Manual** (`manual_scale`, `:190`): two endpoints clicked in two different photos →
DLT triangulation → gates: reprojection mean ≤15 px and max ≤40 px, ray angle ≥0.3°,
positive depth in both cameras; warnings at 8 px, 1.5°, <50 px reference length.

### 3.4 Semi-dense stereo — `densify.py`

1. **Pair selection** (`select_pairs`, `:39`): covisibility ≥25 tracks, baseline within
   `[0.10, 1.5] × median sparse depth of view a`, sort by covisibility, greedy with ≤2 pairs
   per image, ≤30 pairs total.
2. **Per pair** (`stereo_pair`, `:78`): load both images at ≤`stereo_width` (1280 default,
   640 preview), crop to common size, `cv2.stereoRectify(alpha=0, CALIB_ZERO_DISPARITY)`
   from relative pose `R_rel = R_b·R_aᵀ`, `t_rel = t_b − R_rel·t_a`; swap the pair if the
   baseline comes out negative. Disparity window from the 1st/99th percentile of view a's
   sparse depths: `min_disp = floor(f·B/z_max) − 8`, `numDisparities` rounded to 16, clipped
   to [16, 320]. `StereoSGBM` (block 5, P1=200, P2=3200, uniqueness 10, speckle 300/3,
   mode HH4) run left→right and right→left; keep pixels with `|d_R(x−d_L) + d_L| ≤ 1.5`.
   `reprojectImageTo3D(Q)` → rectified-camera frame → `x_cam = x_rect·R1` → world.
3. **Fusion** (`dense_cloud`, `:270`): voxel = `sparse extent / 900` (model units);
   each pair is voxel-downsampled immediately, all merged and downsampled again;
   statistical outlier removal (k=10, 2σ, 2 iterations); **support clip** — drop points
   farther than `max(5·voxel, 2·sparse spacing, 2 % extent)` from any sparse point;
   cap at 2.5 M points by growing the voxel (`_cap_voxel`); **surface-normal filter**
   (`surface_filter`, k=16 PCA normals in 300 k chunks) keeps points whose normal is within
   ~75° of `up`; store float32; cache to `work/dense.npz` (`dense_<w>.npz` for non-1280).
4. **Up vector** (`estimate_up`, `:205`): normal of the least-squares plane through camera
   centres, sign chosen so it points from the sparse centroid toward the cameras.

### 3.5 Region selection

**Photo mode** (`volume.select_region`, `:475`): project the whole cloud into the marked view
(`ImageView.project` uses `cv2.projectPoints` with distortion); `interior` =
`points_in_polygon` (matplotlib `Path.contains_points`); `rim` = ring distance to polygon
edges in `[inner, inner + rim_px]` (default inner = rim_px/2 = 6 px, rim_px = 12 px) and not
interior. Optional `extra_views` ANDs masks across covisible neighbours (never enabled by
`measure`).

**Ortho mode** (`ortho.py`): `render_orthophoto` projects the metric cloud onto the ground
basis `(e1, e2) ⟂ up`, `res = span / 1400 px`, keeps the highest point per pixel, draws a
scale bar, writes `ortho.json` with `u0, v0, res, e1, e2, up`. `select_region_ortho` maps
polygon pixels to ground metres and builds the rim annulus in metres:
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

* Heights `h = (p − c)·n − datum(u,v)`; drop points with `h > max(1.5 m, 8σ)` (floaters).
* `scipy.spatial.Delaunay` on (u,v); per triangle `V = area × mean(h_vertices)`.
* **Bridging cull**: drop triangles with any edge `> max(20 × median spacing, 0.5 × region
  diameter)`; warn when >5 % of area is dropped.
* `fill = Σ V(h>0)`, `cut = −Σ V(h<0)`, `net = fill − cut`, `area = Σ kept area`.
* **Slope stats** (`slope_stats`, `:163`): bin to cells of `clip(2.5·spacing, 0.05, 1.0) m`
  (≥3 points), `np.gradient`, report max/mean slope and area >35°.
* **LoD** (`_lod_per_triangle`, `:429`): per-point k=9 local-plane residual
  (`local_roughness`) binned to the same grid → `σ_local`;
  `LoD(x,y) = 1.96·√(σ_datum² + σ_local²)`; `sig_area_frac` = share of area with
  `|h_tri| > LoD`; warning when `|net| < 1.96·σ·area`.
* Uncertainty (`pipeline.measure`, `:231`): `est_volume_error_m3 = σ_datum·area + 2·scale_rel_error·|net|`.

`dem_volume` (`:303`) shares the TIN / bridging / slope / LoD machinery but uses
`h = z_surface − DEM(x,y)`; requires ≥60 % of the region on the DEM.

### 3.8 Prior DEM and change monitoring — `dem.py`, `change.py`

* `load_dem`: XYZ text (≥200 points) or GeoTIFF via optional rasterio.
* `DemSurface`: IDW over 9 nearest neighbours in (x,y); NaN beyond 3× point spacing.
* `align_to_dem`: gravity rotation `_gravity_R(up)` → +z, centroid translation seed,
  `icp_rigid` (25 iterations, keep best 50 % correspondences, point-to-plane linearised
  solve with rotation step clamped to 0.2 rad). Scale is fixed (cloud is already metric).
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
| Dense cache | `work/dense.npz` / `dense_<w>.npz` (compressed) | rebuilt only with `force=True` |
| Log tail | `Job.log[-400:]` persisted, `[-60:]` in snapshots | |

---

## 4. HTTP API — `server/main.py`

All bodies are JSON unless noted; request bodies are accepted as untyped `dict`
(no Pydantic models). Errors return `{"detail": "<message>"}`.

| Method & path | Body / params | Response | Errors |
| --- | --- | --- | --- |
| `GET /` | — | `index.html` | |
| `POST /api/jobs` | multipart `files[]` (3–200 images, ≤80 MB each, ext in `IMAGE_EXTS`) | `{"id": "<YYYYMMDD-HHMMSS-hex6>"}`; SfM starts in background | 400 count/type/size/import |
| `GET /api/jobs` | — | `[{id, status, created, n_photos, has_result}]` newest first | |
| `GET /api/jobs/{id}` | — | job snapshot (see §5.6); triggers lazy ctx reload when `ready` | 404 |
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
datum ∈ {rim_plane, rim_quad, rim_tps, surface_plane, dem, prior_epoch}
datum_rms_m est_volume_error_m3 n_points n_rim_points n_rim_outliers n_high_dropped
mean_height_m max_depth_m max_height_m
max_slope_deg mean_slope_deg area_steep_m2
lod_m lod_max_m sig_area_frac warnings[]
mode ∈ {photo, ortho}  image?  rim_band_px[2]?  rim_band_m[2]?
polygon_px scale scale_method scale_rel_error cloud ∈ {dense, sparse} n_cloud_points
artifacts: [overlay.jpg, heightmap.png, slopemap.png, pointcloud.ply?]
(change_volume adds icp_rms_m, registration ∈ {marker, icp})
```

### 5.6 Job snapshot (`GET /api/jobs/{id}`)
`id, status, error, created, log[-60:], reconstructable, ortho, images[{name,width,height,points}],
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

## 6. Frontend — `server/static/app.js`

* Single global `state` object: `jobId, images, scale, ortho, manual{a,b}, markImg,
  traceMode, polygon[], polygonClosed, lastResult`.
* **Canvas coordinate chain** (`setupCanvas`, `:40`): photos are served at ≤1400 px; the
  canvas draws at `displayW`. `k = stored_width / min(1400, stored_width)`.
  Click → `canvas px × (canvas.width / rect.width) ÷ view.scale × k` = stored-photo px
  (`toOriginal`). Decorators draw in stored px scaled by `view.scale / k`. Ortho canvas
  uses `k = 1` so polygons are in ortho pixels.
* Tracing modes: click-to-add vertex, Undo, Close (≥3 vertices), Clear, Freehand
  (mousedown/mousemove/mouseup; thinned to ≤500 vertices). Mouse events only.
* Auto-detect fills `state.polygon` from the largest returned region.
* Polling (`poll`, `:132`): 1.2 s `setTimeout` loop until status leaves
  `reconstructing|measuring|orthorectifying` (and `images` are present).
* Result table (`showResult`, `:470`): volumes, area, depth, datum name, uncertainty,
  swell factor rows, warnings box, `overlay.jpg` and `heightmap.png` (cache-busted).
* Job list chips, delete, resume via `localStorage["lsv-last-job"]`.
* `capture.html`: independent page running Laplacian variance, clip fraction and dHash
  on the live camera at ~5 fps to coach capture quality.

---

## 7. Validation baseline

`tools/synth.py` renders a 36 m textured terrain with a cosine bowl (R 6 m, depth 2 m,
truth 67.2 m³ inside a 5.6 m polygon) and a 2 m ArUco board, from 21 pinhole views on a
horizontal arc (radius 26 m, height 7 m, yaw −48°…48°), 1200×900 px, f = 1600 px.
`tests/test_e2e_synth.py` asserts ≥15 registered views, ArUco scale within 3 % of the
camera-centre Umeyama scale, manual scale within 2 % of ArUco, cut volume within 30 % of
truth in both photo and ortho modes. README-reported results: ~14 % (photo tracing),
~4–6 % (ortho tracing), 1280 px stereo ~18 %, 640 px ~33 %.

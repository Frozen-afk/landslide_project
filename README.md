# Landslide volume from phone photos

Estimate the volume of a landslide from a set of overlapping smartphone
photos, using a reference object of known size for metric scale.

You upload photos swept left → right with 60–80 % overlap, the app runs
structure-from-motion (COLMAP via pycolmap) to reconstruct the scene in 3D,
scales it from a reference marker, densifies it with multi-view stereo, and
integrates the volume between the landslide surface (a polygon you draw on
one photo) and an undisturbed-ground datum fitted to the polygon rim.

Validated end-to-end on a synthetic scene with known ground truth:
scale error < 3 %, volume error ≈ 12 % on a 67 m³ bowl
(`tests/test_e2e_synth.py`).

## Quick start

```bash
cd ~/Documents/landslide-volume
./run_server.sh          # → http://localhost:8000
```

(or `source .venv/bin/activate && uvicorn server.main:app --port 8000`)

Workflow in the browser:

1. **Photos** — select 15–60 photos taken in capture order (left → right,
   60–80 % overlap, same focal/zoom for all shots). Blurry frames and
   near-duplicates are culled automatically; reconstruction runs
   automatically (~1–3 min for 20 photos).
2. **Reference for scale** — either
   - *ArUco marker*: print `aruco_marker.html` at 100 % scale (0.25 m side by
     default), put it in the scene where at least 2–3 photos see it, enter its
     side length, hit detect (dictionary defaults to "auto" — tries all known
     dicts and picks the best match); or
   - *Manual*: pick two photos that show a known length (survey pole, tape,
     person's height), click its two endpoints in each photo, enter the length.
3. **Mark the landslide** — trace the boundary on the **top-down view**
   (recommended: no perspective distortion; generated on demand from the
   dense cloud) or on the photo where the feature is clearest. Click around
   the **rim** (the boundary between disturbed and undisturbed ground) — the
   ring just outside your line is used as the undisturbed-ground datum.
   Close the polygon and press *Compute volume*.
4. **Result** — net / cut / fill volume, area, max depth, datum model, an
   uncertainty estimate (datum roughness + scale accuracy), quality
   warnings, a swell-factor toggle for loose/truckload volume, an overlay of
   your polygon, and a height-difference map.

## Robustness features

- **SfM retry ladder** — if the first reconstruction attempt registers too
  few images, the pipeline automatically retries with denser sequential
  matching, higher-resolution features, and shared camera intrinsics before
  giving up.
- **Blur, exposure & duplicate culling (pre-SfM)** — photos are scored by
  Laplacian variance (sharpness), histogram clipping (exposure), and a
  difference hash; motion-blurred frames, over/under-exposed frames (half
  the histogram pinned at black/white), and near-duplicate shots that add
  no parallax are dropped before feature extraction. Every rejection is
  logged with its reason plus a one-line summary ("quality gate: 2 blurry,
  1 over/under-exposed — 18 of 21 photos kept"). Culling is conservative:
  at most half the upload, ≥ 3 photos always survive.
- **Top-down orthophoto tracing** — optionally render the scaled cloud as a
  bird's-eye orthophoto (with scale bar) and trace the boundary there.
  Pixel coordinates map directly to ground coordinates, so a line drawn over
  background terrain can no longer accidentally enclose far-away points the
  way it can on an oblique photo. End-to-end on the synthetic scene this
  halves the volume error vs photo tracing (6% vs 14%).
- **Curved datum (paraboloid)** — the rim datum is a robustly-fitted plane by
  default, automatically upgraded to a second-order surface when the rim
  residual shows real curvature (road crowns, hillsides, switchbacks), with
  an absolute-improvement floor so noise never flips the model. Spline/RBF
  datums are deliberately not used: the rim is a thin ring with no interior
  data, and splines oscillate when extrapolated across that hole.
- **RANSAC-hardened datum plane** — the plane fit runs two refinements and
  keeps the better one: sigma-clipping from all rim points (right when the
  rim is curved — the paraboloid upgrade then handles the curvature) and a
  RANSAC-seeded clip (right when a *clustered* contaminant — rubble inside
  the rim band, a vegetation patch, stereo floaters from one bad pair —
  would drag the all-points fit before clipping engages). The RANSAC
  candidate only wins when its plane explains the entire rim decisively
  better, so a tight fit on a one-sided band of a curved rim can never
  hijack the datum. Deterministic seed, bounded cost.
- **Outer-buffer rim sampling** — the undisturbed-ground rim band starts a
  little *outside* the traced line (photo mode: half the band width in px;
  ortho mode: 0.1–0.5 m), so clicks that land slightly inside the debris
  edge don't pull the datum down and bias the volume.
- **Quality-gated scaling** — manual scaling rejects misclicks (high
  reprojection error) and near-parallel views (poorly conditioned
  triangulation) with diagnostic messages instead of silently producing a
  wrong scale. Scale accuracy (± %) is estimated from reprojection residuals.
- **ArUco auto-detect** — the dictionary selector defaults to "auto", which
  scans all known ArUco dictionaries so you don't have to remember which
  marker you printed.
- **Bridge-safe triangulation** — Delaunay triangles spanning data holes
  larger than the region diameter are excluded, so marking a region that
  extends over unreconstructed background doesn't invent area or volume.
- **Propagated uncertainty** — the reported ± volume combines the datum
  roughness over the footprint (`RMSE_datum × area`) with the scale error
  acting multiplicatively on the volume (2σ). A material swell toggle
  (loose soil ×1.20, mixed gravel ×1.25, blasted rock ×1.40) converts bank
  volume into the loose truckload volume.
- **Statistical significance (LoD-style)** — every measurement reports
  `lod_m` (95% level of detection: 1.96× datum/surface noise) and
  `sig_area_frac`, the share of the region whose height change exceeds it.
  When the net volume sits inside the noise band the result says so
  explicitly ("net volume is within survey noise") instead of presenting
  noise as debris. Cells are reported, not thresholded — zeroing
  sub-threshold cells would bias thin real layers toward no volume.
- **Slope-hazard map (secondary-slide risk)** — every measurement renders
  `slopemap.png`: the surface binned to a robust grid, slope from central
  differences, colored green (<25°, stable) / yellow (25–35°) / red
  (>35°, over-steepened scarp or debris face at the angle-of-repose risk
  zone). The result table carries `max_slope_deg`, `mean_slope_deg` and
  `area_steep_m2` plus a warning when a meaningful share is over-steepened
  — what a dispatch crew needs before sending people onto the pile.
- **Resolution vs speed dial** — dense stereo runs at 1280 px by default
  (the accuracy choice: ~18% volume error on the synthetic benchmark);
  `dense_cloud(..., stereo_width=640)` is ~5× faster for previews at ~33%
  error. Per-triangle slope estimates on noisy clouds produce spurious 90°
  spikes — that's why the hazard map uses gridded medians/means instead.
- **Job persistence** — jobs, their logs, scale state, and results survive
  server restarts. The 3D reconstruction is rebuilt on demand from the cached
  COLMAP database when you revisit an old job.
- **Degraded-photo hardening** — field photos with deep shadows, washed-out
  gravel or wet low-contrast mud starve SIFT of features. The SfM retry
  ladder now includes a dedicated attempt that runs on CLAHE + unsharp
  enhanced copies (radiometric only — same filenames, same geometry, so the
  poses apply to the originals unchanged) with the SIFT peak threshold
  relaxed (0.0067 → 0.0035) and edge threshold raised. The attempt is
  gated on the actual median per-image keypoint count read from the COLMAP
  database, so it only runs when low contrast is genuinely the bottleneck
  (measured on a crushed-contrast/blurred test set: 72 keypoints/image
  with default SIFT vs 10,987 with the enhanced attempt).
- **Rim steepness filter** — rim points sitting on steep surfaces (a
  retaining wall the boundary climbs, the debris face itself) are excluded
  from the datum fit before plane fitting; a wall-contaminated rim tilts
  the datum so far that a pile reads as a depression (inverted cut/fill).
  When the rim still spans large elevation range, the result carries an
  explicit warning to re-trace on one surface — and tracing on the top-down
  view keeps the rim band in true ground coordinates, away from walls.
- **Upload validation** — enforces a minimum of 3 photos, maximum of 200,
  rejects non-image files, and caps individual file size at 80 MB.
- **Bounded memory** — the pipeline is designed to stay well inside a 24 GB
  laptop. pycolmap runs one SIFT extractor *per thread* and each decodes a
  full photo and builds its octave pyramid, so thread count directly
  multiplies peak RAM (all-core extraction on ~7 MP phone photos exceeded
  24 GB and got the process OOM-killed); extraction and matching are now
  capped at 4 threads (2 for the high-resolution retry attempt — mapping
  stays multithreaded since it dominates runtime but is not memory-bound),
  and glibc's per-thread malloc arenas are bounded via `MALLOC_ARENA_MAX=4`.
  The dense stage is additionally bounded: each stereo pair is voxel-
  downsampled the moment it is produced (no 10–30M-point raw accumulation),
  the fused cloud is hard-capped at 2.5M points, stored as float32, and the
  surface-normal filter runs its k-NN queries in chunks. The server keeps
  at most 2 reconstruction contexts in RAM (older ones reload from the
  COLMAP cache on demand) with explicit `gc.collect()` after heavy jobs.
  Measured: SfM on 21 real 3000×2250 phone photos peaks at ~7.8 GB;
  a full synthetic upload → SfM → dense → ortho → measure run peaks
  at ~1.2 GB.

## How the volume is computed

- The polygon you draw selects part of the 3D point cloud — by projection
  into the marked photo (photo mode), or directly in ground coordinates
  (top-down mode). A ring just *outside* the polygon (the rim band) is taken
  as **undisturbed ground**.
- A datum surface is fitted to the (outlier-clipped) rim points — a plane,
  upgraded to a paraboloid when the residual shows real curvature. This is
  the approximation of the pre-failure surface.
- The interior points are triangulated in the datum plane (Delaunay) and the
  signed heights are integrated over triangle areas: cut = material missing
  below the datum, fill = material piled above it, net = fill − cut.

## Automatic region detection (optional)

The **✨ Auto-detect (AI)** button in the marking step sends the selected
photo — or the top-down orthophoto, usually the better input — to a hosted
SegFormer landslide-segmentation model (Roboflow Serverless) and fills the
boundary polygon automatically. The result is an ordinary polygon: edit it,
clear it, or ignore the feature and trace manually as before.

- Configure: put your key in `.env` as `ROBOFLOW_API_KEY=…` (or export the
  environment variable). Without a key the button returns a clear error and
  everything else keeps working — the feature is fully optional.
- The key lives only server-side; the browser never sees it.
- Reality check: the model is trained on aerial/satellite-scale landslides.
  It detects real slide scars from nadir views well, but will often find
  nothing in close-range phone photos of small roadside debris — in that
  case the UI says so and you simply trace manually. Treat it as a
  convenience where it fires, never as a requirement.
- API: `GET /api/jobs/{id}/auto-detect?image=<photo>` or without the
  parameter to segment `artifacts/ortho.jpg`. Returns simplified polygons
  (largest first) with per-region confidence, in stored-photo or ortho
  pixels respectively.

The same estimate is exposed on the CLI:

```bash
python -m landslide.cli spec-template > spec.json   # edit paths/polygon
python -m landslide.cli run spec.json
python -m landslide.cli marker --side 0.25 --out aruco_marker.png
```

## Capturing good photos

- 60–80 % overlap between neighbours; sweep left → right in one pass;
  15–60 photos for a typical slope.
- Same zoom for every shot (don't pinch-zoom mid-sweep).
- Stand far enough that the whole landslide fits in ~half the frame, and keep
  the reference marker/ruler visible and unblurred in at least 2–3 photos.
  The reference should span ≥ 50 px in the image for ~2–3 % scale accuracy.
- Avoid moving people/vehicles inside the marked area; overcast light is best
  (hard shadows confuse stereo).
- Textured surfaces (bare soil, rock, gravel) reconstruct far better than
  smooth grass or wet mud.

## Accuracy & limitations

- The datum is a **plane** — best when the rim roughly follows a plane. For
  strongly curved slopes the residual shows up in `datum_rms_m` and inflates
  the uncertainty (± σ·area is reported).
- The dense cloud is CPU semi-dense stereo (SGBM on well-connected pairs with
  left/right consistency filtering), not full COLMAP CUDA MVS. Expect cm-level
  noise per point on textured ground.
- Points floating above the surface are trimmed (`> max(1.5 m, 8σ)` above the
  datum) and near-vertical surfaces (walls, tree trunks, a marker board) are
  removed by a surface-normal filter before the volume integral.
- 2.5-D integration: overhangs are not represented; the surface is assumed to
  be a height field over the datum plane.

## Project layout

```
landslide/     core library
  sfm.py         COLMAP SfM wrapper (reconstruct, ReconCtx, covisibility)
  scaling.py     ArUco + manual metric scaling with quality gates
  densify.py     stereo pair selection, rectified SGBM, filtering
  volume.py      region selection, robust rim datum, prism volume integral
  viz.py         overlay + heightmap rendering
  pipeline.py    orchestration (import, measure, run_spec)
  cli.py         command line interface
  mkmarker.py    printable ArUco marker generator
server/        FastAPI app + static web UI
tools/synth.py synthetic scene generator (ground-truth validation)
tests/         unit tests + end-to-end synthetic validation
```

## Development

```bash
source .venv/bin/activate
python -m pytest tests/test_geometry.py tests/test_volume.py tests/test_scaling.py -q  # fast unit
python -m pytest tests/test_e2e_synth.py -s                                          # slow e2e
python tools/synth.py --out data/synth2      # new synthetic scene
```

Re-run requirements: `pip freeze > requirements.txt` (Python 3.14, all deps
install from PyPI — no CUDA required).

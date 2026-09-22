# Remaining Accuracy Plan — post-F18/M2 root-cause audit

Read-only audit of the working tree at `62b6db6` plus the uncommitted `ground.py` / `volume.py` / `test_e2e_presets.py` diff. Every number below was re-measured in this audit from the cached reconstructions (`data/bench/<preset>/work`, `dense=True`, `rim_px=14`, the same `measure()` call as `tests/test_e2e_presets.py`). Probe scripts lived in the scratchpad and wrote nothing into the repository.

## 1. What was verified

**The seven reported photo-mode errors reproduce exactly**: arc 1.73 %, sparse8 1.51 %, lowtex 0.69 %, distorted 6.24 %, collinear 13.85 %, nadir 20.69 %, descending 38.35 %; all with `region_method: "ground_frame"`. The RSS figure was not re-measured (the `eval_tps` change is pure chunking and the TPS unit tests are unchanged).

**The benchmark's reference frame is mis-scaled.** `tools/benchmark.py:117` (and `_umeyama_pose_aware`, `:50-74`) fits `P + fwd_p` — one **model unit** along the optical axis — against `Q + fwd_q` — one **metre**. At 2–3.3 m/unit the two correspondence sets are inconsistent, so the "true" similarity has 0.44–1.08 m camera residuals on *every* preset even where SfM is essentially perfect (corrected fit: 7–22 mm on arc/lowtex/distorted/collinear/sparse8). This invalidates the `scale err %` and `cloud RMS` columns of `data/bench/results.md` and every earlier "true-frame" statement built on them (e.g. the previous audit's "descending scale error 5.66 %"; corrected: 3.76 %). `tests/test_e2e_presets.py::_ortho_polygon_px` passes a pre-scaled `P` (s≈1) and is only mildly affected. Everything below uses the corrected fit (axis endpoint scaled by `1/s`, iterated).

**Decomposition per preset.** "True polygon" = the GT circle placed in ground coordinates (what ortho-mode tracing would give). "Occupancy" = fraction of 0.25 m cells inside the true polygon holding ≥ 1 dense point; "largest void" = biggest connected block of empty cells inside the polygon (98.5 m²).

| preset | scale err (true / reported) | dense pts | occupancy | largest void | ray-cast `hit_frac` | polygon centroid shift toward camera | photo error | error, true polygon | error, true polygon + true scale | error, true polygon shifted −1 / +1 / +2 m along camera axis |
|---|---|---|---|---|---|---|---|---|---|---|
| arc | 0.92 / 1.18 % | 241 k | 0.60 | 39 m² | 1.00 | 1.06 m | 1.7 % | 4.8 % | 1.6 % | 8.9 / 20.1 / 59.3 % |
| lowtex | 0.43 / 1.18 % | 244 k | 0.60 | 39 m² | 1.00 | 0.98 m | 0.7 % | 4.2 % | 1.7 % | — |
| distorted | 0.98 / 1.23 % | 236 k | 0.62 | — | 1.00 | 1.07 m | 6.2 % | 15.6 % | 8.1 % | — |
| sparse8 | 0.06 / 1.07 % | 137 k | 0.40 | 58 m² | 0.94 | 1.24 m | 1.5 % | 11.8 % | 12.1 % | 34.8 / 36.2 / 74.3 % |
| collinear | 0.49 / 1.20 % | 397 k | 0.60 | 37 m² | 0.94 | 1.14 m | 13.8 % | 15.1 % | 13.0 % | 10.6 / 17.6 / 54.7 % |
| descending | 3.76 / 1.37 % | 123 k | 0.41 | 58 m² | 0.68 | 1.08 m | 38.3 % | 33.7 % | 32.0 % | 38.1 / 67.3 / 93.9 % |
| nadir | 40.5 / 33.0 % | **0** (3 134 sparse used) | — | — | — | — | 20.7 % | — | — | — |

**Height-vs-sampling split** (true polygon, same XY sample, heights replaced by the analytic terrain): collinear 76.2 → 74.7 m³ (11 of its 13 error points are interpolation over unobserved ground, 2 are height bias); sparse8 59.2 → 58.2 (all interpolation); arc 68.5 → 67.6; descending 50.0 → 57.3 (15 points interpolation, 11 points model dome). Interior-minus-rim height bias: arc −0.019 m, sparse8 −0.013, collinear −0.057, descending −0.208 m.

Three conclusions follow directly. (i) On every preset, including arc, the pipeline observes 40–62 % of the traced region and integrates the rest across a single 31–58 m² void (equivalent diameter 6–9 m) with Delaunay triangles up to 7 m long; the sign of that interpolation error is geometry-dependent (+11 % collinear, −13 % sparse8, +1 % arc). (ii) The "good" numbers depend on placement: shifting the correct polygon 1 m along the camera axis moves arc from 4.8 % to 8.9 % / 20.1 %, and the ray-cast path currently applies a systematic ~1 m shift of its own. (iii) collinear and descending stay 13 % / 32 % off with a perfect polygon and perfect scale — their residual is not region selection.

## 2. Confirmed root causes

### RC1 — Unobserved back-facing ground is bridged by the TIN (all presets; decisive for sparse8, collinear, descending)
* Geometry: the bowl wall reaches 27.6°; the cameras sit at 19.2° elevation (arc family) or 7.4–19.2° (descending). Any surface sloping away from all cameras more steeply than their elevation is beyond every horizon. Measured inside the true polygon: occupancy 0.40–0.62 at 0.25 m, one contiguous void of 31–58 m² on every preset (near half 6–28 % covered).
* `landslide/volume.py:993-1001` (`prism_volume`): `max_edge = max(20·spacing, 0.5·diameter)` — the diameter term (5.5–7 m here) lets Delaunay span the whole void; the cull removes 0.8–2.3 m² and the "could not be reconstructed" warning stays silent (threshold 5 % of area, `:1006`). With an honest limit (`0.1·diameter` ≈ 1.4 m) the measured area is 57–60 % of the polygon on arc/lowtex/collinear (36–39 % on sparse8/descending) and the cut falls to 50–51 m³ (−24 %): the arc's 1.7–4.8 % is the linear interpolation of a smooth cosine over a 39 m² hole, exactly the "happens to track a smooth natural surface" the T2.1 comment relied on. It will not hold on an irregular scarp or hummocky deposit.
* Consequence for the user: no coverage fraction is reported; `unmeasured_area_m2` (7–11 m²) comes from the raster's 7 cm cells and does not describe this void; TIN `area_m2` is inflated by the bridging (arc 86–95 m² of 98.5 while 40 % of the cells are empty).

### RC2 — Ground-frame DSM: 2 m cells, max-height binning, half-cell lookup shift (all presets)
* `landslide/ground.py:57-66` (`estimate_cell_size`): occupancy is measured over the cloud's **bounding box**; a frustum footprint plus marker board cannot fill 50 % of its bounding rectangle, so the cell grows to the 2 m cap on every 21-view preset (`cell = 2.00`; descending 1.38) despite 3–5 cm point spacing. The DSM is a 16×15 grid for a 36 m scene.
* `landslide/ground.py:86-87` (`build_dsm`): highest point per cell. On a 10 % slope with ±0.5 m texture the 2 m-cell maximum sits 0.4–0.6 m above the ground, which at 19° elevation moves the ray hit 1.2–1.7 m toward the camera. Measured polygon centroid shift toward the camera: 0.98–1.24 m on every preset; 20–53 % of rim-band points inside the bowl radius; datum partly fitted on the bowl wall. Scratch test on a 30 % slope: 1.7 m corner error at 2 m cells, 0.3–0.5 m at 0.5 m, < 0.1 m shape error at 0.2 m.
* `landslide/ground.py:116-117` (`_sample_dsm`): `np.round` lookup against floor-binned cells (`:83-84`) adds a `cell/2` offset (secondary; the max-height term dominates).
* Small cells alone do not fix it: at 0.13–0.5 m cells `hit_frac` falls to 0.47–0.91 because holes are no longer bridged, and missing far-side vertices pull the centroid the other way (collinear −1.6 m). The fix needs a filled, robust surface (see A1).
* `tests/test_ground.py` uses flat `z = 0` ground, on which none of this is visible.

### RC3 — descending: degenerate low cameras accepted, model domed, scale under-reported
* Per-camera intrinsics: IMG_18 `k1 = 0.355`, IMG_19 `k1 = 0.728` (true 0) with 96/112 tracks vs ≈ 1 000 for the rest; IMG_16/17 `k1 = 0.056/0.066`, 257/142 tracks; IMG_20 unregistered. Camera centres 7.4 m / 4.9 m off (all others ≤ 6 cm). `landslide/sfm.py:220-240` (`_focal_spread`) checks focal only (1.095 < 1.15 passes); nothing checks `k1`, track count or per-camera residual; `build_ctx` keeps every registered image and the dense stage fuses against them.
* Model warp: `dz` curvature 0.014–0.016 m/m²; bowl floor 21–24 cm lower than the rim relative to truth. Scale 3.76 % low (≈ 11 % of volume) while `scale_rel_error` reports 1.37 %: `landslide/scaling.py:232` derives it from side spread / reprojection and cannot see a common-mode SfM bias (all 20 PnP estimates are 5–8 % off in the same direction, 1.6 % spread).
* Ray-cast: `hit_frac` 0.68 with miss runs of 6 and 5 consecutive vertices (of 87); `landslide/ground.py:200-202` drops them, the polygon closes across the gap (radius 2.5–6.2 m, 73 m²); `select_region_ground` (`:251`) checks only the fraction (0.5), never contiguity.
* CI [-57, -25] m³ excludes the truth (−67): the coverage term (`landslide/volume.py:1074`) uses raster `unmeasured_area` (7.5 m²), not the 22 m² the TIN culled nor the 31 m² outside the TIN hull.
* `estimate_up` returns the terrain normal (6.5° off gravity, F16 confirmed on every preset); on this domed, half-covered model that alone moves the true-polygon result from 44.5 to 50.0 m³.
* Nothing rejects; every warning is advisory.

### RC4 — nadir: three independent silent failures
* **Dense stage returns nothing.** `landslide/densify.py:416-421` (`stereo_pair`) reads `baseline = -P2[0,3]/fx`. The nadir strip moves along camera **y** (`tools/synth.py:260-268`, `up=(1,0,0)`), so `cv2.stereoRectify` puts the baseline into `P2[1,3]` and `P2[0,3] == 0`; the function swaps, gets 0 again, returns empty for all pairs. `dense_cloud` logs "produced nothing" and `measure` proceeds on the 3 134-point sparse cloud (`landslide/pipeline.py:174`, `:249` sets `cloud: "sparse"`, 472 interior points) with no rejection. Any capture whose motion runs along the image's vertical axis hits this.
* **Focal/depth ambiguity of a parallel-axis strip.** All 21 focals 2 354–2 417 px vs 1 300 true (+82 %), spread 1.03 → the F12 gate (`sfm.py:349`) is blind by construction. F12(b) — lock the focal when the path is collinear and an EXIF prior exists — was never implemented (`lock_focal` is only inserted after a *spread* failure, `sfm.py:354-362`). The model is stretched ≈ 1.8× along the viewing axis.
* **Scale applied on a non-square marker.** Vertical board at 68° incidence in the stretched model: triangulated sides 1.40 / 2.76 / 1.42 / 2.83 (`side_spread_rel = 0.33`). `landslide/scaling.py:245-248` sets `ctx.scale` regardless; the > 5 % warning (`:271-273`) is log-only (aruco `scale_info` has no `warnings` key, unlike manual scale), so `measure` never surfaces it. Reported ±33 % → CI [−147, +41] m³ around a 53 m³ answer.
* The 20.7 % figure is a coincidence of a 40 % scale error on a 1.8×-stretched sparse model.

### RC5 — collinear: single-azimuth capture
* With the true polygon and the true scale the error is 13.0 %; 11 points of it are RC1 interpolation over the 37 m² void (all cameras on one azimuth, so the void is one solid block instead of a fragmented crescent), 2 points are a 5.7 cm interior-vs-rim height bias from stereo at 17–19° grazing from a single direction. Ray-cast selection itself is fine (`hit_frac` 0.94; true volume under the selected polygon 66.9 vs 67.2). The model is not domed (curvature 0.004 m/m², camera residual 7 mm). Not fixable in software below ≈ 10 %; a second azimuth or higher elevation is.

### RC6 — Uncertainty and cross-check are uninformative
* `net_volume_ci95_m3` half-width 17–31 m³ (±25–45 %) on the best presets, dominated by `unmeasured_area × max|h|` (`volume.py:1074`) where `unmeasured_area` (8–11 m²) is an artefact of the raster cell `sqrt(6/density)` ≈ 7 cm (`volume.py:1032`) on a 40–60 %-occupied cloud. Truth inside the CI on 6/7 presets; the miss is descending, the case where it matters.
* The same artefact makes the raster/TIN disagreement warning fire on every preset (14–53 %), so it carries no information.

### RC7 — Test thresholds and harness preserve behaviour, not quality
* `tests/test_e2e_presets.py:105` (`sparse8 < 0.08`) pins a 1.5 % that is 11.8 % with the correct polygon and 35 % one metre either way; `:229` (`descending < 0.50`) pins a result the pipeline should refuse; `:263` (`collinear < 0.20`) pins a 13 % bias; `:149` (nadir scale `< 0.60`) pins an *applied* 40 % scale. `tests/test_ground.py` cannot see RC2. `data/bench/results.md` scale/RMS columns are invalid (§1).

## 3. Per-preset verdict

| preset | dominant causes | correct response |
|---|---|---|
| arc, lowtex, distorted | RC1 latent (39 m² void bridged, benign on a cosine), RC2 (1 m shift), scale 0.4–1 % | **(1) algorithmic** A1 + A2 and **(3) gate** G6; supported when G6 passes, otherwise `indicative` with a measured/upper-bound range |
| sparse8 | RC1 (58 m² void, 40 % occupancy) + RC2 | **(3) gate** (G6 → `indicative`) + **(2) guidance** (≥ 12 views or two azimuths) |
| collinear | RC5 = RC1 from one azimuth | **(2) guidance** (second azimuth / higher elevation) + **(3) gate** (`indicative`); no software fix below ≈ 10 % |
| descending | RC3 + RC1 (6 % near half) + RC2 (miss runs) | **(5) explicit rejection today** via G3/G5/G6; **(2) guidance**: elevation ≥ 20° over the debris, never below crown height |
| nadir | RC4 × 3 | **(5) explicit rejection today** via G1/G2/G4; **(4) separate capture mode later**: vertical-baseline stereo, EXIF-locked focal, horizontal ground marker or GPS scale |

## 4. Supported / unsupported capture geometries

* **Supported (after A1–A3 and with G6 = `ok`)**: convergent capture from **two azimuths ≥ 60° apart** or one arc with elevation above the steepest away-facing slope; ≥ 15 views; camera elevation ≥ 20° over the debris; ≥ 50 cm marker facing the cameras, seen in ≥ 5 views.
* **Supported with mandatory `indicative` label and a measured/upper-bound range**: single convergent arc (arc-class, occupancy 0.6); 8–14 views (sparse8-class); single straight strip with convergent aim (collinear-class).
* **Unsupported — reject with reason**: descending or any path with < 15° elevation over the region; parallel-axis strips without an EXIF focal prior; captures whose dense stage yields < 20 k points; non-square marker fits; regions with occupancy < 0.6.

## 5. Mandatory quality gates (result gains `status ∈ {ok, indicative, rejected}` and `reasons[]`)

| gate | where | rule | failing value today |
|---|---|---|---|
| G1 dense cloud used | `pipeline.measure` after `dense_cloud` | `len(ctx.dense["points"]) ≥ 20 000` and `cloud == "dense"`, else `rejected` | nadir 0 |
| G2 marker squareness / scale quality | `scaling.aruco_scale` | raise when `side_spread_rel > 0.10` or `scale_rel_error > 0.10`; persist `warnings` in `scale_info` | nadir 0.33 |
| G3 per-camera sanity | `sfm.reconstruct` after the best attempt | drop cameras with `|k1| > 0.1` (when median `|k1| < 0.02`) or tracks < 20 % of median; `indicative` if > 10 % dropped | descending IMG_16–19 |
| G4 focal constraint | `sfm.reconstruct` | `_camera_center_collinearity < 0.1`: lock focal to the EXIF prior (F12b); no EXIF → `indicative`, reason "focal unconstrained" | nadir +82 % |
| G5 ray-cast integrity | `ground.select_region_ground` | `hit_frac ≥ 0.85` **and** longest contiguous miss run ≤ 3 vertices; expose `hit_frac` | descending 0.68, runs 6/5 |
| G6 coverage | `pipeline.measure` | occupancy of the selected polygon at `cell = max(4·spacing, 0.25 m)` and largest void: `ok` if occupancy ≥ 0.85 and void ≤ 2 m², `indicative` if occupancy ≥ 0.6, else `rejected`; report `coverage_frac`, `largest_void_m2`, measured-only volume and upper bound | arc 0.60 / 39 m²; descending 0.41 / 58 m² |
| G7 scale honesty floor | `pipeline.measure` | `scale_rel_error = max(reported, 0.03)` unless two independent references agree within 2 % | descending 1.4 % vs 3.8 % true |
| G8 UI | `server/static/js/steps/result.js` | show `status`, `reasons`, `region_method`, `hit_frac`, `coverage_frac`, `cloud`, the measured/upper-bound range | none shown today |

## 6. Smallest effective algorithmic improvements (ordered by value ÷ size)

* **A1 — honest bridging + coverage (RC1, ≈ 30 lines, `volume.py` + `pipeline.py`).** `max_edge = max(20·spacing, 0.5 m)` (drop the diameter term); report `area_measured_m2`, `bridged_area_m2`, `cut_measured_m3` (lower bound) and `cut_upper_m3 = cut_measured + (polygon_area − area_measured) × max_depth_measured`; compute G6's occupancy/largest void with `scipy.ndimage.label` on a 0.25 m grid of the selected interior. Expected effect on arc: reported cut ≈ 51 m³ measured + range up to ≈ 105 m³ with `status = indicative` — i.e. the truth becomes visible instead of guessed.
* **A2 — ground DSM (RC2, ≈ 25 lines, `ground.py`).** `estimate_cell_size` → `clip(4·spacing, 0.1, 0.5)` (or hull-based occupancy via `Delaunay(...).find_simplex`); `build_dsm` → per-cell median height; `_sample_dsm` → floor; for the ray march only, fill every hole from the nearest valid cell (`scipy.ndimage.distance_transform_edt(..., return_indices=True)`) within 2 m and report the share of vertices that landed on filled cells. Add a sloped + noisy fixture to `tests/test_ground.py` asserting < 0.1 m corner error at 0.5 m cells.
* **A3 — gates and status (G1, G2, G5, G6, G7, G8; ≈ 40 lines across `pipeline.py`, `ground.py`, `scaling.py`, `result.js`).** `polygon_area` already exists in `geometry.py`.
* **A4 — CI coverage term (RC6, ≈ 10 lines).** Replace `unmeasured_area × max|h|` with `(polygon_area − area_measured) × mean|h|`; raster cross-check cell = the ground DSM cell so the 10 % disagreement warning means something.
* **A5 — harness (RC7, ≈ 5 lines, `tools/benchmark.py`).** `_umeyama_pose_aware(P, Q, P + fwd_p / s0, Q + fwd_q)` with `s0` from a centres-only fit, iterate twice; regenerate `data/bench/results.md`.
* **A6 — per-camera sanity (G3, ≈ 25 lines, `sfm.py`).** After the best attempt: per-camera `k1` and track counts, `rec.deregister_image` outliers before `build_ctx`, record in `ctx.warnings`.
* **A7 — focal lock on collinear paths (G4 / F12b, ≈ 15 lines, `sfm.py`).** With an EXIF focal and `collinearity < 0.1`, first attempt = `CameraMode.SINGLE` + `ba_refine_focal_length = False`; without EXIF, warn and mark `indicative`.
* **A8 — vertical-baseline stereo (RC4a, ≈ 30 lines, `densify.py`).** In `stereo_pair`, if `|P2[1,3]| > |P2[0,3]|` rotate both views 90° about the optical axis before rectification (`R' = Rz·R`, `t' = Rz·t`, swap `fx/fy`, `cx' = h−1−cy`, `cy' = cx`, `cv2.rotate` the images), run the existing path, rotate results back; unit test: a synthetic vertical-baseline pair yields ≥ 50 % of the horizontal pair.
* Not proposed: raster-as-primary, learned features, OpenMVS, symmetric clip, TPS refits, broader refactors — none addresses RC1–RC5.

## 7. Objective acceptance criteria

1. **Coverage honesty (A1):** on every preset `area_measured_m2 / polygon_area_m2` agrees with the 0.25 m occupancy within 0.05; `cut_measured ≤ truth ≤ cut_upper` on all seven presets; the "could not be reconstructed" warning fires whenever bridged area > 5 %.
2. **Region selection (A2):** ground polygon centroid within 0.25 m of the true circle centre and radius rms ≤ 0.15 m on arc/lowtex/distorted/collinear; `hit_frac ≥ 0.95` with ≤ 10 % of vertices on filled cells; the new sloped-ground unit test passes.
3. **Placement robustness:** for arc, lowtex, distorted: |error of the interpolated estimate| ≤ 8 % for the true polygon and for ±1 m shifts along the camera axis (spread ≤ 6 points), *or* `status == indicative` with the range containing the truth.
4. **collinear:** ≤ 10 % with the true polygon or `status == indicative` (reason: single azimuth / coverage); never `ok` at 13 %.
5. **descending:** `status == rejected` with reasons naming coverage and camera sanity — no numeric error threshold. **nadir:** `aruco_scale` raises (spread 0.33); `measure` refuses on the empty dense cloud; after A8 the dense cloud has ≥ 50 k points; after A7 with an injected EXIF focal, focal within 5 % of 1 300 px.
6. **Uncertainty:** truth inside `net_volume_ci95_m3` in ≥ 90 % of {7 presets} × {rim_px 10, 12, 14} × {true polygon, ±1 m}; CI half-width ≤ 15 % of |net| on arc when `status == ok`; raster/TIN warning fires on ≤ 1 of the five arc-family presets.
7. **Scale:** `scale_rel_error ≥ |true error|` on all presets against the corrected harness; G2 rejects a 2:1 rectangle (unit test).
8. **Tests:** `tests/test_e2e_presets.py` thresholds replaced by 1–7 (status assertions for descending/nadir, placement-robustness assertions for the rest); `pytest -q -k "not e2e"` still green; `results.md` regenerated with `coverage_frac`, `largest_void_m2`, `status`, and error at ±1 m columns.

## 8. Benchmark and real-data validation requirements

* Fix A5 first; all later numbers are measured against the corrected frame.
* Add two presets to `tools/synth.py`: `twosided` (two opposing 11-view arcs) to prove occupancy ≥ 0.9 and interpolation-free error ≤ 5 %, and `hillside25` (25° terrain) for the up-vector/datum semantics (F16, confirmed 6–6.5° off here, still untested at field slopes). Keep `descending` and `nadir` as **rejection** fixtures.
* Synthetic evidence is necessary, not sufficient: pinhole PNGs, one 2 m board, a smooth cosine bowl that flatters interpolation. Before any field accuracy is quoted: ≥ 3 real sites with a GNSS/TLS reference volume, each captured with the supported protocol and once with a degraded protocol (single strip, low elevation) to confirm the gates fire; data outside git; `tools/benchmark.py --real`.
* Capture protocol to ship with the UI: two azimuths ≥ 60° apart or an elevated vantage over the steepest away-facing slope; elevation ≥ 20°; ≥ 15 photos; marker ≥ 50 cm facing the cameras; no photo below the debris crown.

## 9. Ordered implementation plan

1. A5 harness fix; regenerate `results.md`.
2. A1 honest bridging + coverage metrics; re-measure §1 (expect arc-family `indicative` with ranges containing the truth).
3. A2 ground DSM; re-check polygon centroid/rms and `hit_frac`.
4. A3 gates + `status`/`reasons` + UI.
5. A4 CI coverage term; run the calibration sweep (§7.6).
6. A6 per-camera sanity; confirm descending → `rejected` with named reasons.
7. A7 focal lock (EXIF-injected nadir variant); A8 vertical-baseline stereo.
8. Re-pin `tests/test_e2e_presets.py` to §7; update `architecture.md` §3.5 / §3.7 / §7.1 (bridging limit, gates, corrected harness).

## 10. Go / No-Go for field deployment

**No-Go.** Each reason is sufficient on its own: (a) on every preset the pipeline observes 40–62 % of the traced region and reports a confident single number for the rest by interpolating across a 6–9 m void — the arc's 1.7 % is interpolation luck on a cosine, not measurement; (b) descending (38 % off, CI excludes truth) and nadir (0 dense points, 40 % scale error applied) return confident results with no rejection; (c) a ±1 m tracing difference moves the answer by 15–25 points on the best scenes; (d) the reported uncertainty is either ±30 % or wrong where it matters; (e) the benchmark's own reference frame is mis-scaled; (f) no real-site validation exists.

**Conditional pilot (expert-operated, every result labelled `indicative` with its measured/upper-bound range)** once §9 steps 1–5 are merged and criteria §7.1–7.3 and §7.6 pass: two-azimuth or elevated convergent captures, ≥ 15 views, elevation ≥ 20°, ≥ 50 cm marker. Single-strip, descending and nadir captures stay rejected until steps 6–7 land and at least three real sites confirm the gates and the ranges.

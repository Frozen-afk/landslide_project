# Guided Image Capture — Design

**Status:** design only. No application code is changed by this document.
**Base:** `main` @ `9bb999d` (G9 datum-stability gate merged). B1–B5 are fixed (`RELEASE_BLOCKER_PROGRESS.md`). F1–F3 are open (`FINAL_RELEASE_AUDIT.md` §5).
**Revision (post-G9):** §1, §2, §3, §7 and §11 are updated. §3a (measurement paths), §3b (user workflow), §7a (capture failure vs datum failure messages) and §12b (calibration scene) are new. §12 has the revised stage order. §12a is kept as the S0b record; its §12a.7 consequences are now applied through §3 and §12b.

## 1. Context and goal

The capture is the dominant error term in SlopeLens, not the software.

- All 8 benchmark presets end `indicative` or `rejected`. `status = ok` has never fired in photo or ortho mode (`FINAL_RELEASE_AUDIT.md` §2, F2).
- Single-arc coverage is 0.48–0.62. G6 needs ≥ 0.85 for `ok` (`gates.py:85-94`, audit §3.1). The audit calls this "a capture-protocol limit, not a code defect".
- Every rejection reason is a capture fault:

| Preset | Capture fault | Gate that fired |
| --- | --- | --- |
| collinear | walked a straight line | G4 + G6 |
| sparse8 | too few views | G6 |
| descending | camera went below the crown | G5 + G6 |
| nadir | marker seen at a grazing angle | G2 |
| oblique60 | marker out of frame in 19/21 photos | scale refused |

- Every one of these faults is found only after upload, SfM (33–165 s), scale, dense stereo, and measurement. By then the user has left the site.
- Since G9 (`DATUM_STABILITY_PROGRESS.md`), a second failure class exists, and it is **not** a capture fault: the ground around the slide does not predict the ground under it. `twosided_b` is a clean capture (38/38 registered, 98.6 % coverage, G1–G6 silent) and still ends `indicative` through G9 (holdout 0.068–0.074 m, 9–10 % of the volume). More photos cannot fix this. Only a different datum source can.

**Goal:** catch these faults while the user is still on site. Check each photo as it is taken. Check the sequence as it grows. Run a cheap preview reconstruction before the expensive one. Turn every predicted gate failure into a concrete action: where to walk, what to shoot, and how to fix the marker. When the failure is the datum and not the capture, say so plainly: stop recapturing, and name the two remaining options (a pre-event DEM, or an indicative-only result) (§3a, §7a). The final pipeline and the gates stay unchanged. Guided capture adds nothing to the measurement. It only makes sure the input can pass the gates.

**Non-goals:** SfM in the browser, client-side ArUco, changes to gate thresholds, and replacing the existing multi-file upload. The existing upload stays as the "expert / gallery import" path.

## 2. What exists today (reuse inventory)

| Asset | Where | Reuse in this design |
| --- | --- | --- |
| Blur, clip, dHash metrics | `pipeline._photo_metrics` (≤900 px grey, `cv2.Laplacian().var()`, rail clip ≤2/≥253, 9×8 dHash) | Server Tier-1 metric; the JS port must match it (parity test) |
| Cull rules | `pipeline._cull`: clip ≥ 0.5 drop; blur < `max(6, 0.25·median)`; Hamming ≤ 3 duplicate | Same thresholds as live per-shot verdicts |
| Live coach page | `server/static/capture.html`: 160 px Laplacian, clip, dHash on `getUserMedia` | Superseded. Its thresholds (60/20 on 160 px) are on a different scale from the server's. It needs a secure context, which the LAN field server lacks (§8). Keep it as a link. |
| Marker detection | `scaling.detect_marker_corners(gray, s, dict, id, path)`, `_load_gray`, `_detector_params` | Per-photo marker check |
| Marker gates | `aruco_scale`: raises if views < 2, side spread > 10 %, or rel error `max(spread, reproj/px_side)` > 10 %. Warns if spread > 5 % or PnP spread > 10 %. G7 floor is lifted only if PnP spread ≤ 2 %. | Preview scale check; px-size thresholds derived from the rel-error formula |
| Import | `pipeline.import_photos(sources, photos_dir)`: EXIF transpose, ≤ 3000 px, `{i:03d}_` capture-order names, cull, GPS | Finalize calls it unchanged, with originals in capture order |
| SfM | `sfm.reconstruct`, `_build_attempts`, `_run_attempt`, `_largest_reconstruction`, `build_ctx` | Preview = one `_run_attempt` at 1000 px |
| Geometry diagnostics | `sfm._camera_center_collinearity` (G4 < 0.10), `sfm._focal_spread` (G3 > 1.15), `densify.estimate_up(info=…)` (`up_disagree_deg` > 20° gate), `densify.select_pairs` / `_pair_geometry_ok`, `sfm.covisibility_pairs` | Preview report, run on the preview model |
| Ground basis | `ortho.ground_basis(up)`, `geometry.points_in_polygon`, `volume._coverage_gate` (0.25 m grid) | Preview coverage map (coarser grid, sparse cloud) |
| Process isolation | `executor.submit_job(job, fn, *extra, on_done)`; worker functions in `server/worker.py` | Preview runs as a new worker function |
| Job persistence | `Job.save_state` (atomic temp + `os.replace`), `load_persisted_jobs`, `localStorage["lsv-last-job"]` | Capture manifest uses the same atomic pattern. Adds status `capturing`. |
| Two-azimuth presets | `tools/synth.py` `twosided` (S0, `arc_offset` fixed to 90°), `twosided_b` (S0b geometry) | `twosided_b` holds the protocol geometry (§3). Its seed-7 terrain fails G9, so it is the **negative datum control**, not the calibration target. The calibration target is `twosided_cal` (§12b, not built yet). |
| Datum holdout | `volume._datum_holdout` (8 × 90° windows), result fields `datum_holdout_m`, `datum_holdout_windows` | T3 datum estimate on the preview rim (§3a.4). Result-side datum/capture split (§7a). |
| G9 gate | `gates.py` G9 block, `DATUM_HOLDOUT_REL = 0.05`, `DATUM_MIN_WINDOWS = 6` | Unchanged. Its three branches map to three reason ids (§7a). |
| Pre-event DEM path | `dem.load_dem`, `dem.align_to_dem`, `POST/DELETE /api/jobs/{id}/dem`, `volume.dem_volume`, `measure(dem=…)` | Path 2 (§3a.2). No new code for the measurement itself. |

## 3. Capture protocol the system guides toward

This protocol comes from `REMAINING_ACCURACY_PLAN.md` §77/§122 and README "Capturing good photos", corrected by S0b (§12a.4). It is the only protocol with a path to G6 `ok`. **One protocol serves all three measurement paths (§3a).** The path is decided after the measurement, not before the capture, so the user never has to choose a protocol.

**Validation status.** The S0b geometry (`twosided_b`) passed every capture criterion of §12a.5: registration, G6 coverage and both voids, G1–G5, up-vector, marker views and elevation (items 1–4, 6, 7). It failed only accuracy (item 5), and G9 later traced that failure to the seed-7 terrain, not to the capture (`DATUM_STABILITY_PLAN.md` §1). The protocol below is therefore **provisional until S0c (§12b) passes** on a scene whose datum the rim can predict.

1. **Marker:** ArUco board, side ≥ 0.50 m, flat and rigid. Place it on stable ground just outside the landslide rim. Tilt it toward the capture positions (§6).
2. **Rim band:** before arm A, clear the undisturbed ground 0.5–2.5 m outside the slide edge of everything that is not ground: bags, tripods, vehicles, spoil heaps. That band is the datum for path 1 (§3a.1). The one marker board is allowed there; the robust fit removes it (`DATUM_STABILITY_PLAN.md` §2.2).
3. **Arm A:** a convergent sweep of ≥ 12 photos around the slide at one vantage. Walk an arc, do not pivot on the spot. Keep 60–80 % overlap with the previous photo. Keep the camera **≥ 20° above the centre of the slide** (the ROI centroid, not the lowest debris point). Get this from higher ground or by standing closer. Keep the whole rim plus a 2.5 m margin in frame. **At the outer end of the arm, take 2 more photos at the same step than feels necessary** (§12a.4).
4. **Transition:** keep shooting every few steps while walking to the second vantage, so the two arms register into one model.
5. **Arm B:** ≥ 12 photos plus the same 2 outer-end photos, with the arm centre ≥ 60° of azimuth from arm A. Recommended separation: 60–120°, so one tilted marker can face both arms (§6). 90° is the validated value.
6. **Total:** ≥ 15 registered views are required. The target is 30–40 (34 in the validated geometry). The marker must be seen in ≥ 5 photos, with ≥ 2 in each arm.
7. **Never:** shoot from below the crown (descending), walk a straight line (collinear), shoot straight down only (nadir), switch lens or zoom mid-capture (G3), or take burst duplicates.

## 3a. Measurement paths

Every finished measurement ends on exactly one of three paths, or on a capture failure. The path is a property of the **datum** (the pre-slide surface the volume is measured against). The gates are unchanged. The path is only a classification of which gates fired and which datum was used.

| | Path 1: rim-inferred datum | Path 2: pre-event DEM | Path 3: indicative only |
| --- | --- | --- | --- |
| Datum | Surface fitted to the rim band (`rim_plane` / `rim_quad` / `rim_tps`) | Imported DEM (`datum == "dem"`) | Rim fit that G9 found unstable |
| Enters when | No capture-class reason fires **and** G9 is silent | The user uploads a pre-event DEM after scale (`POST …/dem`) and re-measures, and no capture-class reason fires | No capture-class reason fires, G9's `datum uncertain` branch fires, and no DEM is used |
| Best status today | `ok` | `indicative` (G6 DEM fallback, `gates.py:99-107`) | `indicative` |
| What decides the status | G1–G7 + G9 | G1–G5, G7, the G6 DEM fallback. G9 is not evaluated (`datum == "dem"`). | G9 |
| Capture requirement | §3 in full, rim seen on ≥ 6 of 8 sides | §3 in full. The rim band is used for DEM alignment support, not as the datum. | §3 in full |
| What the user gets | Volume + CI, `ok` | Volume against an independent surface, `indicative` with "DEM mode has no coverage gate" | Volume + an explicit datum uncertainty in m³ and % (the G9 reason) |

### 3a.1 Path 1: rim-inferred datum (G9 passes)

The default path. It is valid only where the ground around the slide predicts the ground under it: planar or evenly curved slopes, road cuts, embankments. G9 checks this with the holdout: `datum_holdout_m × polygon_area_m2 ≤ 0.05 × |net_volume_m3|` on ≥ 6 of 8 windows. This is the only path that can reach `ok`.

Known limit (unchanged, `DATUM_STABILITY_PLAN.md` §11): a dome or dip centred on the slide looks the same from every rim arc, so G9 misses it (4 of 15 bad seeds). A path-1 `ok` is therefore a statement about the rim, not a proof about the buried surface. Criterion 8 (§11) checks path-1 results against a DEM on at least one real site.

### 3a.2 Path 2: pre-event DEM measurement

For sites where the rim cannot predict the interior, or where the user already has a survey, LiDAR tile or earlier SlopeLens epoch. It uses the existing flow with no new measurement code: scale → `POST /api/jobs/{id}/dem` (`align_to_dem`: gravity + 12-start yaw sweep + trimmed ICP) → `measure(dem=…)` → `dem_volume`.

- **Status is capped at `indicative` today.** `dem_volume` returns no `coverage_frac`, so G6 falls back to `indicative` for every DEM result. Guided capture does not change this. Reaching `ok` on path 2 needs two new gates: a G6 coverage check in the DEM frame, and a gate on the alignment `rms_m`. That is a separate plan, not part of guided capture, and no path-2 message may promise `ok`.
- **The marker is still required.** ICP does not solve scale (`align_to_dem` docstring). A DEM does not replace the §6 marker rules.
- **DEM coverage:** the DEM must cover the whole traced outline plus the rim band. `dem_volume` refuses below 60 % coverage and warns on any gap.
- **When offered:** at setup ("do you have a pre-event elevation model?", §3b step 1) and on every path-3 result (§7a).

### 3a.3 Path 3: indicative-only output (datum inference unstable)

For sites where G9's holdout branch fires on an otherwise clean capture, and no DEM is available. The volume is still reported, with its datum uncertainty stated in the G9 reason (`±X m, about Y m³ (Z %)`). This is the honest answer for irregular terrain. It is not a failure of the user, and the UI must not present it as one.

Path 3 is **only** the `datum uncertain` branch of G9. The other two G9 branches (`no usable rim`, rim seen on < 6 of 8 sides) are capture or tracing faults (§7a), because a better capture or a corrected outline can fix them. If a side of the slide physically cannot be seen (cliff, river, private land), recapture cannot fix the partial rim either. The result keeps its `capture_failure` class and its `indicative` status, and the `G9_rim_partial` message (§7a.2) tells the user the result can be kept as indicative. The classification is not relaxed for this case.

### 3a.4 Path prediction during capture

The final classification runs on the final result (§7a). During capture, T3 gives an early **datum estimate** so the user learns about path 3 while still on site, when a DEM request can still be made:

- On the preview context, take sparse points in the band 0.5–2.5 m outside the §5.1 ROI. Keep the inliers of `fit_plane_robust`, then run `_datum_holdout("plane", …)` on them with the ROI centroid as centre. Report `preview_datum_ratio = datum_holdout_m × ROI area ÷ |preview net|`, where preview net is `prism_volume` on the sparse interior.
- The estimate is **information only. It never blocks finalize.** A datum failure cannot be fixed by capture, so blocking would only trap the user.
- Fewer than 6 usable windows on the sparse rim → "not enough rim points to estimate the datum yet", not a capture fault. The T3 azimuth-coverage check (§4.3) stays authoritative for missing sides.
- Until calibrated (§12b.6), show it as "datum check estimate" without a predicted path. After calibration, the rule is one-sided: **never predict path 1 when the final G9 fires** on any calibration run.

## 3b. Revised user workflow

1. **Setup.** Marker checklist (§6). Enter the printed side length. Answer one question: "Do you have a pre-event elevation model of this slope (survey, LiDAR, earlier SlopeLens scan)?" Yes / No / Not sure. The answer changes only the messages in step 7. The capture protocol is the same.
2. **Rim check.** Before arm A: "Clear bags, vehicles and equipment from a 3 m strip around the slide edge. That ground is the reference surface." (§3 step 2.)
3. **Capture.** Arm A → transition → arm B, with T0/T1/T2 checks per photo (§4). Counters show the 2 extra outer-end photos per arm.
4. **Preview (T3).** Capture checks decide **block / warn** (§4.3). The datum estimate (§3a.4) is shown as information. If it predicts instability and the user answered "Yes" in step 1: "This slope is uneven around the slide. Plan to use your pre-event model; the photos are still needed for the post-event surface and the scale." If "No": "This slope may only give an indicative volume. Consider requesting a pre-event survey. Finish the capture anyway."
5. **Recapture loop.** Only for capture-class items (§5.4, §7a). Never for the datum estimate.
6. **Finalize → reconstruct → scale → trace → measure.** Unchanged.
7. **Result.** The panel shows the path (§7a) first, then the status and reasons:
   - Capture failure → the "next time" instruction for each capture-class reason.
   - Path 3 → the datum message plus an **"Upload pre-event DEM"** action that uses the existing `POST …/dem` endpoint and re-measures. The result then becomes path 2.
   - Path 2 → "Measured against your pre-event model. Indicative until the DEM coverage gate exists."
   - Path 1 → `ok`.

## 4. Check tiers

Each check runs in the cheapest tier that can answer it. A check that a lower tier can only estimate is repeated in a higher tier, and the higher tier's answer wins.

| Tier | Where | Latency budget | Works offline | Runs on |
| --- | --- | --- | --- | --- |
| **T0 instant** | Phone browser, pure JS | ≤ 300 ms per photo (mid-range phone) | Yes | Every photo, before upload |
| **T1 per-image server** | FastAPI threadpool, cv2 | ≤ 2 s p95 per photo (field laptop) | No; queued until online | Every photo, after upload |
| **T2 sequence** | Server, from the T0/T1 results already stored | ≤ 100 ms | Partly: the T0 subset runs on the client | Every photo change |
| **T3 preview SfM** | Worker process, pycolmap one attempt at 1000 px | ≤ 60 s for 30 photos (4-core laptop) | No | End of each arm, on demand, before finalize |

### 4.1 Per-image checks

| Check | Tier | Metric | Pass / warn / fail | Instruction on fail |
| --- | --- | --- | --- | --- |
| Blur | T0, confirmed T1 | Laplacian variance on a ≤900 px grey decode, same as `_photo_metrics` | Fail if `< max(6, 0.25·session median)`. Warn if `< 0.5·median`. Before 5 photos exist, use an absolute floor calibrated in S1. | "Blurry — hold still, tap to focus on the slope, retake." |
| Exposure | T0 | Fraction of rail pixels (≤2 or ≥253) | Fail ≥ 0.50 (server drops it). Warn ≥ 0.10. | "Sky or shadow is blowing out the frame — tilt down or step so the sun is behind you." |
| Duplicate / no movement | T0 | dHash Hamming distance to the previous kept photo in the same arm | Fail ≤ 3 (server drops it). Warn ≤ 6. | "Same as the last photo — take 1–2 steps along the arc first." |
| Lens / zoom consistency | T0 | EXIF `FocalLength`, `FocalLengthIn35mmFilm`, `LensModel` compared with photo 1 | Fail on mismatch. Missing EXIF → warn once per session. | "Camera switched lens (0.5×/2×). Set it back to 1× and retake." (G3 cause) |
| Resolution / orientation | T0 | Long side ≥ 1600 px. Orientation same as photo 1. | Warn only | "Keep the phone in the same orientation for every photo." |
| Features | T1 | SIFT keypoint count at 1000 px (`cv2.SIFT_create(4000)`) | Fail < 300. Warn < 800. | "Too little texture in frame (water, snow, sky) — include more of the slope and its edges." |
| Overlap with previous photo | T1 | Ratio-test SIFT matches vs the previous kept photo in the arm → `findFundamentalMat` RANSAC inliers | Fail < 40 inliers. Warn < 100. | "Too far from the last photo — step back half the distance and retake." |
| Parallax (pivot detection) | T1 | Homography inliers ÷ fundamental inliers against the previous photo | Warn if ≥ 0.95 and the median match displacement is small. Fail on two such photos in a row. | "You turned without moving. Walk sideways along the arc between shots." |
| Marker present / size / angle | T1 | `detect_marker_corners` on `_load_gray(path)`. Mean side in stored-resolution px. Min/max side ratio. Corners ≥ 10 px inside the frame edge. | Size: ok ≥ 60 px, warn 30–60, fail < 30 (see note). Angle: warn if side ratio < 0.6, fail < 0.4. Clipped at the edge → warn. | "Marker too small — move closer or use a bigger print." / "Marker seen edge-on — turn the board toward you." / "Marker cut off at the frame edge." |
| Arm-bridge overlap | T1 (first 3 photos of arm B and transition photos) | Same inlier test vs the last 3 photos of arm A | Warn < 40 | "Arm B isn't linking to arm A — take 3–5 photos while walking back toward your first position." |

**Where the marker size thresholds come from.** `aruco_scale` rel error = `reproj_px / px_side`. Rejection is at 10 %. The G7 3 % floor is lifted only by PnP agreement. With about 1–2 px of real-photo reprojection error, a 30 px side gives about 5 % and a 60 px side about 2–3 %. The size is measured in stored-photo px, after `import_photos` resizes to ≤ 3000 px, not in raw sensor px. Calibrate these thresholds in S5 against 20–30 cm and 50 cm prints at 5–15 m (audit §6 item 3).

### 4.2 Sequence checks (T2, with a T0 subset on the client)

| Check | Tier | Metric | Threshold | Instruction |
| --- | --- | --- | --- | --- |
| Photo count | T0 | Kept photos per arm and in total | ≥ 12 per arm. ≥ 15 total is required, 25–35 is the target. | "Arm A: 7 of 12 — keep walking the arc." |
| Marker views | T2 | Photos with the marker at ok or warn size | ≥ 5 total and ≥ 2 per arm | "Marker seen in only 1 arm-B photo — include it in 2 more." |
| Heading spread per arm | T0 (sensor only) | Circular range of the compass heading in the arm | ≥ 30° | "Your photos all point the same way — walk further around the slide." |
| Arm separation | T0 (sensor only) | Difference of the arm-A and arm-B circular mean headings | ≥ 60° (warn below). Also warn above 120° (marker cannot face both arms). | "Arm B is only 35° from arm A — walk further around, to about bearing 210°." |
| Elevation (pitch) | T0 (sensor only) | `DeviceOrientation` beta → camera pitch below horizontal | Warn if pitch < 10° for > 30 % of an arm. This is a proxy only. T3 measures the true elevation. | "You're level with or below the debris — get higher, never below the crown." |
| Overlap chain | T2 | Every consecutive pair in an arm passes the T1 overlap check | No fail links | "Gap between photos 8 and 9 — retake between them." |

The sensor checks are advisory. The T3 preview decides.

### 4.3 Preview-reconstruction checks (T3)

Run one `_run_attempt` (exhaustive matcher, n ≤ 45, 1000 px, per-image cameras) into `capture/preview/`, never into `work/`, then `build_ctx`. On the preview context compute:

| Check | Source function | Maps to | Threshold → verdict |
| --- | --- | --- | --- |
| Registration and connectivity | `num_reg_images` of `_largest_reconstruction` vs photos submitted. Photos missing from the largest model, per arm. | SfM < 50 % hard fail; G1 | < 90 % registered, or a whole arm missing → **block** |
| Collinearity | `_camera_center_collinearity(rec)` | G4 | < 0.10 → **block** (G4 always fires) |
| Focal spread | `_focal_spread(rec)` | G3 | > 1.15 → warn and check T0 lens consistency |
| Camera elevation | `estimate_up(info=…)`, then the angle of each camera centre above the ROI centroid, in the up frame | G5 / G6 (descending); S0b showed it also predicts G6 voids | Median < 20° or any arm < 15° → warn, shown together with the predicted G6 class. Any camera below the ROI's lowest rim point → warn. |
| Up-vector agreement | `info["disagree_deg"]` | up gate (> 20°) | > 20° → warn: "include more flat, stable ground around the slide in frame" |
| Azimuth coverage | Camera azimuths about the ROI centroid in the ground basis (`ortho.ground_basis(up)`), in 8 × 45° sectors. Arm separation computed from the model. | G6 | Separation < 60° → **block**. Separation is authoritative here, unlike the compass. |
| Stereo pairs | `densify.select_pairs(ctx)` count. Share of images with ≥ 1 geometry-gated neighbour (`_pair_geometry_ok`). | G1 (dense < 20 000 points) | < 70 % of images with a pair → warn |
| Marker scale | `aruco_scale(ctx, side_m, …)` in a try block. Record `side_spread_rel`, `scale_rel_error`, `pnp_scale_spread`, `views_used`. | G2 / G7 | Raises → **block**, show its message. Spread > 5 % → warn. PnP spread > 2 % → info: "scale will carry the 3 % floor". |
| Coverage proxy | §5 | G6 | Class predicted per §5.3 |
| Datum estimate | `_datum_holdout("plane", …)` on the sparse rim band around the ROI (§3a.4) | G9 (`datum uncertain` branch) | **Information only, never blocks.** Shown as an estimate until §12b.6 calibrates it. |

**Block** means finalize is refused unless the user sets `override=true` (expert pilot). **Warn** is shown with an instruction but allows finalize.

## 5. Coverage tracking and directional recapture

### 5.1 Region of interest

G6 is measured inside the traced polygon, so the preview needs a rough region.

- After arm A's first photo, the user draws a rough outline on that photo with a few taps. This reuses `canvas.js` tracing. "Skip" is allowed. When skipped, the ROI defaults to the convex hull of the central 50 % of the sparse cloud.
- On the preview context, the ROI is lifted to ground coordinates:
  1. Project the sparse points into that photo (`ImageView.project`).
  2. Keep the points inside the outline (`points_in_polygon`) and in front of the camera.
  3. Take the ROI as their convex hull in the `ground_basis(up)` plane.
- A dense DSM ray-cast is not needed. A rough ROI is enough for guidance.

### 5.2 Per-cell observation map

- Grid the ROI at 1.0 m. The sparse cloud is too thin for G6's 0.25 m cells.
- For each cell, from sparse points and their tracks (`rec.points3D[*].track` → image ids), record:
  - `n_points`
  - `n_cams`: distinct cameras observing points in the cell
  - `sectors`: set of 45° azimuth sectors of those cameras about the cell centre
  - `normal`: local plane fit on the cell's points plus neighbouring cells' points, when there are ≥ 6 points
- Cell class:
  - **void**: no points, or the cell's normal faces away from every observing camera (dot < 0.1). This is the "beyond every horizon" case in `REMAINING_ACCURACY_PLAN.md` §30.
  - **weak**: observed from only 1 sector, or by < 3 cameras.
  - **good**: ≥ 2 sectors ≥ 60° apart and ≥ 3 cameras.
- `preview_coverage = good ÷ ROI cells`. `preview_void_m2` = the largest connected region of void cells (`scipy.ndimage.label`, as in `_coverage_gate`).

### 5.3 Predicting G6

The sparse proxy does not equal dense G6. It must be calibrated in S5 on the presets. Fit two cut points, `c_rej` and `c_ok`, so that the predicted class equals the final G6 class on arc, lowtex, distorted, collinear, sparse8, descending and twosided.

Constraint: **no preset may be predicted `ok` or `indicative` when its final G6 class is `rejected`.** Among cut points that meet this constraint, prefer the most permissive. Until calibration, show the proxy as "coverage estimate" without a class.

### 5.4 Directional recapture instructions

For each connected group of void or weak cells of ≥ 2 m² (the G6 void limit):

- **Needed direction.** Take the mean downslope horizontal direction of the group's normals. Where no normal exists, use the direction opposite to the sectors that did observe the group. The camera must stand on that side, looking back across the group.
- **Stand distance.** Use the median camera-to-ROI distance of the existing arms. This keeps the same GSD and a similar elevation.
- **Express the direction** in whichever frame is available:
  - **Compass bearing** (sensor data present): align model azimuths to compass headings with a circular-mean offset between each camera's model heading and its recorded `alpha` heading. Only do this when the residual spread is < 20°.
  - **Relative to the user** (always available): "walk about 70° around the slide to your right from where you took photo 1" or "from the far side of the slide, opposite arm A".
  - **Top-down sketch**: ROI outline, camera positions as dots (per arm, coloured), void cells shaded, and an arrow and target arc for the needed position. Drawn on a canvas from the preview report, with no new library.
- **Count:** `max(5, ceil(group angular width / 8°))` photos. 8° per step is the widest registering step measured on `sparse8` (`tools/synth.py:57-61`).
- **Camera-side wall voids** (S0b, §12a.3): a void on the wall nearest the cameras faces the gap between the arms. The instruction is "continue arm A (or B) outward by N photos from its end", with N = ceil(angular distance ÷ 7°). Never "go to the far side": an isolated far-side mini-pass does not register.
- **Priority:** largest area first. At most 3 instructions are shown at once.
- **After the recapture photos**, re-run the preview and update the map.

## 6. Marker placement guidance

Shown as a checklist before arm A. The design turns each existing failure into a rule.

| Rule | Why (evidence) |
| --- | --- |
| Side ≥ 0.50 m. Print on rigid board (foam core or plywood). Never a curled sheet. | README/plan protocol. Bent print → spread > 10 % → `aruco_scale` raises (G2). The UI default `aruco-side` is 0.25 m. Guided capture asks for the printed size explicitly instead of defaulting. |
| Place it outside the traced rim, 1–3 m from it, on stable undisturbed ground | Inside the polygon, the board becomes part of the measured surface. On debris it can move between photos. |
| Tilt it so its normal points at the midpoint between the two arm centres, about 30–45° up from horizontal | nadir failure: a vertical board seen at a grazing angle → 38 % spread. One marker must be face-on enough for both arms, which limits arm separation to about 120°. `aruco_scale` uses only the (dict, id) seen in most views, so a second marker adds nothing today. |
| Include it in ≥ 5 photos, ≥ 2 per arm, at ≥ 60 px side, fully in frame | `aruco_scale` needs ≥ 2 views and rejects above 10 % rel error. oblique60 failed with the marker out of frame in 19/21 photos. More views give PnP agreement ≤ 2 %, which lifts the G7 floor. |
| Avoid hard shadow edges or glare across the board. Keep the board dry and matte. | Adaptive-threshold detection (`_detector_params`) fails on split illumination. |
| Enter the printed side length, measured with a tape, before arm A | The scale is `side_m ÷ fitted side`, so any error in `side_m` passes straight into the volume (× 3). |

**Live feedback:** a per-photo marker chip (T1: ok / small / edge-on / clipped / none) and a running counter "marker: 3 of 5 views (A 2, B 1)".

## 7. Mapping G1–G8 to user instructions

This table is the single source for the instruction text. It is used in three places: the capture UI (predicted), the preview report, and the result panel (actual). `landslide/gates.py::RULES` holds the table and the §7a classes (§9.2). It lives next to the gates so that the result-side mapping (S6a) ships before the capture module exists. The existing `evaluate_gates` reason strings are unchanged. The result panel adds the matching "what to do next time" line by matching the gate id, which requires an id per reason (§9.3).

| Gate (source) | Final-pipeline failure | Capture-time predictor (tier) | User instruction |
| --- | --- | --- | --- |
| SfM < 50 % registered / no reconstruction (`sfm.py`) | Job `error` | T1 overlap chain, T3 registration | "Photos N–M don't connect. Retake that stretch with smaller steps (more overlap)." |
| **G1** dense cloud < 20 000 points or sparse fallback (`gates.py:30-33`) | `rejected` | T1 features, T3 stereo-pair share, nadir-only path (T3 elevation ≈ 90°) | "Not enough overlapping texture for 3-D. Shoot from an angle (not straight down), 70 %+ overlap, include textured ground." |
| **G2** marker side spread 5–10 % warn; > 10 % or rel error > 10 % refused (`scaling.py`) | `indicative` / scale refused | T1 marker size and angle, T3 `aruco_scale` | See §6: "closer", "turn board toward you", "flatten the board", "more marker views". |
| **G3** focal varies > 1.15 (`gates.py:45-47`) | `indicative` | T0 EXIF lens consistency, T3 `_focal_spread` | "Don't zoom or switch lenses. Stay on the 1× camera for every photo." |
| **G4** collinearity < 0.10 (`gates.py:53-59`) | `indicative` | T0 heading spread, T3 `_camera_center_collinearity` | "You walked a straight line. Walk a curve around the slide, and add a second vantage point." |
| **G5** image-plane fallback, hit < 0.85 or miss run > 3 (indicative), hit < 0.5 (rejected) (`gates.py:62-74`) | `indicative` / `rejected` | T0 pitch, T3 camera elevation vs ROI, ROI in frame | "Get higher (≥ 20° above the debris). Keep the whole rim and a margin in every photo. Never shoot from below the crown." |
| up-vector disagreement > 20° (`gates.py:80-83`) | `indicative` | T3 `estimate_up` `disagree_deg` | "Include more flat stable ground around the slide in frame." |
| **G6** coverage < 0.6 (rejected), < 0.85 or void > 2 m² (indicative) (`gates.py:86-94`) | `indicative` / `rejected` | T0 arm count and separation, T3 azimuth sectors and coverage proxy (§5) | Directional recapture (§5.4): "Walk to bearing 210°, about 25 m out, take 8 photos across the slide." |
| DEM mode no coverage gate (`gates.py:99-107`) | always `indicative` (path 2) | none (not a capture fault) | Result panel only: "Measured against your pre-event model. Indicative until the DEM coverage gate exists." |
| **G9** `no usable rim` (`datum == "surface_plane"`) | `indicative` | T3 rim-band point count around the ROI, ROI traced to the frame edge | Capture/tracing fault, §7a `G9_no_rim` |
| **G9** rim on < 6 of 8 sides | `indicative` | T3 azimuth coverage, T3 datum-estimate window count | Capture fault, §7a `G9_rim_partial` |
| **G9** `datum uncertain` | `indicative` (path 3) | T3 datum estimate (information only) | **Datum fault, not capture**, §7a `G9_datum_unstable` |
| **G7** scale floor 3 % unless PnP spread ≤ 2 % (`pipeline.py:282-285`) | wider CI | T3 `pnp_scale_spread` | "Get the marker into more photos from varied distances to tighten the scale." |
| **G8** result UI (`result.js`) | — | — | The result panel shows the capture report (§9.3) next to the status. A capture finalized with `override` is labelled as such. |

## 7a. Capture failure versus datum failure

The user must be able to tell, from the first line of the message, whether **going back out with the camera will help**. Every reason id carries one of three classes:

| Class | Meaning | Fix | Blocks finalize at T3? |
| --- | --- | --- | --- |
| `capture` | The photos did not measure what the gates need | Recapture or retrace | Per §4.3 (block or warn) |
| `datum` | The photos are fine. The rim cannot predict the ground under the slide. | Pre-event DEM (path 2), or accept path 3 | Never |
| `info` | Path or mode label, no fault | none | Never |

### 7a.1 Classification of a final result

Computed on the server from the reason ids (§9.3, S6a) and `datum`, and returned as the additive field `res["measurement_path"]`. Apply in this order. The first match wins.

1. Any reason with class `capture` → **`capture_failure`**. If `G9_datum_unstable` also fired, append the combined message below.
2. `datum == "dem"` → **`dem`** (path 2).
3. `G9_datum_unstable` fired → **`indicative_only`** (path 3).
4. `status == "ok"` → **`rim`** (path 1).
5. Anything else → `capture_failure`. This is a fallback so that an unmapped reason can never be shown as path 1 or path 3. Criterion 9 (§11) asserts it is never reached on the presets.

Reason ids and classes (the §7 gates plus the three G9 branches):

| Reason id | Gate | Class |
| --- | --- | --- |
| `G1_thin_dense`, `G2_scale_warn`, `G3_focal`, `G4_collinear`, `G5_fallback`, `G5_partial`, `G5_rejected`, `UP_disagree`, `G6_indicative`, `G6_rejected` | §7 | `capture` |
| `G9_no_rim` | G9, `datum == "surface_plane"` | `capture` |
| `G9_rim_partial` | G9, `datum_holdout_windows < 6` or `datum_holdout_m is None` | `capture` |
| `G9_datum_unstable` | G9, `datum_holdout_m × area > 0.05 × \|net\|` | `datum` |
| `DEM_no_coverage_gate` | G6 DEM fallback | `info` |

### 7a.2 Messages

Each message says what happened, whether recapture helps, and the one next action. The text is the default for `RULES` (§9.2). `{…}` fields come from the result dict.

| Reason id | First line (shown large) | Detail and action |
| --- | --- | --- |
| `G9_no_rim` | **Capture or outline problem — fixable.** No undisturbed ground was measured around the outline. | "First, check the outline: trace the slide edge, not the photo edge. If the outline is right, recapture with 2–3 m of undisturbed ground around the slide in every photo (§3 step 2)." |
| `G9_rim_partial` | **Capture problem — fixable.** Undisturbed ground was measured on only {n} of 8 sides of the slide. | "Recapture so the ground beyond the {side} edge is in frame." `{side}` comes from the additive field `datum_holdout_missing_deg` (S6a), expressed as in §5.4 (compass bearing when aligned, otherwise relative to arm A). Without that field, omit the side. Add: "If that side cannot be seen (cliff, river, no access), keep this result: it is indicative only." |
| `G9_datum_unstable` | **Not a photo problem — more photos will not help.** The ground around the slide varies by ±{dh:.2f} m, so its edge does not predict the surface the slide removed. | "The volume {net:.1f} m³ is indicative, with about ±{dh·area:.1f} m³ ({pct:.0%}) from the reference surface alone. For a measured result, upload a pre-event elevation model (survey, LiDAR, earlier scan) and re-measure." Show the **Upload pre-event DEM** action. If the setup answer was "Yes", say "Upload the model you mentioned at setup." |
| `G9_datum_unstable` together with any `capture` reason | **Fix the capture first, but expect an indicative result.** | Show the capture instructions first. Then: "The reference-surface check also failed. After a better capture this result will still be indicative unless you use a pre-event elevation model." This stops the user from recapturing in the hope of reaching `ok`. |
| `DEM_no_coverage_gate` | **Measured against your pre-event model.** | "Indicative until SlopeLens can check how much of the slide the model covers. The datum was not inferred from the rim, so the rim check does not apply." |
| capture ids (§7) | **Capture problem — fixable.** {existing §7 instruction} | Unchanged from §7. |

At T3 (preview) the same ids are used with "predicted" in front of the first line. The datum estimate uses its own text:

- Unstable estimate: "Early check: the ground around the slide looks uneven. The result may be indicative only. This does not depend on your photos. Keep capturing." Plus the step-4 DEM sentence from §3b.
- Stable estimate: no message. Silence avoids promising `ok` before G9 runs on the dense cloud.

## 8. Mobile browser constraints and resulting choices

| Constraint | Consequence for the design |
| --- | --- |
| `getUserMedia`, `DeviceOrientation`, Geolocation and Wake Lock all need a **secure context**. The field server is plain HTTP on a LAN IP (`run_server.sh`). | **Primary capture path is `<input type="file" accept="image/jpeg" capture="environment">`**, one photo per tap. It works over plain HTTP and returns full-resolution photos with EXIF (focal length for SfM, lens id for G3). Sensors, the live viewfinder and wake lock are progressive enhancements, enabled only when `window.isSecureContext` is true. Document the HTTPS option for `run_server.sh` (uvicorn `--ssl-keyfile/--ssl-certfile` with a locally trusted cert). |
| `getUserMedia` frames are video-resolution, have no EXIF focal, and on iOS cannot select a lens | Not used for the photos that get reconstructed |
| iOS `DeviceOrientationEvent.requestPermission()` must be called from a user gesture | "Enable compass/tilt hints" button on the setup screen. Declined → sensor checks are hidden, not failed. |
| Android may kill the backgrounded tab while the camera app is open. iOS may reload the page after memory pressure. | Every photo is written to IndexedDB before any processing. The session resumes from storage (§10). |
| A 12 MP decode to canvas is about 48 MB. iOS limits the total canvas area. | T0 decodes with `createImageBitmap(file, {resizeWidth: 900, resizeQuality: "medium"})`, with a fallback to an `<img>` plus a 900 px canvas. One photo at a time. Bitmaps are closed right after the metrics. |
| iOS may deliver HEIC from the photo library. `IMAGE_EXTS` has no `.heic`. | `accept="image/jpeg"` makes iOS transcode. The server's per-photo endpoint rejects other types with a clear message. |
| iOS strips GPS from file-input photos | GPS is not required. Geolocation API only with a secure context, advisory only. |
| Weak or no connectivity at the site | T0 and the T0 subset of T2 run offline. Uploads queue in IndexedDB and retry with backoff. The UI shows "3 photos waiting for server checks". Preview and finalize need the server. |
| Storage quota and Safari's 7-day eviction | Blobs are deleted from IndexedDB once the server acks them (sha256 match). Call `navigator.storage.persist()` when available. |
| Magnetometer error of ±10–20° near vehicles and metal | Compass values are advisory. T3 model azimuths are authoritative. Compass-to-model alignment is used only when its residual spread is < 20°. |
| Battery and thermal limits | No continuous video processing on the main path. T0 costs one 900 px decode per photo. |

## 9. API and frontend changes

### 9.1 Server: job state and storage

- New job status **`capturing`**, before `reconstructing`: `capturing ──finalize──▶ reconstructing ──▶ …` (the existing machine is unchanged).
- Job directory additions:
  ```
  capture/
    session.json        manifest (atomic write, same temp + os.replace pattern as Job.save_state)
    originals/<uuid>.jpg
    feats/<uuid>.npz    SIFT keypoints/descriptors at 1000 px (T1 cache, for overlap checks)
    preview/            preview SfM workdir (never shared with work/)
  photos/               created empty at session start, so load_persisted_jobs reattaches the job
  ```
- `session.json`:
  `{version, marker_side_m, marker_dict, roi: {photo, polygon_px}|null, photos: [{uuid, seq, arm: "A"|"T"|"B"|"R<n>", sha256, client: {lap, clip, dhash, exif_focal, lens, heading, pitch, t}, server: {lap, clip, n_feat, overlap_prev: {inliers, h_ratio}, marker: {id, side_px, side_ratio, clipped}|null}, verdict: "ok"|"warn"|"fail", reasons: [ids], excluded: bool}], sequence: {…T2 results…}, preview: {status, started, finished, n_photos, report|error}, finalized: {at, override, blocking_at_finalize: [ids]}|null}`
- `load_persisted_jobs`: `capturing` passes through unchanged (it only remaps the busy statuses). A preview interrupted by a restart is marked `preview.status = "stale"`.
- `delete_job`: also refuse while `preview.status == "running"`.
- `list_jobs`: for `capturing` jobs, `n_photos` counts the non-excluded manifest entries.

### 9.2 New endpoints (`server/routes.py`, models in `server/schemas.py`)

| Method & path | Body | Response | Notes |
| --- | --- | --- | --- |
| `POST /api/jobs/capture` | `{marker_side_m, marker_dict?: "auto"}` | `{id}` | Creates a `capturing` job |
| `PUT /api/jobs/{id}/capture/photos/{uuid}` | multipart `file` + form `meta` (JSON: seq, arm, client metrics, sensors) | `{verdict, reasons:[{id, text}], server:{…}, sequence:{…}}` | **Idempotent on `uuid` + sha256:** a re-send returns the stored result. T1 runs in `run_in_threadpool` behind a `threading.Semaphore(2)`, so per-photo checks cannot starve the SfM workers. 409 unless status is `capturing`. |
| `DELETE /api/jobs/{id}/capture/photos/{uuid}` | — | `{excluded: true}` | Soft exclude (retake replaces). The sequence is recomputed. |
| `PUT /api/jobs/{id}/capture/roi` | `{photo_uuid, polygon: [[x,y],…]}` | `{ok: true}` | Rough ROI in original-photo px |
| `POST /api/jobs/{id}/capture/preview` | `{}` | `{queued: true}` or `{running: true}` | One preview in flight per job. A request during a run sets `rerun_after = true`. |
| `GET /api/jobs/{id}/capture` | — | full `session.json` minus blobs | Resume source of truth |
| `POST /api/jobs/{id}/capture/finalize` | `{override?: false}` | `{queued: true}` or 409 `{blocking: [{id, text}]}` | Copies the non-excluded originals in `seq` order into a temp dir named `{seq:03d}_{uuid}.jpg`, then calls the unchanged `import_photos` → `photos/`, sets `reconstructing`, and calls `executor.submit_job(job, worker.run_reconstruction, …)`, the same path as `create_job`. Stores the final preview report on the job for §9.3. |

**Worker:** `worker.run_preview(job_id, photos_dir, work_dir, queue, capture_dir, originals_order, marker_side_m, roi)`. `submit_job` always passes `photos/` and `work/`, and `run_preview` ignores them. It stages a symlink or copy of the originals, runs one SfM attempt, and returns the report dict.

**SfM change (minimal):** add `max_attempts: int | None = None` to `sfm.reconstruct`, which slices `_build_attempts(...)`. Preview calls `reconstruct(staged, capture/preview, max_image_size=1000, max_attempts=1)`. There is no other SfM change.

**New module `landslide/capture.py`** (no web dependencies; unit-testable):
- `image_checks(path, prev_feats, side_px_rules)` → T1 dict. Reuses `_photo_metrics`, `detect_marker_corners` and `_load_gray`.
- `sequence_checks(manifest)` → T2 dict.
- `preview_report(ctx, roi, marker_side_m, arms)` → T3 dict. Reuses `_camera_center_collinearity`, `_focal_spread`, `estimate_up`, `select_pairs`, `aruco_scale`, `ground_basis` and `points_in_polygon`, plus the §5 coverage map.
- `recapture_instructions(report)` → a list of instructions.
- Imports `RULES` (id → gate, class, severity, text) from `landslide/gates.py` (§7, §7a). It does not define its own copy.

### 9.3 Result-side changes (G8)

- `evaluate_gates` keeps its return signature. Add a parallel `reason_ids` list (for example `"G6_rejected"`, ids in §7a.1) to `res`. This change is additive, so old results without it still render.
- Add `res["measurement_path"]` ∈ {`rim`, `dem`, `indicative_only`, `capture_failure`}, computed by the §7a.1 rules in `pipeline.measure` right after `evaluate_gates`. Additive. It changes no status.
- Add `res["datum_holdout_missing_deg"]`: the centre azimuths (in the datum's (u, v) frame) of the holdout windows that `_datum_holdout` skipped. Additive, detection only, so `G9_rim_partial` can name the missing side. `datum_holdout_m` and `datum_holdout_windows` are unchanged.
- `result.js`:
  - For each reason id, show the §7 "next time" instruction under the reason.
  - When `result.capture_report` exists, show a "capture" row: arms, separation, median elevation, marker views, preview coverage estimate.
  - Show a visible "finalized with override" label when that applies.
- Legacy results (no `status`) get the label "pre-gate result — not checked" (closes audit §3.10 note).

### 9.4 Frontend

- `index.html`: step 1 gets two entry buttons, **"Guided capture (recommended)"** and **"Import photos"** (the existing file input, unchanged). A new `<section id="step-capture">` holds these parts:
  - Setup: marker checklist (§6), printed side input, optional "enable compass/tilt hints" button.
  - Arm progress bar with counters (A / transition / B / recapture).
  - A large "Take photo" button: the `<input capture>` behind a styled label.
  - Last-photo card: thumbnail, T0 chips (instant), T1 chips (spinner, then result), one-line instruction, Retake / Keep.
  - Sequence panel: T2 counters and warnings.
  - Preview panel: status, top-down sketch canvas (§5.4), recapture list, predicted G-class per gate.
  - "Finish and reconstruct" button: disabled while blocking items exist, with a "Finish anyway (expert)" link that sends `override`.
- New modules, following the T3.2 pattern:
  - `js/capture_metrics.js`: **pure**, no DOM. Laplacian variance, rail clip and 9×8 area-average dHash on a grey `Float32Array` (matching `cv2.resize(arr, (9, 8))` INTER_LINEAR is not exact, so parity is defined by tolerance, §11), EXIF focal and lens parsing from the JPEG APP1 segment (about 60 lines, no library), and circular heading statistics. Node-testable.
  - `js/capture_store.js`: IndexedDB wrapper for the session record plus the pending-blob queue, with `put`, `listPending`, `ack` and `clear`.
  - `js/steps/capture.js`: UI flow and upload queue with retry/backoff, and polling of `GET …/capture` while a preview runs, at the same 1.2 s cadence as `upload.poll`.
- `state.js`: add a `capture` object (`{jobId, arm, seq, pendingCount}`) and `LS_CAPTURE_KEY = "lsv-capture"`.
- `capture.html`: keep it. Change its intro to say "live viewfinder coach (needs HTTPS)" and link to guided capture.

## 10. State persistence and interrupted-capture recovery

**Principle:** the server manifest is the truth for accepted photos. The client IndexedDB holds only what the server has not acked. Photo identity is a client-generated UUID plus sha256, so every retry is idempotent.

| Interruption | Recovery |
| --- | --- |
| Tab killed while the camera app is open (Android) | The `change` handler writes the blob plus metadata to IndexedDB **first**. On reload: `localStorage[LS_CAPTURE_KEY]` → `GET …/capture` → re-queue any IndexedDB entries whose uuid is missing from the manifest → return to the same arm and seq. A photo taken but never returned to the page is lost. The user sees the count and retakes. |
| Page refresh or reopen | Same path as above. Resumes at the arm and step derived from the manifest (last arm with photos, preview state). |
| Upload fails or the network drops | The queue retries with exponential backoff (2 s → 60 s cap). The UI keeps capturing on T0 alone. The pending count is shown. |
| Duplicate send (retry after a lost response) | `PUT …/photos/{uuid}` with the same sha256 returns the stored result. A different sha256 for the same uuid → 409. |
| Server restart during capture | `capturing` persists (manifest plus `state.json`). A preview in flight is marked `stale`, and the UI offers "re-run preview". |
| Server restart during preview | Same as above. The preview workdir is disposable. |
| Two devices or tabs on one session | Last write wins per uuid. `seq` is assigned by the server at first receipt, so ordering stays total. Not a supported workflow. The UI warns if the manifest's seq max exceeds the local seq. |
| Finalize interrupted | Finalize writes `finalized` to the manifest **after** `import_photos` succeeds and **before** submit. On restart, a job in `reconstructing` without a model becomes `error` by the existing `load_persisted_jobs` rule. A new "retry reconstruction" action re-runs finalize from `capture/originals`. The originals stay until the job is deleted. |

## 11. Objective acceptance criteria

Each is a runnable check. **Test file names are proposals.**

1. **Metric parity (T0 vs server).** On `tests/fixtures/capture/*.jpg` (about 10 real phone JPEGs, plus blurred, clipped and duplicate variants generated from them), `capture_metrics.js` and `_photo_metrics` agree: clip |Δ| ≤ 0.01, Laplacian variance relative Δ ≤ 10 %, dHash Hamming ≤ 2, and identical fail/ok verdicts under the §4.1 rules. Test: `tests/test_capture_metrics.mjs` against a JSON of Python outputs produced by a fixture script.
2. **Per-image check correctness.** On synthetic fixtures derived from `tools/synth.py` renders, each check fires with no false fail on the clean `arc` frames:
   - Gaussian blur σ = 3 → blur fail.
   - Same eye with 10° yaw only → parallax warn (pivot).
   - Consecutive `sparse8` frames → overlap ok. Frames 3 steps apart → overlap fail.
   - Marker on `arc` frames → ok. `nadir` frames → side ratio < 0.4 → edge-on fail.
   - EXIF focal changed on one frame → lens fail.
   Test: `tests/test_capture.py`.
3. **Preset replay (the core criterion).** Feed each preset's images in order through `PUT …/photos`, then preview. The arm label comes from preset metadata (`arc2` splits at n/2).
   - `collinear` → blocked with the G4 id.
   - `oblique60` → blocked with the marker < 2 views id.
   - `nadir` → blocked by marker spread (G2), or warned for elevation or nadir.
   - `descending` → elevation warn plus predicted G6 `rejected`.
   - `sparse8` → count warn plus predicted G6 `rejected`.
   - `arc`, `lowtex`, `distorted` → "single azimuth: add arm B" plus predicted G6 `indicative`.
   - `twosided` → no blocking item. Elevation warn (16.5° < 20°), predicted G6 `indicative`.
   - `twosided_b` → no blocking item, predicted G6 `ok`. Datum estimate: unstable once §12b.6 calibrates it.
   - `twosided_cal` (§12b) → no blocking item, predicted G6 `ok`, no datum message.
   - **False-accept = 0:** no preset whose final status is `rejected` finalizes without a blocking item or predicted-`rejected` warning.
   - **False-block = 0 on `twosided_b` and `twosided_cal`.** A datum estimate never counts as a block.
   Test: `tests/test_e2e_capture.py` (slow, `e2e` marker, excluded from the fast suite).
4. **Predicted vs actual G6.** Over the 9 scale-able presets (the 7 original ones plus `twosided_b` and `twosided_cal`), the §5.3 calibrated class equals the final G6 class on ≥ 8 of 9. It is never more optimistic than the final class.
5. **Latency.**
   - T0 ≤ 300 ms per photo, measured on a mid-range Android phone (Chrome) and an iPhone (Safari) with 12 MP photos.
   - T1 ≤ 2 s p95 per photo on the target field laptop with 1 worker.
   - T3 ≤ 60 s for 30 photos at 1000 px on a 4-core laptop.
   - Record the numbers in `GUIDED_CAPTURE_PROGRESS.md`.
6. **Recovery.** Tests with TestClient and manifest fixtures:
   - (a) The same uuid PUT twice → one manifest entry.
   - (b) The same uuid with a different sha256 → 409.
   - (c) `load_persisted_jobs` on a `capturing` job keeps its status and photos. A running preview becomes `stale`.
   - (d) Finalize after a simulated crash between `import_photos` and submit is recoverable via retry.
   - (e) A manual device test: kill the tab after 3 shots, reopen, see all 3 plus the correct arm and seq.
7. **No regression.**
   - `pytest -q -k "not e2e"` stays green.
   - The existing `POST /api/jobs` multi-file path is byte-for-byte unchanged in behaviour.
   - Legacy saved jobs load.
8. **Field criterion (links to audit F1 and §6 items 1–3).** On ≥ 3 real sites, a guided two-arm capture reaches final `indicative` or `ok` with no `rejected`. The guided session's predicted G-classes match the final ones. At least one site reaches `ok` (path 1). At least one site has an independent pre-event DEM: measure it on path 2 as well, and record the path-1 vs path-2 difference. This is the only check of the radially symmetric blind spot (§3a.1). This is required before claiming that guided capture "prevents weak captures" outside synthetic data.
9. **Path classification.** `res["measurement_path"]` (§7a.1) on the final results of the replayed presets:
   - `arc`, `lowtex`, `distorted`, `collinear`, `sparse8`, `descending`, `twosided` → `capture_failure`. `twosided` also shows the combined G6 + `G9_datum_unstable` message.
   - `twosided_b` → `indicative_only`, with `G9_datum_unstable` as its only reason id.
   - `twosided_b` re-measured with a DEM of the true pre-slide surface (§12b.5 item 10) → `dem`.
   - `twosided_cal` → `rim`, `status == "ok"`.
   - Rule 5 of §7a.1 (fallback) is never reached.
   - Fast tests on `evaluate_gates` fixtures: each G9 branch maps to its id and class. R5 (one-sided rim) → `G9_rim_partial`, `capture_failure`. R1 (seed-7 perfect points) → `G9_datum_unstable`, `indicative_only`. The §12b.4 fixture → `rim`.

## 12. Staged implementation order

Each stage can ship and be tested on its own. Stop after any stage and the product is still consistent.

**Done:**

| Stage | Result |
| --- | --- |
| **S0 Calibration data** | `twosided` (90°): `indicative`, G6 void 9.1–10.1 m². NO-GO recorded (`GUIDED_CAPTURE_PROGRESS.md`). |
| **S0b Protocol correction** | `twosided_b`: every capture criterion passes, accuracy +15–17 %. NO-GO. The cause was re-attributed to the seed-7 datum (§12a, `DATUM_STABILITY_PLAN.md`). |
| **G9 Datum stability** | `9bb999d`. `twosided_b` → `indicative` (G9). Detection only, no volume change. |

**Revised order (post-G9).** The old rule "S1–S6 blocked until S0b passes" is replaced. S0b cannot pass on seed-7 terrain with any capture (`DATUM_STABILITY_PLAN.md` §7.2). Only the stages whose exit checks need a path-1 reference scene now wait for S0c.

| # | Stage | Scope | Depends on | Exit check |
| --- | --- | --- | --- | --- |
| 1 | **S0c Calibration scene** | §12b: opt-in terrain octaves in `tools/synth.py`, `twosided_cal` preset, benchmark terrain fix, perfect-point pre-check, one SfM run, DEM-path run on `twosided_b` | G9 | §12b.5, under the §12b.7 decision rule |
| 2 | **S6a Result-side paths and ids** | `reason_ids`, `RULES` in `gates.py`, `measurement_path`, `datum_holdout_missing_deg`, §7a messages in `result.js`, "Upload pre-event DEM" action on path 3 (existing endpoint), legacy label. Missing G9 tests from `DATUM_STABILITY_PLAN.md` §8: R5 (one-sided rim), R6 (pure `evaluate_gates` mapping), R8 (small pile on a curved road). | G9 only | Criterion 9 (fast part), criterion 7. No status or volume changes on any fixture. |
| 3 | **S1 Instant checks in the existing upload** | `capture_metrics.js` plus parity fixtures. Per-file T0 verdicts and EXIF lens check shown in the upload step **before** upload. No new API. | — | Criteria 1 and 7 |
| 4 | **S2 Capture session backend** | `capturing` status, `session.json`, `PUT/DELETE photos`, `GET capture`, `finalize` (no preview gating yet), `landslide/capture.py::image_checks` / `sequence_checks`, semaphore. Setup stores the DEM answer (§3b step 1) in `session.json`. | S1 | Criteria 2, 6(a–d), 7 |
| 5 | **S3 Guided UI and persistence** | `step-capture` section, `capture_store.js` IndexedDB queue, arms and counters (including the 2 outer-end photos), rim-check step, marker checklist and chips, optional sensors on a secure context | S2 | Criteria 5 (T0/T1), 6(e) |
| 6 | **S4 Preview SfM** | `reconstruct(max_attempts=…)`, `worker.run_preview`, `preview_report` (registration, collinearity, focal, elevation, azimuth, stereo pairs, marker scale, datum estimate §3a.4), finalize blocking plus override | S2, **S0c** | Criteria 3 (except the G6 prediction), 5 (T3). Datum estimate calibrated per §12b.6. |
| 7 | **S5 Coverage and recapture** | ROI tap, §5 cell map, calibrated `c_rej`/`c_ok` (now including `twosided_b` and `twosided_cal`), directional instructions including the outer-end rule (§5.4), top-down sketch, marker px thresholds calibrated on real prints | S4, **S0c** | Criteria 3 (full), 4 |
| 8 | **S6b Capture report on the result** | `capture_report` row, "finalized with override" label | S4 | UI smoke: each replayed preset shows its path and matching instruction |
| 9 | **S7 Device and field validation** | Android Chrome and iOS Safari over HTTP (no sensors) and HTTPS (sensors). ≥ 3 real sites, one with a pre-event DEM. | S5, S6b | Criterion 8 |

Why this order:
- **S0c first.** It is cheap (one ~13 min SfM run plus about 5 min of cached re-measures) and is the only stage whose result can change the design. If it fails, S4 and S5 have no path-1 reference and stay blocked. Every other stage still ships.
- **S6a second.** It needs only the gates that exist. It fixes the largest user-facing gap today: a G9 result looks the same as a capture failure, and nothing offers the DEM path. It also defines the ids and `RULES` that S2–S5 reuse.
- **S1 → S2 → S3** do not depend on any scene. They follow the original order.
- **S4 → S5** need S0c: criterion 3 needs a preset whose final status is `ok` to prove false-block = 0, and §5.3 needs it to fit `c_ok`.
- **S6b** needs the preview report from S4.

S4 is still the largest single cost and risk.

## 12a. S0b — correction plan for the `twosided` void

**Status:** analysis done (read-only), validation not run. No application code or `tools/` file was changed by S0b. §3 is **not** edited yet. The §12a.3 protocol changes go into §3 only after §12a.6 passes.

### 12a.1 What S0 measured

`twosided` (`tools/synth.py:79`, `arc2`, 2 × 15 views, arms centred at yaw 0° and 90°, each 96° wide, radius 26 m, height 7 m): 30/30 registered, scale error 1.64 %, volume error 0.90–1.39 %, coverage 0.867 (photo) / 0.878 (ortho). Status `indicative` only because `largest_void_m2` = 10.1 / 9.1 m² > 2.0 (`gates.py:92`). Details: `GUIDED_CAPTURE_PROGRESS.md`.

Yaw convention below (same as `synth.view_poses`): 0° = camera south of the bowl, 90° = east, 180° = north. The union of both arms covers yaw −48°…138° (186°). The gap 138°…312° (174°, NW half) has no camera.

### 12a.2 Root cause

**Method.** The cached dense cloud (`data/bench/twosided/work/dense_1280_v2_*.npz`) was mapped into the ground-truth frame with the benchmark's own `_umeyama_pose_aware` fit (camera residual ≤ 0.12 m). Then it was gridded at 0.25 m inside the true polygon, the same grid as `_coverage_gate`. This reproduces S0: coverage 0.869, largest void 9.4 m² (the pipeline's 9.1–10.1 m² comes from its traced polygon and basis). For each void cell, the angle of each camera ray above the local surface plane ("grazing") and occlusion were computed on the exact synthetic heightfield (`synth.build_terrain(seed=7)`).

There are **two** voids above 2 m², not one. Closing only the largest one still leaves S0 `indicative`.

| | Void 1 (largest) | Void 2 (second) |
| --- | --- | --- |
| Area | 9.4 m² (reported 9.1–10.1) | 3.4 m² |
| Location | Crescent on the SE inner wall. r = 3.9–5.6 m from the bowl centre (wall to polygon edge). Yaw 3°–95°, centred at 50°. | Bowl floor just south of the centre, r = 0.5–2.5 m, yaw −77°…57° |
| Surface | Slope 20–28°. The downslope face points to yaw 190–270° (NW), the middle of the camera gap. | Slope ~18°, faces yaw 170–225° (N) |
| Arm A view | Grazing falls from 22° (the westmost camera) to 0° (the camera at the arm junction). Only 2 cameras reach ≥ 18°. | 86 % of rays are occluded by the near (south) rim |
| Arm B view | Grazing 0–4° at the junction end, rising to 15° at the north end. No camera reaches 18°. | 97 % of rays are occluded |
| Mechanism | **Elevation × azimuth.** The camera elevation (16.5° above the ROI centroid, 19.2° above the bowl bottom) is below the wall slope (up to 27.6°). A near wall is visible only from across the bowl. The wall at the bisector of the two arms faces the centre of the 174° gap, about 87° from both arm centres. Only the 2–3 cameras at each arm's **outer end** see it at a usable angle. The dense fusion needs about 3 cameras from one arm, because stereo pairs are intra-arm (`convergence_deg` 4–35°). | **Elevation.** The ray from 16.5° elevation passes below the near rim before it reaches the floor: tan(19°) · 4.5 m ≈ 1.55 m, while the floor sits 1.7 m below the rim. Classic rim shadow. |

Hypotheses from the S0b request, one by one:

| Hypothesis | Verdict |
| --- | --- |
| Camera elevation | **Primary cause of both voids.** The S0 preset sits at 16.5° above the ROI centroid. That is below this plan's own ≥ 20° protocol rule (§3 step 3; step 2 before the post-G9 renumbering) and the §4.3 T3 warn. So S0 tested a non-compliant capture. |
| Path endpoints | **Contributing (void 1).** Void 1 is seen only by the outermost cameras of each arm, and by too few of them. |
| Azimuth separation | **Contributing (void 1).** At 90° the arm bisector (yaw 45°) is the camera-side wall, which faces the gap centre (~225°). |
| Surface occlusion | **Void 2 only** (near-rim shadow). Void 1 is back-facing or grazing, not occluded (≤ 12 % occluded rays). |
| Stereo-pair geometry | Not the cause. Pairs exist. Void cells lack *views*, not pairs. |
| Missing close-range views | Not the cause. Close views from the camera side see the same back-facing wall. Modelled: 5 views at 12 m from yaw 45° still leave a 4.0–7.6 m² void. |

### 12a.3 Option comparison

**Proxy.** A cell counts as observed if, within at least one arm, the 3rd-best camera has grazing ≥ θ. The camera must be unoccluded and the cell in frame. This models "≥ 3 fused views from one arm". Calibration against the real dense clouds:

| θ | arc (real 0.60 / 39 m²) | descending (real 0.41 / 58 m²) | twosided (real 0.87 / 9.1–10.1 m²) |
| --- | --- | --- | --- |
| 18° | 0.71 / 28.0 | 0.58 / 41.1 | **0.87 / 10.9** |
| 20° | 0.66 / 33.1 | 0.55 / 44.6 | 0.83 / 16.4 |
| 22° | **0.63 / 36.4** | **0.51 / 46.8** | 0.79 / 19.8 |

θ = 18° fits the two-arm case, and θ = 22° fits single arcs. The truth for a new geometry is taken to lie in that band. **A candidate passes only if its predicted largest void is ≤ 2 m² at θ = 18, 20 and 22°.** θ = 24° is reported as a stress margin. The proxy is a ranking tool, not a substitute for the benchmark. The scripts were run from a scratchpad and are not committed. Rebuilding them is about 80 lines on `synth` plus `scipy.ndimage.label`.

Predicted coverage / largest void (m²) at θ = 18 / 20 / 22°. The marker count is the number of views per arm where the board is in frame and faces the camera at < 60°:

| Option | Photos | Marker views A / B | θ 18° | θ 20° | θ 22° | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| S0 baseline | 30 | 15 / 5 | 0.87 / 10.9 | 0.83 / 16.4 | 0.79 / 19.8 | fails |
| Extend arm A outward +4 (to −75°) | 34 | 18 / 5 | 0.97 / 1.8 | 0.94 / 2.6 | 0.89 / 10.6 | fails |
| Extend arm A outward +6 | 36 | 18 / 5 | 0.98 / 1.4 | 0.96 / 1.8 | 0.93 / 4.4 | fails at 22° |
| Extend arm B outward +4 | 34 | 15 / 5 | 0.96 / 2.9 | 0.92 / 3.2 | 0.87 / 11.4 | fails |
| Separation 110° | 30 | 15 / 2 | 0.93 / 3.2 | 0.88 / 11.3 | 0.84 / 15.5 | fails |
| Separation 120° | 30 | 15 / **1** | 0.96 / 2.9 | 0.92 / 3.2 | 0.87 / 11.2 | fails, and marker < 2 views in B |
| Height +2 m only (9 m) | 30 | 15 / 5 | 0.99 / 1.1 | 0.95 / 4.4 | 0.89 / 10.4 | fails |
| Height +3 m only (10 m) | 30 | 15 / 5 | 1.00 / 0.0 | 0.98 / 1.5 | 0.95 / 4.5 | fails at 22° |
| Radius 22 m (closer, same height) | 30 | 15 / 6 | 0.98 / 1.5 | 0.93 / 6.1 | 0.86 / 12.3 | fails |
| Close-range 5 views, yaw 45°, r 12 m | 35 | 15 / 5 / 0 | 0.95 / 4.0 | 0.93 / 5.6 | 0.91 / 7.6 | fails |
| Mini-pass 6 views at yaw 225° (opposite the void) | 36 | 15 / 5 / 0 | 0.99 / 0.6 | 0.98 / 0.9 | 0.96 / 1.2 | passes the proxy, **but cannot register** (§ below) |
| Third full arm at 180° | 45 | 15 / 5 / 0 | 0.99 / 0.9 | 0.98 / 1.1 | 0.96 / 1.2 | passes, +15 photos, no marker in arm 3 |
| **Height 9 m + 2 photos at each outer arm end** | **34 in the field (38 in the preset, §12a.4)** | 19 / 7 | **1.00 / 0.0** | **1.00 / 0.0** | **0.99 / 0.2** | **passes. θ 24°: 0.93 / 4.4** |
| Height 9 m + 1 photo at each outer end | 32 | — | 1.00 / 0.1 | 0.99 / 0.5 | 0.94 / 4.2 | fails at 22° |

Why the "mini-pass opposite the void" is not the answer, even though its proxy is best: yaw 225° is 87° from the nearest arm end (138° and 312°). `sparse8` showed that 8° per step is the widest step that still registers (`tools/synth.py` sparse8 comment). An isolated mini-pass there does not join the model. Reaching it means walking about 87° of connecting arc, roughly 12 more photos. That is the "extend an arm by ~90°" option at a higher cost than the recommended change. It also puts the user on the far side of the slide, which is often the scarp or unsafe ground.

### 12a.4 Chosen change (smallest that passes)

Two protocol rules, both needed. Neither one alone passes (see the table):

1. **Elevation ≥ 20° above the ROI centroid for every photo.** This is the existing §3 rule, now stated in the T3 metric, which is the median angle of camera centres above the ROI centroid (§4.3). S0 was at 16.5°. In the field, get this from higher ground or by standing closer. At a fixed height, 26 m → 21 m gives the same angle, and a 12 m slide still fits in frame at 1× zoom. It is also what closes void 2 (the rim shadow).
2. **Walk each arm about 14° further *away* from the other arm: 2 more photos at each arm's outer end, at the same step.** The combined azimuth span grows from 186° to about 214°. The camera gap shrinks from 174° to about 147°. This closes void 1: the outer ends are the only cameras that see the bisector wall, and they now come in groups of 3 or more. Same walk, no new vantage. The user just keeps walking.

Marker: one board, unchanged. Marker views per arm go from 15 / 5 to 19 / 7, because the wider arms swing further toward face-on the south-facing board. Board side in stored px is 58–92 at a 2 m board (the synthetic board). The ≥ 5 total and ≥ 2 per arm rule (§3 step 6; step 5 before the post-G9 renumbering) holds with margin. Separation stays at 90°. Wider separation loses arm B's marker views (120° → 1 view) and does not close void 1.

Field cost: +4 photos (30 → 34, still inside the 25–35 target), about 1 minute of extra walking, and one new instruction ("stand high enough / come closer"). That instruction is already in the T0 pitch proxy and the T3 elevation check.

Not changed: G6 thresholds (0.85 / 2 m² / 0.6), all other gates, marker rules, and arm separation.

**Corrected preset (to add to `tools/synth.py` `PRESETS` when S0b validation runs; dict entry only, `view_poses` unchanged):**

```python
# S0b (GUIDED_CAPTURE_PLAN.md §12a): twosided + the two protocol fixes.
# height 9 m -> 20.5 deg above the ROI centroid (S0's 7 m gave 16.5 deg, below
# the protocol's 20 deg). Each arm 2 views wider at each end (6.86 deg step kept):
# arc2 widens arms symmetrically, so 2 of the 4 extra views per arm land in the
# inter-arm overlap and are protocol-neutral; the field protocol adds only the
# 2 outer-end views per arm (34 photos total).
"twosided_b": dict(path="arc2", n_views=38, yaw_span=123.43, radius=26.0,
                    height=9.0, height_ramp=0.0, f_px=1600.0, tex_scale=1.0,
                    k1=0.0, arc_offset=90.0),
```

`arc2`'s `middle` is (38 // 2) // 2 = 9, arm A index 9 at yaw 0°, so the GT polygon framing matches `twosided`. The proxy predicts the same result for 38 and 34 views, because the 4 inner-end views sit in already-covered overlap. The preset uses the symmetric form so that `synth.py` needs no path code.

### 12a.5 Objective acceptance criteria (S0b exit)

On `data/bench/twosided_b`, clean cache, default settings. Every item must hold in **both** photo and ortho mode:

1. Registration 38/38.
2. `status == "ok"`. That means `coverage_frac ≥ 0.85` **and** `largest_void_m2 ≤ 2.0`, read from the `measure()` result dict. No gate threshold is changed or overridden.
3. Second-largest void ≤ 2.0 m². This proves void 2 closed as well, and that void 1 did not just split.
4. No other reason fires: G1–G5, up disagreement < 20°, and `aruco_scale` without warnings.
5. Accuracy is no worse than S0: scale error ≤ 3 %, and `cut_measured_m3` within ±5 % of the 67.16 m³ truth.
6. Marker: `aruco_scale` `views_used` ≥ 5, including ≥ 2 from each arm (images 0–18 and 19–37).
7. Median camera elevation above the ROI centroid ≥ 20°, computed from the GT poses.
8. `pytest -q -k "not e2e"` stays green. Adding a preset must not change any other preset's output (`arc` byte-identical).

### 12a.6 Focused validation (one run)

1. Add the `twosided_b` dict entry (§12a.4) to `tools/synth.py`. This is a tool file, not application code.
2. `.venv/bin/python -m tools.benchmark --presets twosided_b --out data/bench --md-out /tmp/twosided_b_bench.md`. Expect about 11–13 min and < 2 GB RSS (S0: 540 s and 1.6 GB for 30 views).
3. Read `status`, `reasons`, `coverage_frac` and `largest_void_m2` from `measure()` on the cached work dir, in photo and ortho mode, the same way S0 did (`GUIDED_CAPTURE_PROGRESS.md`). Also label the 0.25 m occupancy grid and record the top 3 voids with their centroid, radius and yaw, as in §12a.2.
4. Record the results in `GUIDED_CAPTURE_PROGRESS.md` under "S0b".

**Decision rule, fixed in advance (no tuning to the result):**
- All of §12a.5 passes → apply §12a.4 to §3 (elevation rule wording, "2 more photos past each arm's outer end", target 30–40 photos). Set `twosided_b` as the S5 calibration target in place of `twosided`. Unblock S1.
- Fails **only** because a void > 2 m² remains on the SE wall (void 1 region) → run **one** escalation, 3 photos per outer end (`n_views=42, yaw_span=137.14`; proxy θ 24° gives 2.0 m²). Then stop, whatever the result.
- A void remains on the south floor (void 2 region), or somewhere new → the elevation model is wrong. Stop and re-analyse. Do not escalate.
- Any other gate fires → stop and report. The change must not trade G6 for another gate.

### 12a.7 Consequences for the rest of the plan (apply after §12a.6 passes)

**Post-G9 status:** §12a.6 did not pass, but the only failed item (accuracy) was re-attributed to the terrain datum (`DATUM_STABILITY_PLAN.md` §1). The capture items passed. The items below are therefore applied now, **provisionally**, through §3, §4.3, §5.4 and §11. S0c (§12b) confirms or reverts them.


- §3 step 2: "elevation ≥ 20°" is measured above the ROI centroid, not the lowest debris point. Add: "walk each arm about 2 photos further away from the other arm than feels necessary."
- §4.3 elevation check: the median < 20° warn is correct as written. S0b shows it is a **G6 predictor**, not only G5. Show it together with the predicted G6 class.
- §5.4 recapture: a void on the camera-side wall faces the gap between the arms. The instruction must be "continue arm A (or B) outward by N photos from its end", with N = ceil(angular distance / 7°). It must never say "go to the far side", because an isolated far-side mini-pass does not register (§12a.3).
- §11 criterion 3: `twosided` is now expected to be predicted `indicative` (non-compliant elevation). `twosided_b` takes over the "no blocking item / false-block = 0" role.

## 12b. S0c — calibration scene whose datum the rim can predict

**Status:** design only. No `tools/` or application file is changed by this section. This is the `DATUM_STABILITY_PLAN.md` §7.3 decision, made here as a plan. It needs approval before S0c runs, because it changes `tools/synth.py`.

### 12b.1 Why a new scene is needed

Seed-7 terrain has 0.35 m value noise at about 6 m cell spacing (`build_terrain`, octave `(6, 0.35)`) plus 0.15 m at 3 m. The rim ring is 16 m across. So the ground under the bowl contains bumps the ring never sees. A perfect cloud still gives +18.2 % (`DATUM_STABILITY_PLAN.md` §2.1). On this scene no capture can pass both G9 (`ok`) and ±5 %. Every guided-capture criterion that needs an `ok` reference (§11 criteria 3, 4, 9) therefore needs a second scene. The fix must be in the scene, never in a gate.

### 12b.2 Requirements

1. The undisturbed surface is predictable from the rim band (0.5–2.5 m outside the outline) at the ±5 % target, with margin, **on any seed**, not by luck of one seed.
2. The scene is still realistic: sloped (not flat), rough at the decimetre scale, textured exactly like the benchmark scene.
3. Only the datum-relevant content changes. Cameras, bowl, truth volume, marker, texture and lens stay identical to `twosided_b`. Any change in capture results between the two scenes can then only come from the terrain.
4. Every existing preset stays byte-identical.
5. It must exercise every check guided capture relies on: capture geometry, marker visibility, reconstruction, coverage and G9, with no gate or threshold change.

### 12b.3 Design: `twosided_cal`

**Terrain.** Keep the planar tilt `0.10·x + 0.03·y` (5.96° slope, the benchmark's own). Keep the two short-wavelength octaves: `(24 cells, 0.08 m)` at 1.5 m spacing and `(48 cells, 0.04 m)` at 0.75 m spacing, which give up to 0.12 m of roughness. **Set the two long-wavelength octaves to zero**: `(6, 0.35)` and `(12, 0.15)`. Their cell spacings (6 m and 3 m) are the scale of the interior itself, so the rim cannot predict them. The real analogue is a planar hillslope, a road cut or an embankment: the sites where a rim datum is physically justified.

**Everything else is unchanged:** the cosine bowl (R 6 m, depth 2 m), so the truth stays 67.16 m³ inside the 5.6 m polygon (`vol_true_poly` depends only on the bowl); the ArUco board (2 m side, vertical, 7.5 m north of the bowl centre, inside the rim band, as in every preset); the colour texture; and the `twosided_b` cameras (38 views, 2 arms at 90°, 123.43° each, radius 26 m, height 9 m, f 1600 px).

**Implementation (S0c, not now):**
- `build_terrain(seed=7, tex_scale=1.0, octave_amps=(0.35, 0.15, 0.08, 0.04))`. The loop still calls `value_noise` for **every** octave and multiplies by the given amplitude, so the RNG stream is consumed exactly as today. The colour texture and the two kept octaves are therefore identical to the benchmark scene, and the default reproduces every existing preset byte-for-byte.
- `PRESETS["twosided_cal"]` = the `twosided_b` dict plus `terrain_amps=(0.0, 0.0, 0.08, 0.04)`. `main()` passes `p.get("terrain_amps", <default>)`. `ground_truth.json` records `terrain_amps`.
- `tools/benchmark.py` `run_preset` calls `build_terrain` again for `cloud_rms_m`. It must pass the same `terrain_amps`, or the cloud RMS is scored against the wrong surface.
- `add_marker` already places the board with `terrain_height`, so it sits on the new ground with no change.

### 12b.4 Evidence already gathered (read-only scratch run, perfect points, no SfM)

The R1 recipe (`tests/test_volume.py::test_g9_unstable_datum_downgrades_ok_to_indicative`): terrain grid points, interior r < 5.6 m, rim r 6.1–8.1 m, `prism_volume` → `evaluate_gates`. The `build_terrain` copy in the scratchpad consumed the RNG exactly as described in §12b.3. It reproduces R1 on the default amplitudes (seed 7: +13.26 %).

| Terrain octaves | seed 7: cut error / G9 ratio / status | seeds 1–40: max \|error\| | seeds 1–40: max G9 ratio | seeds 1–40 `ok` |
| --- | --- | --- | --- | --- |
| benchmark (0.35, 0.15, 0.08, 0.04) | +13.26 % / 0.079 / indicative | 13.26 % | 0.212 | 6 / 40 |
| drop 6-cell only (0, 0.15, 0.08, 0.04) | +1.65 % / 0.037 / ok (TPS) | 8.40 % | 0.097 | 23 / 40 |
| **drop 6- and 12-cell (0, 0, 0.08, 0.04)** | **−0.49 % / 0.025 / ok (plane)** | **1.36 %** | **0.025** | **40 / 40** |

G9 ratio = `datum_holdout_m × polygon_area_m2 ÷ |net_volume_m3|`. The gate fires above 0.05.

Reading: dropping only the 6-cell octave passes on seed 7 by luck (8.4 % worst case, 17 of 40 seeds still `indicative`). Dropping both long octaves meets requirement 1 on every seed with the datum error at ≤ 1.4 % and the G9 ratio at half the threshold. G9 stays silent **because** the datum is right, not because the gate is loose. The same gate still flags the benchmark terrain.

### 12b.5 Objective pass criteria (S0c exit)

Items 1–2 run before any SfM. Items 3–11 hold in **both** photo and ortho mode on `data/bench/twosided_cal`, clean cache, default settings. No gate threshold is changed or overridden.

1. **Pre-check (fast test, new).** The §12b.4 recipe on `twosided_cal`'s terrain, seed 7: `status == "ok"`, no reasons, `datum_holdout_windows == 8`, |cut error| ≤ 1.5 %, G9 ratio ≤ 0.03. This is the positive-control complement of R1 and stays in `pytest -q -k "not e2e"`. The 40-seed sweep (all `ok`, max |error| ≤ 2 %) is re-run once and recorded, not added as a test.
2. **No regression.** `arc` and `twosided_b` regenerated to a scratch dir are byte-identical (images and `ground_truth.json`) to the committed `data/bench/arc` and to a fresh pre-change `twosided_b` render. `pytest -q -k "not e2e"` green: 185 + new, 3 skipped.
3. **Capture geometry.** Registration 38/38. G4 silent (collinearity ≥ 0.10). Median camera elevation above the ROI centroid ≥ 20° (GT poses). Arm separation measured in the model 80–100°.
4. **Marker visibility.** `aruco_scale` raises nothing and warns nothing. `views_used` ≥ 5, with ≥ 2 from each arm (images 0–18 and 19–37). `side_spread_rel` ≤ 5 %. Record `pnp_scale_spread`.
5. **Reconstruction.** G1 silent (dense ≥ 20 000 points). Dense residual against the true terrain (the benchmark's `_umeyama_pose_aware` mapping, as in S0b): inside r < 5.6 m, RMS ≤ 0.05 m and |mean| ≤ 0.02 m; in the rim band outside the board footprint, |median| ≤ 0.02 m. (`twosided_b`: 0.026 m RMS inside, +0.006 m rim median.)
6. **Coverage.** G6 silent: `coverage_frac` ≥ 0.85, `largest_void_m2` ≤ 2.0, second-largest void ≤ 2.0 m². `area_m2 ÷ polygon_area_m2` ≥ 0.97, so no unmeasured area can cancel a bias as in S0.
7. **G9.** `datum` ∈ {`rim_plane`, `rim_quad`, `rim_tps`}, `datum_holdout_windows == 8`, G9 ratio ≤ 0.04. The 0.04 is a scene-validity margin (80 % of the gate) so that SfM run-to-run noise cannot flip S4/S5 replay results. It is not a gate change.
8. **Status.** `status == "ok"`, `reasons == []`. Up-vector disagreement < 20°. Photo mode: `hit_frac` ≥ 0.85, `max_miss_run` ≤ 3. If S6a has shipped: `measurement_path == "rim"`.
9. **Accuracy.** Scale error ≤ 3 %. `cut_measured_m3` within ±5 % of 67.16 m³ (the unchanged §12a.5 target). Scale-removed cut, `cut ÷ (s_est ÷ s_true)³`, within ±2 %. This separates the capture/datum error from the scale budget (`DATUM_STABILITY_PLAN.md` §7.4).
10. **Path 2 on the scene path 1 cannot measure.** On cached `data/bench/twosided_b` (no new SfM): write the true pre-slide surface (`z + bowl` from `build_terrain(7)`, without the board) as an XYZ file in the scratchpad. Run `align_to_dem`, then `measure(dem=…)`. Pass: alignment `rms_m` ≤ 0.05 m; `datum == "dem"`; `status == "indicative"` with the DEM-mode reason as the **only** reason (no G9, because it does not apply to `dem`); |cut error| ≤ 5 %; scale-removed error ≤ 2 %.
11. **Negative control.** `twosided_b` re-measured after the `tools/synth.py` change: `indicative` with the G9 reason, and every number equal to the `DATUM_STABILITY_PROGRESS.md` table to 1e-6 relative.

### 12b.6 Calibration of the T3 datum estimate (runs in S4, uses S0c's data)

Run the preview (§4.3) on `twosided`, `twosided_b` and `twosided_cal` images. Use the same 0.05 ratio as G9. The estimate is shown only if **both** hold: it says "unstable" on `twosided` and `twosided_b` (final G9 fires), and it says nothing on `twosided_cal` (final G9 silent). If either fails, the T3 datum estimate is **dropped from the UI**, not re-tuned. The result-side path (§7a) still works without it.

### 12b.7 Decision rule (fixed in advance)

- Item 1 fails → stop before SfM. Do not adjust amplitudes to fit. Report the result.
- Item 2 fails → revert the `tools/synth.py` change. The design is wrong.
- All items pass → **S0c GO.** `twosided_cal` becomes the S4/S5 path-1 reference and replaces `twosided_b` in that role. §3 drops "provisional". S4 is unblocked.
- Item 1 passes but item 7 fails on the real run → SfM adds datum error that perfect points do not show. Stop and analyse the rim reconstruction. This is new evidence. No gate or threshold changes.
- Item 6 fails → the §3 capture geometry does not transfer across terrain. Stop. §3 stays provisional, and §12a is re-opened. No escalation run.
- Only item 9 fails and its scale-removed part passes → the scale budget decides the ±5 %. Record it as a scale item. S0c is NO-GO for accuracy. Do not change the scene.
- Only item 10 fails → path 1 is validated (S4/S5 unblocked). Open a path-2 defect. S6a ships the "Upload pre-event DEM" action only after it is fixed.
- Any other gate fires → stop and report.

### 12b.8 What this scene does not validate

- **Real marker sizes.** The synthetic board is 2 m (58–92 px). A 0.5 m field board at 26 m with a ~2200 px phone focal is about 40 px: the warn band of §4.1. S5/S7 calibrate this on real prints.
- **Real texture, light and vegetation.** S7.
- **Radially symmetric undulation** (§3a.1). By design the scene has none. Only a DEM comparison on a real site (criterion 8) checks it.
- **Curved datums.** The scene is planar plus roughness. Quad/TPS adoption is covered only by the unit fixtures (R3).
- **Path-2 status.** Item 10 validates the DEM measurement, not an `ok` on path 2. That needs the DEM gates named in §3a.2.

## 13. Deliberately out of scope

- **In-browser SfM or ArUco (WASM OpenCV):** about 10 MB of download, it would duplicate the server logic, and phone CPU and heat limits apply. Add it only if offline marker checks turn out to be the main field complaint.
- **Continuous-video capture with auto-shutter:** needs a secure context, has no EXIF, and gives video resolution. Revisit once HTTPS field serving is standard.
- **Multi-marker scale:** `aruco_scale` uses one (dict, id). This would be a scaling change, not a capture change.
- **Automatic GPS-based arm separation:** the phone GPS error of 3–10 m is comparable to the arm geometry at a 26 m radius. The preview-model azimuth is better.
- **Changing any gate threshold:** guided capture targets the gates as they are. This includes G9: a site that fails G9 is routed to path 2 or 3, never passed.
- **DEM-mode gates** (G6 in the DEM frame, alignment `rms_m`): needed for a path-2 `ok`, but they are measurement changes. They need their own plan.

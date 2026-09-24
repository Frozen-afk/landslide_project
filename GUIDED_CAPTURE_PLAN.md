# Guided Image Capture — Design

**Status:** design only. No application code is changed by this document.
**Base:** `main` @ `d5653ba`. B1–B5 are fixed (`RELEASE_BLOCKER_PROGRESS.md`). F1–F3 are open (`FINAL_RELEASE_AUDIT.md` §5).

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

**Goal:** catch these faults while the user is still on site. Check each photo as it is taken. Check the sequence as it grows. Run a cheap preview reconstruction before the expensive one. Turn every predicted gate failure into a concrete action: where to walk, what to shoot, and how to fix the marker. The final pipeline and the gates stay unchanged. Guided capture adds nothing to the measurement. It only makes sure the input can pass the gates.

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
| Two-azimuth preset | `tools/synth.py` `twosided` (`arc2`) | Calibration target. **Not benchmarked yet (V1).** Its comment says the arms are "90 deg apart", but it sets `arc_offset=180.0`. Fix this before calibration (§12, S0). |

## 3. Capture protocol the system guides toward

This protocol comes from `REMAINING_ACCURACY_PLAN.md` §77/§122 and README "Capturing good photos". It is the only protocol with a path to G6 `ok`.

1. **Marker:** ArUco board, side ≥ 0.50 m, flat and rigid. Place it on stable ground just outside the landslide rim. Tilt it toward the capture positions (§6).
2. **Arm A:** a convergent sweep of ≥ 12 photos around the slide at one vantage. Walk an arc, do not pivot on the spot. Keep 60–80 % overlap with the previous photo. Keep the camera elevation ≥ 20° above the debris. Keep the whole rim plus a margin in frame.
3. **Transition:** keep shooting every few steps while walking to the second vantage, so the two arms register into one model.
4. **Arm B:** ≥ 12 photos, with the arm centre ≥ 60° of azimuth from arm A. Recommended separation: 60–120°, so one tilted marker can face both arms (§6).
5. **Total:** ≥ 15 registered views (target 25–35). The marker must be seen in ≥ 5 photos, with ≥ 2 in each arm.
6. **Never:** shoot from below the crown (descending), walk a straight line (collinear), shoot straight down only (nadir), switch lens or zoom mid-capture (G3), or take burst duplicates.

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
| Camera elevation | `estimate_up(info=…)`, then the angle of each camera centre above the ROI centroid, in the up frame | G5 / G6 (descending) | Median < 20° or any arm < 15° → warn. Any camera below the ROI's lowest rim point → warn. |
| Up-vector agreement | `info["disagree_deg"]` | up gate (> 20°) | > 20° → warn: "include more flat, stable ground around the slide in frame" |
| Azimuth coverage | Camera azimuths about the ROI centroid in the ground basis (`ortho.ground_basis(up)`), in 8 × 45° sectors. Arm separation computed from the model. | G6 | Separation < 60° → **block**. Separation is authoritative here, unlike the compass. |
| Stereo pairs | `densify.select_pairs(ctx)` count. Share of images with ≥ 1 geometry-gated neighbour (`_pair_geometry_ok`). | G1 (dense < 20 000 points) | < 70 % of images with a pair → warn |
| Marker scale | `aruco_scale(ctx, side_m, …)` in a try block. Record `side_spread_rel`, `scale_rel_error`, `pnp_scale_spread`, `views_used`. | G2 / G7 | Raises → **block**, show its message. Spread > 5 % → warn. PnP spread > 2 % → info: "scale will carry the 3 % floor". |
| Coverage proxy | §5 | G6 | Class predicted per §5.3 |

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

This table is the single source for the instruction text. It is used in three places: the capture UI (predicted), the preview report, and the result panel (actual). `landslide/capture.py::RULES` holds the table (§9.2). The existing `evaluate_gates` reason strings are unchanged. The result panel adds the matching "what to do next time" line by matching the gate id, which requires an id per reason (§9.3).

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
| DEM mode no coverage gate (`gates.py:95-103`) | always `indicative` | none (not a capture fault) | Result panel only: "DEM mode is labelled indicative until its coverage gate exists." |
| **G7** scale floor 3 % unless PnP spread ≤ 2 % (`pipeline.py:282-285`) | wider CI | T3 `pnp_scale_spread` | "Get the marker into more photos from varied distances to tighten the scale." |
| **G8** result UI (`result.js`) | — | — | The result panel shows the capture report (§9.3) next to the status. A capture finalized with `override` is labelled as such. |

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
- `RULES`: the §7 table (id → gate, severity, text).

### 9.3 Result-side changes (G8)

- `evaluate_gates` keeps its return signature. Add a parallel `reason_ids` list (for example `"G6_rejected"`) to `res`. This change is additive, so old results without it still render.
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
   - `twosided` → no blocking item.
   - **False-accept = 0:** no preset whose final status is `rejected` finalizes without a blocking item or predicted-`rejected` warning.
   - **False-block = 0 on `twosided`.**
   Test: `tests/test_e2e_capture.py` (slow, `e2e` marker, excluded from the fast suite).
4. **Predicted vs actual G6.** Over the 7 scale-able presets, the §5.3 calibrated class equals the final G6 class on ≥ 6 of 7. It is never more optimistic than the final class.
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
8. **Field criterion (links to audit F1 and §6 items 1–3).** On ≥ 3 real sites, a guided two-arm capture reaches final `indicative` or `ok` with no `rejected`. The guided session's predicted G-classes match the final ones. At least one site reaches `ok`. This is required before claiming that guided capture "prevents weak captures" outside synthetic data.

## 12. Staged implementation order

Each stage can ship and be tested on its own. Stop after any stage and the product is still consistent.

| Stage | Scope | Depends on | Exit check |
| --- | --- | --- | --- |
| **S0 Calibration data** | Fix the `twosided` comment/offset mismatch (choose 90° to match the protocol's 60–120° guidance, or document 180°). Build `data/bench/twosided`, run `tools/benchmark.py`. Record its final G6 class. This is V1 from the audit. | — | twosided final coverage and status recorded. **If twosided does not reach G6 `ok`, stop and revisit the protocol before building guidance toward it.** |
| **S0b Protocol correction** | Result of S0: `indicative`, only because of the void-size condition. Root cause, corrected protocol, `twosided_b` preset, and one validation run. See §12a. | S0 | `twosided_b` reaches G6 `ok` in photo and ortho mode under the §12a.5 criteria. **S1–S6 stay blocked until this passes.** |
| **S1 Instant checks in the existing upload** | `capture_metrics.js` plus parity fixtures. Per-file T0 verdicts and EXIF lens check shown in the upload step **before** upload. No new API. | — | Criteria 1 and 7 |
| **S2 Capture session backend** | `capturing` status, `session.json`, `PUT/DELETE photos`, `GET capture`, `finalize` (no preview gating yet), `landslide/capture.py::image_checks` / `sequence_checks`, semaphore | S1 | Criteria 2, 6(a–d), 7 |
| **S3 Guided UI and persistence** | `step-capture` section, `capture_store.js` IndexedDB queue, arms and counters, marker checklist and chips, optional sensors on a secure context | S2 | Criteria 5 (T0/T1), 6(e) |
| **S4 Preview SfM** | `reconstruct(max_attempts=…)`, `worker.run_preview`, `preview_report` (registration, collinearity, focal, elevation, azimuth, stereo pairs, marker scale), finalize blocking plus override | S2, S0 | Criteria 3 (except the G6 prediction), 5 (T3) |
| **S5 Coverage and recapture** | ROI tap, §5 cell map, calibrated `c_rej`/`c_ok`, directional instructions, top-down sketch, marker px thresholds calibrated on real prints | S4, S0 | Criteria 3 (full), 4 |
| **S6 Result-side mapping (G8)** | `reason_ids`, §7 "next time" lines, `capture_report` row, override label, legacy label | S2 (the ids only need S0–S1) | UI smoke: each preset's result shows the matching instruction |
| **S7 Device and field validation** | Android Chrome and iOS Safari over HTTP (no sensors) and HTTPS (sensors). ≥ 3 real sites. | S5, S6 | Criterion 8 |

S1 and S6 give value without any server capture session and are the cheapest first steps. S4 is the largest single cost and risk.

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
| Camera elevation | **Primary cause of both voids.** The S0 preset sits at 16.5° above the ROI centroid. That is below this plan's own ≥ 20° protocol rule (§3 step 2) and the §4.3 T3 warn. So S0 tested a non-compliant capture. |
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

Marker: one board, unchanged. Marker views per arm go from 15 / 5 to 19 / 7, because the wider arms swing further toward face-on the south-facing board. Board side in stored px is 58–92 at a 2 m board (the synthetic board). The ≥ 5 total and ≥ 2 per arm rule (§3 step 5) holds with margin. Separation stays at 90°. Wider separation loses arm B's marker views (120° → 1 view) and does not close void 1.

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

- §3 step 2: "elevation ≥ 20°" is measured above the ROI centroid, not the lowest debris point. Add: "walk each arm about 2 photos further away from the other arm than feels necessary."
- §4.3 elevation check: the median < 20° warn is correct as written. S0b shows it is a **G6 predictor**, not only G5. Show it together with the predicted G6 class.
- §5.4 recapture: a void on the camera-side wall faces the gap between the arms. The instruction must be "continue arm A (or B) outward by N photos from its end", with N = ceil(angular distance / 7°). It must never say "go to the far side", because an isolated far-side mini-pass does not register (§12a.3).
- §11 criterion 3: `twosided` is now expected to be predicted `indicative` (non-compliant elevation). `twosided_b` takes over the "no blocking item / false-block = 0" role.

## 13. Deliberately out of scope

- **In-browser SfM or ArUco (WASM OpenCV):** about 10 MB of download, it would duplicate the server logic, and phone CPU and heat limits apply. Add it only if offline marker checks turn out to be the main field complaint.
- **Continuous-video capture with auto-shutter:** needs a secure context, has no EXIF, and gives video resolution. Revisit once HTTPS field serving is standard.
- **Multi-marker scale:** `aruco_scale` uses one (dict, id). This would be a scaling change, not a capture change.
- **Automatic GPS-based arm separation:** the phone GPS error of 3–10 m is comparable to the arm geometry at a 26 m radius. The preview-model azimuth is better.
- **Changing any gate threshold:** guided capture targets the gates as they are.

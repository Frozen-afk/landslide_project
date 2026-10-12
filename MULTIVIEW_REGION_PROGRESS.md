# Multi-view Region Marking — Design, Implementation and Validation

Status: implemented and validated on the synthetic benchmark scenes and through a real,
unmocked HTTP run. **Not exercised in a real browser** (no browser is installed in this
environment) and not on field photos — see §8.

The task: replace the single-photo polygon as the preferred photo-mode workflow with
several outlines of the same landslide from different viewpoints, fused in 3D, while the
single-photo path, ortho mode, DEM mode, guided capture and saved jobs keep working and
keep their numbers.

---

## 1. Design

### 1.1 Why one polygon on one photo is the weak point

`ground.select_region_ground` already removed the parallax from a photo-mode outline, but
the region is still whatever one hand-drawn line on one oblique photo says it is: a
slightly wrong line, ground that only looks enclosed from that direction, or a part of
the slide hidden behind its own rim in that photo all go straight into the volume with
nothing to contradict them. Several outlines from different directions can contradict
each other, so they can be checked against each other.

### 1.2 Marking requirement

| | value | where |
| --- | --- | --- |
| POV groups for an eligible high-confidence result | 4 | `multiview.MIN_POV_GROUPS` |
| accepted outlines per POV group | 2 | `multiview.MIN_PER_GROUP` |
| outlines in total | ≥ 8 | `multiview.MIN_ANNOTATIONS` |

A **POV group** is a set of registered photos that view the scene from about the same
direction: unit vectors from the scene centre (median sparse point) to each camera centre,
complete-linkage clustering cut at 15°. Direction, not position along the path: azimuth
and elevation both separate groups, and eight photos panned from one standpoint are one
group (`test_pov_groups_split_by_direction_not_by_photo_count`). An outline the fusion
rejects (§1.4) does not count toward 4×2.

Below 4×2 a measurement is still possible, but only when asked for explicitly
(`indicative: true`), and it can never be better than `indicative`
(`MV_insufficient_pov`).

### 1.3 Marking workflow (API + frontend)

1. `GET /api/jobs/{id}/annotations` returns the stored outlines and a `summary`: POV groups
   with `n_accepted`, the 4×2 `requirement`, `suggestions` (which photo to outline next),
   per-outline `agreement`, the fused region's areas, and plain-language `instructions`.
2. The user outlines the slide on the suggested photo with the existing canvas tools
   (click, freehand, auto-detect, vertex drag / insert / delete, zoom, pan), then
   **Accept**, **Skip**, **Delete**, or **Next suggested photo**. Any photo can be reopened
   from the list or the viewpoint chips; its outline comes back for editing.
3. Once at least one outline is accepted, `GET …/annotations/{image}` returns the current 3D
   region reprojected into that photo (`projected.{lower, best, upper}`). The canvas draws
   all three; **Use projected region as outline** copies `best` in as an ordinary editable
   polygon.
4. Every edit of a closed outline is saved with an idempotent `PUT …/annotations/{image}`
   (`draft` until accepted). Outlines live in `state.json`; a reload or a server restart
   returns to the same state.
5. **Compute volume** sends `{"multiview": true}`. The server refuses with 409 and the
   missing-viewpoint instructions until 4×2 is met, unless `indicative: true` is sent.

Suggestion order (`multiview.suggest_images`): POV groups in farthest-first order of viewing
direction; the four best-spread groups that hold ≥ 2 photos are completed first, one photo
per group per round; inside a group the photo that keeps most of the current region (or of
the sparse cloud, before any outline) in frame. Following the list from an empty job
reaches 4×2 in exactly eight outlines on every scene that can reach it.

### 1.4 3D region fusion (`landslide/multiview.py`)

Per accepted outline, per dense point:

1. **Project** with the view's own distortion model (`ImageView.project`).
2. **Visible?** positive depth, inside the frame, and on the front surface of that view's
   z-buffer (6 px cells, nearest depth + 2% of the view's median depth). A point that is not
   visible gets **no vote from that view** — not a negative one.
3. **Vote** softly: a signed-distance ramp 0.5% of the image long side wide around the
   outline (6 px on a 1200 px photo). A point well inside votes 1, well outside 0, on the
   line 0.5.

Then:

4. **Pool by POV group**: weighted mean of the votes inside each group, then the mean over
   the groups that saw the point. Support has to come from different directions, not from
   more photos at one spot.
5. **Robust weights**: each outline's leave-one-out IoU against the consensus of the others.
   An outline is dropped only when its IoU is below 0.50 **and** it is the odd one out —
   the other outlines (at least three) agree with each other, median IoU ≥ 0.70. Between
   0.50 and 0.70 it is down-weighted linearly. Without an odd one out (two outlines that
   disagree; half the viewpoints on one patch and half on another) nothing is dropped and
   the disagreement is reported instead of being settled arbitrarily.
6. **Classify**: confident interior = seen by ≥ 2 groups, vote ≥ 0.75; exterior = seen by
   ≥ 2 groups, vote ≤ 0.25; uncertain boundary = everything between.
7. **Footprints**: the same votes on a ground grid (cell = 2× point spacing, 0.05–0.5 m) give
   three nested regions — **lower** (confident), **best** (vote ≥ 0.5), **upper**
   (vote > 0.25) — each closed over 0.3 m gaps, hole-filled, reduced to its largest
   connected part and contoured to a polygon.

**Cells without data.** A ground cell with no reconstructed point has no surface to project
or to occlude. Two simpler rules were tried and measured on `arc` (40% of the bowl is
unobserved there) before the adopted one:

| rule for no-data cells | `arc` ×8 result | verdict |
| --- | --- | --- |
| project the cell at its nearest-data height, z-buffer as usual | lower footprint 79 m² vs best 100 m² (true 98.5): the wrong guessed height makes the pocket "occluded" or mis-voted, so the confident region gets a bay | rejected |
| no vote; inherit from the nearest voted cell within 2 m | footprint leaks up to 2 m outside the rim wherever the ground just outside is unobserved; boundary error 0.32 m | rejected |
| **vote through each outline's ray-cast ground polygon** (`ground.cast_polygon_to_ground`, the single-photo path's boundary) | lower / best 98.8 / 102.7 m², boundary error 0.11 m | adopted |

So an unobserved pocket that every outline encloses stays inside the region, where the
existing G6 coverage gate measures it (it is not hidden, and it is not counted as seen:
`weak_support_frac`), and unobserved ground outside the outlines stays outside.

### 1.5 Measurement integration (`pipeline.measure(..., annotations=[...])`)

The fused region is handed to the existing photo-mode pipeline through the interface the
ray-cast path already uses, so nothing downstream changed:

| existing stage | what multi-view supplies |
| --- | --- |
| interior points | points inside the **best** footprint polygon, minus points ≥ 2 groups agree are exterior (floaters, a board standing in the footprint) |
| rim extraction | `ortho.select_region_world`'s annulus, taken outside the **upper** footprint — the uncertain band may still be slide material, and datum points must not come from it |
| `polygon_ground` (G6 coverage, `cut_upper_m3`) | the best footprint polygon |
| datum fit, steepness filter, holdout (G9), TIN, bridging cull, raster cross-check, bootstrap CI, LoD, slope stats, artifacts | unchanged `prism_volume` / `dem_volume` call |
| G1–G4, G6, G7, G9, up-vector check | unchanged `evaluate_gates`, same thresholds |

Added on top:

* `volume_bounds_m3 = {lower, best, upper}`: the lower and upper selections integrated with
  the same rim (`prism_volume(..., bootstrap=False)`), each with net / cut / fill / area.
* `est_volume_error_m3` is at least the largest |net| difference between best and the two
  bounds.
* `multiview` (fusion statistics), `annotation_mode`, `confidence`, artifact `regionmap.png`.

### 1.6 Gates and reason ids

New class `marking` in `gates.RULES` (fixed by outlining — not a capture fault, not a datum
fault); `measurement_path = "marking"`. All are `indicative`.

| id | fires when | threshold |
| --- | --- | --- |
| `MV_insufficient_pov` | the 4×2 requirement is unmet after rejections | 4 groups × 2 |
| `MV_mask_disagreement` | an outline was dropped or down-weighted, or outlines with no odd one out disagree | IoU < 0.50 / < 0.70 |
| `MV_poor_visibility` | part of the footprint was not seen by ≥ 2 groups, or the region is mostly hidden in an outlined photo | > 15% of the footprint; < 30% visible |
| `MV_uncertain_boundary` | the outlines leave much of the area undecided, or no confident interior | (upper − lower) / best > 25% |
| `MV_occlusion_support` | too little reconstructed depth under an outline to test occlusion | < 50% of the outline's image area |
| `MV_disconnected` | the outlines agree on more than one patch (only the largest is measured) | ≥ 2 significant parts |

`confidence`: `high` only for a multi-view result with `status == "ok"` (which implies 4×2
and no MV gate); otherwise the status; `lower` for a single-photo outline.

No existing gate or threshold was changed. G5 (ray-cast integrity) is not evaluated on a
multi-view result — there is no single ray-cast boundary; see §6.

---

## 2. Files changed

| file | change |
| --- | --- |
| `landslide/multiview.py` | **new** (590 lines): POV groups, requirement, suggestions, per-view votes, fusion, footprints, reprojection, `select_region_multiview`, `region_map` |
| `landslide/pipeline.py` | `measure(..., annotations=None)`: multi-view branch, volume bounds, `annotation_mode` / `confidence`, `regionmap.png`; `run_spec` passes `region.annotations`. The single-photo branch is the same code, re-indented under `else` |
| `landslide/gates.py` | six `MV_*` rules + thresholds, evaluated only when `res["multiview"]` exists; `classify_measurement_path` learns `marking` |
| `landslide/volume.py` | `prism_volume(..., bootstrap=True)`: `False` skips the bootstrap CI (3 lines; default path identical) |
| `server/schemas.py` | `MeasureRequest.multiview` / `.indicative` (default off), `polygon` defaults to `[]`; `AnnotationRequest` |
| `server/jobs.py` | `Job.annotations`, `annotations_rev` persisted in `state.json` and restored; snapshot carries `annotations_rev`, `n_annotations`; a job interrupted mid-measure reloads with an explanatory `error` |
| `server/routes.py` | `GET/PUT/DELETE /api/jobs/{id}/annotations[/{image}]`, fusion preview + summary, 4×2 gate on `POST …/measure` |
| `server/worker.py` | passes `annotations` to `measure`, stamps `annotations_rev` on the result |
| `server/static/js/steps/multiview.js` | **new** (360 lines): marking panel, save queue, overlays |
| `server/static/js/steps/mark.js` | trace mode `multi` (default), `openImage`, measure flow (in-flight guard, 409 handling, `#measure-note`), per-job mode/photo memory |
| `server/static/js/steps/{scale,upload,result}.js`, `state.js`, `api.js` | measure-button rule, default mode, result rows / reason texts / region map, `state.mv`, `err.status` |
| `server/static/index.html`, `style.css` | marking panel, mode selector, legacy note, region-map figure |
| `tools/mv_benchmark.py` | **new**: ground-truth masks + the validation matrix in §5 |
| `data/bench/*/gt_masks.json` | **new**: synthetic ground-truth masks for the cached scenes |
| `tests/test_multiview.py`, `tests/test_annotations_api.py`, `tests/test_multiview_ui.mjs`, `tests/test_e2e_multiview.py` | **new** tests (§5.1) |
| `architecture.md` | §1, §2, §3.5, §3.7, §3.8a, §4, §5, §6, §7.2 |

---

## 3. Compatibility

| surface | behaviour |
| --- | --- |
| `POST …/measure` with `{polygon, image}` | unchanged request, unchanged code path, same numbers; result gains `annotation_mode = "single_view"`, `confidence = "lower"` and one warning line. `status`, `reasons`, `reason_ids`, `measurement_path` are not touched (`test_measure_multiview_and_legacy_single_view`, `test_legacy_single_photo_measure_body_is_unchanged`) |
| ortho mode, DEM mode | untouched; no new field on their results |
| guided capture | untouched (no file under the capture flow changed except the shared `routes.py`) |
| saved jobs | a `state.json` without `annotations` loads as `{}` / rev 0 (`test_saved_job_without_annotations_loads`); a saved photo-mode result without `annotation_mode` is shown as lower-confidence by the result panel |
| CLI spec | `region.annotations` is optional; an existing spec runs as before |
| `prism_volume` callers | new keyword defaults to the old behaviour |
| stored outlines vs re-scale | outlines are pixel coordinates; a re-scale keeps them and only invalidates the cached preview |

---

## 4. Calibration

Constants were set from the benchmark runs in §5, not chosen in advance.

| constant | value | evidence |
| --- | --- | --- |
| `POV_GROUP_DEG` | 15° | `arc` (96° sweep, 21 views) → 8 groups; `twosided_*` (38 views, ~213°) → 16; `sparse8` (8 views, 64°) → 5, of which only 3 hold 2 photos — correctly not 4×2-eligible |
| `IOU_REJECT` / `IOU_WARN` | 0.50 / 0.70 | clean masks 0.91–0.99; σ = 8 px boundary noise 0.84–0.91; a deliberately bad mask 0.12–0.18. Both thresholds sit in the empty gap |
| `T_INTERIOR` / `T_EXTERIOR` | 0.75 / 0.25 | with 4 groups: "3 of 4 agree" is confident; with 2 groups both must agree |
| `BOUNDARY_FRAC` | 0.5% of the long side | uncertain band 6–8% of the area on clean masks, 12–14% at σ = 8 px |
| footprint cell | 2× point spacing | `twosided_cal` boundary error 0.069 m at 4× → 0.027 m at 2× |
| `MV_UNCERTAIN_AREA_FRAC` | 0.25 | clean ≤ 0.08, σ = 8 px ≤ 0.14; two-group cases with half the bowl seen from one side only reach 0.55 |
| `MV_WEAK_SUPPORT_FRAC` | 0.15 | = 1 − G6's 0.85 `ok` coverage; `twosided_cal` 0.07, `arc` 0.45 |

---

## 5. Validation evidence

### 5.1 Tests added

| file | what it covers | result |
| --- | --- | --- |
| `tests/test_multiview.py` (13, analytic scene: exact cameras and points, no SfM) | POV grouping by direction; 4×2 requirement; suggestion order and skips; clean masks; boundary noise; one bad mask rejected; two disagreeing outlines flagged, not arbitrarily rejected; **occluded views abstain** (exactly 3 of 4 groups vote on the hidden half, which stays confident); missing POV groups; disconnected selection; reprojection into an unmarked view; no MV gate on a clean 4×2; `measure()` multi-view vs legacy single-view (legacy numbers equal a direct `select_region_ground` + `prism_volume` call to 1e-9) | see §5.5 |
| `tests/test_annotations_api.py` (9) | create / update / retrieve / delete per photo, idempotent re-PUT and double delete; validation (unregistered photo, short or out-of-frame polygon, bad status); progress by POV group and suggestions; disagreeing outline reported with an instruction; projected suggestion; outlines survive a restart; a pre-multi-view `state.json` loads; 409 below 4×2, 200 with `indicative`, duplicate submit refused; legacy body unchanged | see §5.5 |
| `tests/test_multiview_ui.mjs` (4, stub DOM + stub fetch) | first suggested photo opens; server text is escaped; a failed save is kept and retried, never sent twice; measure gate and indicative checkbox; stale-result marker | see §5.5 |
| `tests/test_e2e_multiview.py` (3, real SfM + stereo, `twosided_cal`) | suggestions reach 4×2; multi-view measure is `ok` / `high` within 5% of truth with ordered bounds; one bad outline rejected on a real cloud | see §5.5 |

### 5.2 Synthetic ground-truth masks

`tools/mv_benchmark.py::gt_masks` projects the true region ring (r = 5.6 m circle about the
bowl, on the analytic terrain) into **every** photo with the ground-truth camera of that
photo and writes `data/bench/<scene>/gt_masks.json` (`arc`, `sparse8`, `twosided_b`,
`twosided_cal`). 3D truth labels for the dense points come from the same pose-aware
similarity alignment `tools/benchmark.py` already uses.

### 5.3 Benchmark (`python -m tools.mv_benchmark`, full table in `data/bench/multiview_results.md`)

Truth: 67.2 m³, 98.5 m². `legacy` = one ground-truth outline through the unchanged
ray-cast path. `×N` = N ground-truth outlines taken in suggestion order.

**`twosided_cal`** (943 k points, 16 POV groups, the one scene whose legacy result is `ok`)

| scenario | groups / used | precision | recall | boundary err | uncertain area | min IoU | status | cut m³ (lower – best – upper) | vol err |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| legacy 1-view | — | 0.998 | 0.996 | 0.018 m | — | — | ok (confidence lower) | 66.2 | 1.48% |
| ×1 | 1 / 1 | 0.992 | 0.990 | 0.049 m | 7.5% | — | indicative (`MV_insufficient_pov`) | 66.1 – 66.2 – 66.3 | 1.42% |
| ×2 | 2 / 2 | 0.994 | 0.992 | 0.040 m | 55% | 0.92 | indicative (+ `MV_poor_visibility`, `MV_uncertain_boundary`) | 20.4 – 66.2 – 66.3 | 1.40% |
| ×4 | 4 / 4 | 0.995 | 0.996 | 0.030 m | 6.3% | 0.94 | indicative (`MV_insufficient_pov`) | 66.1 – 66.2 – 66.3 | 1.38% |
| **×8 (4×2)** | 4 / 8 | 0.996 | 0.995 | 0.027 m | 6.2% | 0.91 | **ok, confidence high** | 66.1 – 66.2 – 66.3 | 1.39% |
| ×16 | 12 / 16 | 0.995 | 0.995 | 0.031 m | 6.0% | 0.91 | ok, high | 66.1 – 66.2 – 66.3 | 1.39% |
| ×8, noise σ = 3 px | 4 / 8 | 0.995 | 0.994 | 0.037 m | 7.5% | 0.89 | ok, high | 66.1 – 66.2 – 66.3 | 1.38% |
| ×8, noise σ = 8 px | 4 / 8 | 0.977 | 0.995 | 0.089 m | 13.6% | 0.84 | ok, high | 66.2 – 66.3 – 66.5 | 1.22% |
| ×8, one bad mask | 4 / 7 | 0.996 | 0.996 | 0.027 m | 6.3% | 0.18 | indicative (`MV_mask_disagreement`, `MV_insufficient_pov`); bad one rejected | 66.1 – 66.2 – 66.3 | 1.39% |
| 8 good + 1 bad | 4 / 8 | 0.996 | 0.995 | 0.027 m | 6.2% | 0.13 | indicative (`MV_mask_disagreement`); bad one rejected | 66.1 – 66.2 – 66.3 | 1.39% |
| ×8, occluder (wall hides part of the slide from 2 groups; outlines clipped to what is visible) | 4 / 8 | 0.997 | 0.993 | 0.030 m | 6.5% | 0.91 | region metrics only | — | — |
| 8 outlines, 2 POV groups | 2 / 8 | 0.993 | 0.991 | 0.045 m | 7.2% | 0.99 | indicative (`MV_insufficient_pov`) | 66.1 – 66.2 – 66.3 | 1.40% |

Background inclusion = 1 − precision (0.2–2.3%); true-point loss = 1 − recall (0.4–1.0%).

**Other scenes, `×8` vs legacy**

| scene | legacy: status, vol err, boundary | ×8: status, vol err, boundary | notes |
| --- | --- | --- | --- |
| `twosided_b` (942 k pts, 16 groups) | indicative (`G9_datum_unstable`), 16.8%, 0.019 m | indicative (`G9_datum_unstable` only), 17.4%, 0.033 m | 4×2 met, no MV gate; the datum fault is unchanged and still the only reason — multi-view does not, and should not, hide it |
| `arc` (248 k pts, 8 groups, 40% of the bowl unobserved) | rejected (`G6_rejected`, `G9_datum_unstable`), 26.0%, 0.064 m | rejected (same + `MV_poor_visibility`), 26.0%, 0.114 m | coverage 0.595 legacy vs 0.589–0.608 multi-view: both sit on G6's 0.60 line, so the G6 class flips between `rejected` and `indicative` run to run on either path (architecture.md §7.1 already lists `arc` as 60–62%) |
| `sparse8` (150 k pts, 5 groups, only 3 with 2 photos) | rejected (`G5_partial`, `G6_rejected`, `G9_datum_unstable`), 39.1%, 0.119 m | rejected (`G6_rejected`, `G9_datum_unstable`, `MV_insufficient_pov`, `MV_poor_visibility`), 32.7%, 0.142 m | 4×2 is not achievable with these photos, and the summary says so |

Bad-mask rejection held on all four scenes (IoU 0.13–0.18 against 0.84–0.99 for every
honest outline); precision, recall, boundary error and volume are unchanged to the digits
reported, on each scene.

### 5.4 What the numbers say

* **1 vs 2 vs 4 vs 8+ outlines, clean masks**: on a well-captured scene the *volume* barely
  moves (1.38–1.48%) — a ground-truth outline through the ray-cast is already a very good
  region. Multi-view does not buy accuracy when the single outline is perfect; it buys
  **robustness and an honest label**. Two outlines from two sides are worse than one for
  the confident region (half the bowl is seen from one side only: lower-bound cut 20.4 m³),
  which is exactly why two is not enough and is flagged.
* **Boundary noise**: σ = 8 px per vertex on every outline moves the volume by 0.1 m³ and
  widens the uncertain band from 6% to 14%; nothing is rejected.
* **One bad mask**: rejected, region unchanged, result marked `indicative` with the photo
  named.
* **Occlusion**: with part of the slide hidden behind a wall for two groups, recall stays
  0.993. On `arc` and `sparse8` the same wall hides that part from *every* outlined view
  (all cameras are on one side); the hidden part then cannot be marked, recall drops to
  0.97 / 0.86 and the footprint loses it (`MV_uncertain_boundary`, `MV_poor_visibility`).
* **Boundary error** is 1–5 cm worse than a ground-truth single outline (0.027 vs 0.018 m
  on `twosided_cal`) — the footprint is contoured from a 0.1–0.2 m grid.
* **Uncertainty coverage**. Region bounds (lower – upper) contain the *best* estimate in
  every run and contain the truth only where marking is the dominant error; they are
  0.1–0.6 m³ wide on clean scenes and do not, and are not meant to, cover coverage or
  datum error. `est_volume_error_m3` contains the truth on `twosided_cal`, `arc` and
  `sparse8` (as for legacy) and misses it on `twosided_b` for both paths (the 16–17%
  datum bias G9 flags). The pipeline's measured-to-G6-upper range contains the truth on
  the same three scenes for both paths.

### 5.5 Runtime and memory

Key scenarios run alone (12-core laptop, no GPU):

| scene | legacy measure | ×8 measure | ×16 measure | fusion alone ×8 / ×16 | peak RSS (process, all scenarios) |
| --- | --- | --- | --- | --- | --- |
| `sparse8` (150 k) | 19 s | 22 s | — | 3 s | 0.39 GB |
| `arc` (248 k) | 46 s | 51 s | 53 s | 5.5 s / 7.2 s | 0.96 GB |
| `twosided_cal` (943 k) | 90 s | 97 s | 120 s | 10 s / 17 s | 0.91 GB |
| `twosided_b` (942 k) | 168 s | 241 s | 247 s | 10.5 s / 16 s | 1.06 GB |

Fusion costs about 1 s per outline per million points plus ~4 s fixed, and one byte per
point per outline (16 outlines × 2.5 M points = 40 MB). The rest of the difference is the
two extra `prism_volume` calls for the bounds, run without the bootstrap. **Known cost**:
when the rim datum escalates to the TPS membrane, each of those two calls refits it —
`arc` with a single outline took 407 s against 46 s for that reason (§8).

The in-request preview fuses a 150 k-point subsample once per change of the outlines and
caches it; its first-call latency was not measured separately (the HTTP run below shows
6–11 ms `PUT`s and a 0.04 s cached `GET`).

### 5.6 Real HTTP run (unmocked: uvicorn, process pool, real job directory)

A job built from the `twosided_cal` photos and reconstruction, driven with `requests`:

| step | observed |
| --- | --- |
| `POST …/measure {multiview: true}` with no outline | 400 "accept at least one outline first" |
| 5 outlines via `PUT`, following `suggestions[0]` | each 200 in 6–11 ms; from the 2nd on, `projected.best` returned 35–37 vertices |
| `POST …/measure {multiview: true}` with 5 | 409 with `requirement` (2 of 4 groups complete) and instructions |
| server killed and restarted | 5 outlines, `rev` 5, group counts restored |
| 3 more outlines, then measure twice in a row | first 200 `queued`, second 409 "busy" |
| result | `status ok`, `confidence high`, `region_method multiview_fusion`, cut 66.2 m³ (truth 67.2), coverage 0.984, bounds 66.13 – 66.23 – 66.28, `annotations_rev` 8 = snapshot's; all four artifacts served |
| restart again | result and outlines still there |
| legacy `POST …/measure {polygon, image}` | `status ok`, `confidence lower`, `region_method ground_frame`, cut 66.17 m³ |

### 5.7 Existing behaviour outside multi-view

`measure()` on `twosided_cal` with the pre-change tree (`git stash`) and with this tree,
every field compared:

| mode | keys added | shared keys that differ |
| --- | --- | --- |
| photo, single outline | `annotation_mode`, `confidence` | `warnings` (one line appended) — every number identical |
| ortho | none | none |

### 5.8 Final validation run

One run of everything, after the last code change:

| command | result |
| --- | --- |
| `.venv/bin/python -m pytest -q` (fast + every slow e2e file: `test_e2e_synth`, `test_e2e_presets`, `test_e2e_capture`, `test_e2e_multiview`) | **324 passed, 1 skipped** (pre-existing skip) in 39 min |
| `node --test tests/*.mjs` | **33 passed** (29 existing + 4 new) |
| `python -m tools.mv_benchmark --presets twosided_cal,twosided_b,arc,sparse8` | §5.3; `data/bench/multiview_results.md` |
| `python -m tools.mv_benchmark … --only "legacy,clean x1,clean x8"` (alone, for runtimes) | §5.5 |
| pre-change vs current `measure()` field diff (photo single outline, ortho) | §5.7 |

Of the 324: 13 + 9 + 3 are the new Python tests in §5.1; the other 299 are the existing
suite, unmodified, including every e2e threshold.

---

## 6. Deviations from the request

| request | what was done instead, and why |
| --- | --- |
| "Classify points … using calibrated multi-view agreement" | Points **and** ground cells are classified. A point set has no boundary to hand to rim extraction or G6; the footprint needs cells. No-data cells cannot be projected, so they are voted on through each outline's ray-cast ground polygon (§1.4). |
| "Do not count occluded views as negative votes" | Holds for every reconstructed point and every cell with data. A no-data cell has no surface to occlude; it takes each outline's ground-polygon vote, positive or negative. |
| G1–G9 evaluation on the fused region | G5 (ray-cast hit fraction of *the* boundary) has no multi-view equivalent and is not evaluated; `MV_poor_visibility`, `MV_occlusion_support` and `MV_uncertain_boundary` cover what it guarded. `sparse8` shows the trade: `G5_partial` disappears, two MV gates appear, the status stays `rejected`. |
| "mark [legacy jobs] lower-confidence" | Done with a new `confidence` field and a warning, **not** by changing `status`. Forcing `indicative` would have changed existing results and broken `test_non_dem_mode_unaffected_by_dem_guard`, against "preserve existing numerical behavior". |
| lower / best / upper volume **bounds** | Reported, and their spread is a floor on `est_volume_error_m3`. They bound marking uncertainty only (§5.4); they are not a confidence interval for the volume. |
| "Compare 1-view versus 2, 4, and 8+" | Done with ground-truth outlines. Legacy with a *noisy* single outline was not benchmarked, so the robustness gain over a realistic hand-drawn single outline is argued from the bad-mask and noise rows, not measured head to head. |
| occlusion benchmark on real scenes | A synthetic wall added to the reconstructed cloud, outlines clipped to the visible part; region metrics only, no volume (the wall would enter the rim band). |
| `prism_volume` | One keyword added (`bootstrap`), needed to keep the bound volumes from tripling the runtime. Not otherwise touched. |
| frontend validation | No browser available here. Covered by `node --check`, a stub-DOM test of the save queue and gate, and the HTTP run; the canvas overlays, buttons and layout have **not** been seen rendered. |
| README | Step 3 of the quick start rewritten for the new workflow (not asked for; the old text described a workflow that is no longer the default). |

A bug of my own, found by an existing test and fixed before any reported number: the first
version of the `prism_volume` flag was named `ci`, shadowed by an existing local `ci = None`,
which silently disabled the bootstrap interval for every caller.
`test_measure_scale_error_widens_ci_by_cube_power` failed; the flag was renamed and all
benchmark runs were redone.

---

## 7. Retry, interruption, duplicate submission, reload

| case | before | now |
| --- | --- | --- |
| page reload while marking | the polygon was browser-only state and was lost | outlines (drafts included) are on the server; mode and open photo are restored per job |
| save fails (connection drop) | n/a | edit kept locally, "not saved yet — retrying", retried every 3 s and before any measurement; `beforeunload` warns while anything is unsaved |
| same outline sent twice | n/a | `PUT` is idempotent; an identical body does not bump `rev` |
| double click on "Compute volume" | second request → 409 → `alert` | second click dropped client-side; a 409 "busy" attaches to the running measurement |
| reload while measuring | worked (poll resumes) | unchanged; the measure button no longer invites a second submit that would fail |
| measurement failed while the page was away | nothing shown | the snapshot's `error` is shown next to the button |
| server restart mid-measure | job back to `ready`, result silently missing | same status, plus `error` "measurement was interrupted by a server restart — run it again" |
| outlines edited after a result | n/a | `annotations_rev` on the result vs the job: "outlines changed since the result below was computed" |

---

## 8. Remaining risks

1. **Not seen in a browser.** The marking panel, overlays and button states are verified
   only as described in §6. Expect small UI defects on first real use.
2. **Synthetic outlines only.** Every benchmark outline is a projection of the true ring,
   optionally jittered or displaced. Real outlines are biased in ways Gaussian vertex
   noise does not model (consistently inside the rim; cut at the photo edge).
   `IOU_REJECT` / `IOU_WARN` sit in a wide empty gap on this data (0.18 … 0.84); real data
   may narrow it.
3. **One scene geometry.** All four scenes are the same bowl and ring. The uncertain-area
   and weak-support thresholds are calibrated on it; an elongated or branching slide,
   or one on a steep face, is untested.
4. **POV groups are centred on the scene, not the slide** (`ponytail:` note in
   `pov_groups`). A slide far off-centre in a wide scene gets slightly wrong separations.
5. **A cut-off of 15° makes adjacent groups ~14° apart.** A user who ignores the
   suggestions can satisfy 4×2 with four neighbouring groups spanning ~50°. The
   suggestions spread them; the requirement itself does not enforce a minimum total span.
6. **TPS datum × 3.** When the rim datum escalates to the TPS membrane, the two bound
   volumes refit it (§5.5): minutes, not seconds. Reusing the best selection's fitted datum
   for the bounds would remove this but means reaching into `prism_volume`.
7. **Preview vs final fusion.** The 4×2 gate on the request uses the subsampled preview;
   the worker fuses the full cloud. If the two disagree about a rejection, the result
   comes back `indicative` (`MV_insufficient_pov`) after a request that was accepted as
   final. Never the unsafe direction.
8. **Region partly hidden from every outlined view** is dropped from the footprint
   (`arc` / `sparse8` occluder rows) and reported only through the uncertainty gates.
9. **Stray-name escaping.** Photo names reach the page through the new panel escaped; the
   pre-existing result panel still writes server strings (which can now contain photo
   names inside reason text) with `innerHTML`.

---

## 9. Acceptance matrix

| # | requirement | status | evidence |
| --- | --- | --- | --- |
| 1a | ≥ 4 POV groups, ≥ 2 photos each, ≥ 8 outlines for a high-confidence result | met | `requirement`, `MV_insufficient_pov`; `test_requirement_is_four_groups_times_two`; bench ×4 / 2-group rows `indicative` |
| 1b | suggest images across azimuths / elevations | met | `suggest_images`; first four suggestions = four groups, eight = 4×2 (unit, API, e2e tests) |
| 1c | draw, edit, accept, skip, revisit | met in code; **not browser-tested** | `steps/multiview.js`, `mark.js::openImage`; API statuses `draft` / `accepted` / `skipped` |
| 1d | reproject the 3D region as an editable suggestion | met | `project_ring`, `GET …/annotations/{image}`; mean error < 6 px in the unit test |
| 1e | persist and restore outlines | met | `state.json`; `test_annotations_survive_a_restart`; HTTP run across two restarts |
| 2a | project dense points into every annotated image; depth, bounds, z-buffer | met | `_view_votes` |
| 2b | occluded views are not negative votes | met (see §6 for no-data cells) | `test_occluded_views_do_not_vote_against_hidden_points`; bench occluder row |
| 2c | confident / uncertain / exterior by calibrated agreement | met | §1.4, §4 |
| 2d | reject or down-weight one inconsistent outline | met | bad-mask rows on four scenes; unit + e2e tests |
| 2e | support from multiple POV groups | met | group pooling, `MIN_SUPPORT_GROUPS`; 2-group ×8 row `indicative` |
| 2f | lower / best / upper selections and volume bounds | met | `volume_bounds_m3`; ordered in every run |
| 3a | integrated into photo pipeline, rim, coverage, datum, G1–G9, artifacts, result | met, G5 excepted (§6) | §1.5; `twosided_b` keeps `G9_datum_unstable`, `arc` keeps `G6_rejected` |
| 3b | multi-view reason ids and gates | met | six `MV_*` ids, §1.6 |
| 3c | ortho, DEM, guided capture, saved jobs, numbers preserved | met | §5.7 field-by-field diff; existing suite (§5.8) |
| 3d | no existing gate or threshold weakened | met | `git diff landslide/gates.py`: additions only; G6 coverage on `arc` 0.589–0.608 vs 0.595 legacy |
| 4a | backward-compatible create / update / delete / retrieve API | met | `test_annotations_api.py`; legacy body test |
| 4b | marking step with progress by POV group | met in code; not browser-tested | `#mv-progress` chips |
| 4c | show suggestions, disagreement, missing viewpoints, confident / uncertain regions, instructions | met in code; not browser-tested | panel + canvas overlays + `regionmap.png` |
| 4d | block final measurement below 4×2, allow explicit indicative | met | 409 / `indicative`; API test; HTTP run |
| 4e | retry, interruption, duplicate submission, reload | met | §7 |
| 5a | synthetic ground-truth masks for benchmark scenes | met | `data/bench/*/gt_masks.json` |
| 5b | 1 vs 2, 4, 8+ | met | §5.3 |
| 5c | clean, noise, bad mask, occlusion, missing POV, disconnected, legacy | met | §5.1, §5.3 (disconnected: unit test only) |
| 5d | precision / recall, boundary error, background inclusion, point loss, volume error, uncertainty coverage, runtime, memory | met | §5.3–§5.5 |
| 5e | focused tests during development, one final integration validation | met | §5.8 |
| 6 | `architecture.md` updated, this file created | met | |

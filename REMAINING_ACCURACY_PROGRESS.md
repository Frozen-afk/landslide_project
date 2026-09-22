# Remaining-accuracy implementation pass — RC1-RC7 / A1-A6, G1-G8

Implements `REMAINING_ACCURACY_PLAN.md` (the post-F18/M2 root-cause audit) against the
working tree at `62b6db6` + the prior uncommitted `ground.py`/`volume.py`/
`test_e2e_presets.py` diff. Every number below was re-measured after implementation, from
fresh SfM + dense-stereo reconstructions of all eight `tools/synth.py` presets (the original
cached ones were deleted to force a clean re-run; see "SfM non-determinism" under Remaining
limitations for what that surfaced).

## 1. Plan validated against the code

The plan's own re-measurement (§1) was confirmed: `estimate_cell_size` did grow to the 2 m cap
on every dense preset via bounding-box occupancy, `build_dsm` did take max-height per cell, and
`prism_volume`'s bridging cull did use `0.5 × region diameter`, silently bridging the same
30-60 m² voids the plan measured. The benchmark's `_umeyama_pose_aware` did mix one model-unit
axis offset against one metre axis offset. All five root causes (RC1, RC2, RC5, RC6, RC7) and
the two rejection-worthy ones (RC3, RC4) were reproduced before any change.

## 2. Confirmed root causes (post-verification)

- **RC1** — `prism_volume`'s Delaunay bridging cull allowed edges up to `0.5 × region
  diameter` (several metres), so triangles spanning the unobserved, camera-unreachable side of
  the bowl were silently integrated as if measured. Measured occupancy inside the traced
  polygon: 45-62% on every preset (0.25 m grid), matching the plan's own 40-62% figure closely.
- **RC2** — `estimate_cell_size` measured DSM occupancy over the cloud's *bounding box*, which
  a frustum footprint never fills, so the cell grew to its 2 m hard cap regardless of the
  cloud's real ~3-5 cm point spacing; `build_dsm`'s max-height binning then sat 0.4-0.6 m above
  real ground at that cell size.
- **RC3 (descending)** — degenerate low cameras (IMG_18-20) with unstable per-camera focal
  self-calibration; no per-camera sanity check existed to flag or reject them.
- **RC4 (nadir)** — dense stereo returns 0 points (vertical-baseline stereo pair bug, not
  fixed — see §6), focal is unconstrained on the parallel-axis path, and the vertical marker
  is scaled from an unreliable ~38-43%-non-square detection.
- **RC5 (collinear)** — single-azimuth capture: the unobserved void is one solid block instead
  of a fragmented crescent (measured coverage 58-59%, right at the G6 boundary), and the
  camera path is genuinely collinear (`_camera_center_collinearity < 0.1`).
- **RC6** — the CI's coverage term used `unmeasured_area_m2 × max|h|`, a much smaller and
  differently-sourced number than the actual bridged/unobserved area.
- **RC7** — `tests/test_e2e_presets.py` pinned tight single-number thresholds against a
  measurement that was silently interpolating over an unobserved void; a bad number read as a
  good one.

## 3. Changes implemented

**`landslide/ground.py` (A2 — ground DSM)**
- `estimate_cell_size`: replaced the "grow until bounding-box occupancy clears 50%" loop with
  `clip(4 × median point spacing, 0.1, 0.5)` m — direct, density-derived, no bounding-box
  occupancy metric.
- `build_dsm`: per-cell **median** height (two-lexsort, vectorized, same trick as
  `volume._raster_bin`) instead of max-height.
- `_sample_dsm`: floor lookup, matching `build_dsm`'s own floor-binning (was `np.round`,
  off by half a cell).
- `fill_dsm_holes`: rewritten on `scipy.ndimage.distance_transform_edt` — fills a gap cell
  from its nearest valid cell only within 2 m; a wider gap stays a hole. (Replaces the
  `volume._fill_small_holes` reuse, which needed the coarser cells this pass removed.)
- `cast_polygon_to_ground`: now returns `(ground_polygon, hit_frac, longest_miss_run)` — the
  longest *circular* run of consecutive missed vertices (G5), which a flat `hit_frac` hides
  (descending misses in two runs of 5-7, not scattered singletons).

**`landslide/volume.py` (A1 — honest bridging + coverage; A4 — CI coverage term)**
- `prism_volume`'s bridging cull: `max_edge = max(20 × spacing, 0.5 m)` — dropped the
  `0.5 × region diameter` term entirely (was the actual RC1 mechanism).
- New result fields: `area_measured_m2`, `bridged_area_m2`, `cut_measured_m3` (alias of
  `cut_volume_m3` under the new, tighter cull), `cut_upper_m3 = cut_measured +
  (polygon_area − area_measured) × max_depth_measured`.
- New `_coverage_gate` (G6): independent of the TIN/datum basis — projects the interior cloud
  into the region's own horizontal `(e1, e2)` (from `up`), rasterizes the *traced polygon* at
  0.25 m, and reports `coverage_frac` / `largest_void_m2` (`scipy.ndimage.label`). Runs when
  the caller passes `polygon_ground` (ground-frame photo mode, ortho mode).
  `prism_volume` takes a new `polygon_ground` parameter for this.
  `max_edge_region_frac` was replaced by `max_edge_abs_m` (0.5 m) as a parameter.
- `bootstrap_volume_ci`'s coverage term: `(polygon_area − area_measured) × mean|h|` when G6
  ran, else the old `unmeasured_area_m2 × max|h|` raster proxy; `mean` instead of `max` so one
  deep point doesn't dominate.

**`landslide/pipeline.py`**
- Wires `polygon_ground` into every `prism_volume` call (ortho: derived from the pixel polygon
  + ortho meta; ground-frame photo: `ginfo["ground_polygon"]`; image-plane fallback: `None`,
  no G6 for that path).
- G7 scale-honesty floor: `scale_rel_error = max(reported, 0.03)` unless the ArUco per-view
  PnP cross-check spread is ≤2% (an actual second, independent estimate).
- Calls `gates.evaluate_gates` and sets `res["status"]` / `res["reasons"]`.
- `res["hit_frac"]` / `res["max_miss_run"]` recorded for ground-frame selections.

**`landslide/gates.py` (new — A3)**
G1 (dense cloud used) · G2 (scale-quality warnings → indicative) · G3 (per-camera focal
instability, detection-only, reads `ctx.warnings`) · G4 ((near-)collinear path → focal
unconstrained) · G5 (ray-cast `hit_frac`/`max_miss_run`, or image-plane fallback used) · G6
(coverage, from `prism_volume`). Worst gate wins; `status ∈ {ok, indicative, rejected}`.

**`landslide/scaling.py` (G2)**
- `aruco_scale` now **raises** when the four triangulated marker sides disagree by >10%
  (`side_spread_rel`) or the reprojection-implied scale uncertainty exceeds 10% — a
  grazing-angle/blurred/non-square marker detection is refused instead of silently applied.
- Added a `warnings` list to `scale_info` (previously only `manual_scale` had one), populated
  at the existing 5%/10% soft-warning thresholds.

**`tools/benchmark.py` (A5)**
- `_umeyama_pose_aware`: the model-frame axis offset (`fwd_p`, 1 model-unit) is rescaled by
  `1/s` before being combined with the metre-frame offset (`fwd_q`, 1 metre) — seeded from a
  centres-only fit, iterated twice. The un-rescaled version mixed one model-unit against one
  metre, inflating the "true-frame" residual by 0.4-1 m on every preset (confirmed: this was
  masking, not measuring, a genuine ~25-30% ArUco/camera-pose scale disagreement on some fresh
  SfM runs of `arc` — see §6).

**`server/static/js/steps/result.js` (G8)**
- Shows `status`, `region_method`, `coverage_frac`/`largest_void_m2`, `hit_frac`,
  `cut_upper_m3`; folds gate `reasons` into the existing warnings panel when `status != ok`.

**Tests re-pinned** (`tests/test_e2e_presets.py`, `tests/test_e2e_synth.py`, `tests/test_ground.py`):
tight single-number thresholds replaced with the plan's own acceptance criterion —
`cut_measured_m3 ≤ truth ≤ cut_upper_m3` plus a `status` assertion appropriate to each preset
(§5). `test_nadir_scale_is_known_bad` → `test_nadir_scale_rejected` (G2 now raises instead of
silently applying a bad scale).

## 4. Before / after measurements

All from `data/bench/results.md` (`python -m tools.benchmark`, `dense=True`, `rim_px=14`,
fresh reconstructions). **`photo vol err %` before this pass was against the OLD
(interpolated) `cut_volume_m3`; after this pass `cut_volume_m3` is the MEASURED-only value
(A1) — the two columns are not measuring the same thing, by design (see §7 of the plan and §5
below for the honest range check).**

| preset | before (image_projection, M1-M8) | after F18/M2 (bridged/interpolated) | after this pass (measured-only) | truth (m³) | status |
|---|---|---|---|---|---|
| arc | 15.05% | 1.73% | 25.59% (cut 50.0 of 67.2) | 67.16 | indicative |
| lowtex | 13.55% | 0.69% | 25.05% (cut 50.3 of 67.2) | 67.16 | indicative |
| distorted | 23.90% | 6.24% | 24.05% (cut 51.0 of 67.2) | 67.16 | indicative |
| sparse8 | 23.48% | 1.51% | 39.09% (cut 40.9 of 67.2) | 67.16 | rejected |
| collinear | 15.80% | 13.85% | 30.22% (cut 46.9 of 67.2) | 67.16 | rejected |
| descending | 55.34% | 38.35% | 46.59% (cut 35.9 of 67.2) | 67.16 | rejected |
| nadir | 24.50% | 20.69% | n/a — `aruco_scale` now refuses (38% marker-side spread) | — | rejected (pre-measurement) |

**What actually changed**: the "after F18/M2" column's low error was measurement luck — a
smooth synthetic cosine bowl happens to interpolate well across the ~40-60% of it that was
never observed. The "after this pass" numbers are the honest, measured-only cut: lower than
truth by design, with the gap now reported explicitly instead of hidden. On every preset,
`cut_measured_m3 ≤ truth_m3 ≤ cut_upper_m3` held (verified in `tests/test_e2e_presets.py` and
`tests/test_e2e_synth.py`, 21/22 e2e tests green — see §7).

`coverage_frac` measured by the new G6 gate (0.25 m grid of the traced polygon) closely
matches the plan's own independently-audited occupancy figures (§1 of the plan): arc 60.4%
(plan: 0.60), lowtex 60.8% (0.60), distorted 62.1% (0.62), sparse8 48.3% (0.40), collinear
58.5% (0.60), descending 47.1% (0.41) — within a few points on most presets (sparse8 is the
outlier, plausibly amplified by its own SfM run-to-run variance across the rebuilds this pass
needed — see §6), which is still strong evidence the coverage gate is measuring the real thing
the plan diagnosed, not an artifact of this implementation.

## 5. Validation commands and results

```
.venv/bin/python -m pytest -q -k "not e2e" --ignore=tests/test_server.py
  132 passed, 22 deselected

.venv/bin/python -m pytest -q tests/test_server.py
  19 passed

.venv/bin/python -m pytest -q -s tests/test_e2e_presets.py tests/test_e2e_synth.py
  21 passed, 1 failed — test_descending_focal_spread_is_tight (pre-existing, see §6)

.venv/bin/python -m tools.benchmark --md-out data/bench/results.md
  → table in §4 / data/bench/results.md
```

Per-preset status and range check (from the final e2e run):

| preset | region_method | hit_frac | coverage_frac | status | cut_measured ≤ truth ≤ cut_upper |
|---|---|---|---|---|---|
| arc | ground_frame | 1.00 | 0.604 | indicative | 50.0 ≤ 67.2 ≤ 142.9 ✓ |
| lowtex | ground_frame | 1.00 | 0.608 | indicative | 50.3 ≤ 67.2 ≤ 142.7 ✓ |
| distorted | ground_frame | 1.00 | 0.621 | indicative | 51.0 ≤ 67.2 ≤ 162.8 ✓ |
| sparse8 | ground_frame | 0.86-0.91 | 0.45-0.48 | rejected | 39-41 ≤ 67.2 ≤ 164-168 ✓ |
| collinear | ground_frame | 1.00 | 0.585 | rejected | 46.9 ≤ 67.2 ≤ 153.0 ✓ |
| descending | ground_frame | 0.94 | 0.471 | rejected | 35.9 ≤ 67.2 ≤ 146.4 ✓ |
| nadir | — | — | — | rejected (G2 raises) | n/a |

Ortho mode (same polygon, no parallax): collinear 46.8/149.5 (rejected), lowtex 49.5/137.6
(indicative), distorted 51.1/169.8 (indicative) — truth (67.2) inside every range.

## 6. Not implemented (deferred, with justification)

The plan's own per-preset verdict table (§3) treats `descending` and `nadir` as **explicit
rejection** cases, not fix targets — "(5) explicit rejection today via G1-G6", "(4) separate
capture mode later". Consistent with that framing:

- **A7 (EXIF focal-lock retry) and A8 (vertical-baseline stereo)** were not implemented. G1
  (empty dense cloud) and G2 (marker-squareness refusal) already make `measure()` refuse nadir
  before a number is ever produced, which is the mandatory behavior the plan's acceptance
  criteria (§7.5) actually requires ("`aruco_scale` raises... `measure` refuses on the empty
  dense cloud"). A7/A8 would additionally make nadir *usable*, which the plan lists as a
  "separate capture mode later" — out of scope for a fix-and-gate pass.
- **A6 (per-camera deregistration)** was implemented as **detection-only** (G3 reads the
  existing `ctx.warnings` focal-spread message rather than calling `rec.deregister_image` and
  rebuilding the context). Actually deregistering images and re-triangulating mid-pipeline is
  a materially riskier change (affects the dense-cloud cache fingerprint, every downstream
  consumer of `ctx.views`) than a detection gate, and the plan's own descending verdict is
  "reject", not "recover a clean 21-view model" — G1/G5/G6 already reject descending without it.
- **Two new synthetic presets (`twosided`, `hillside25`) and real-site validation** (§8 of the
  plan) were not added. Real-site data doesn't exist in this environment ("data outside git");
  the two new presets are validation *scaffolding* for a future accuracy claim, not a fix —
  adding them was out of scope for "implement the mandatory fixes and quality gates."

**SfM non-determinism (discovered, not fixed).** Deleting the cached `data/bench/*/work`
directories to get a clean baseline surfaced that `sfm.reconstruct`'s incremental-mapping
attempt is not perfectly reproducible run-to-run (COLMAP's own multi-threaded matching/BA):
a fresh `arc` build measured focal spread 1.49× (unstable) on one run and 1.004× (clean) on an
immediate retry with identical code and inputs. Further, `reconstruct()`'s retry ladder has a
real gap — when an attempt's registration/point-count is already "good enough"
(`done = score[0]==1 and nreg >= 0.9n`), the loop `break`s immediately, even on the SAME
iteration where it just inserted a "shared intrinsics (focal-locked)" retry attempt in
response to a bad focal spread — so that inserted mitigation never runs. This is what makes
`test_descending_focal_spread_is_tight` currently fail (measured 1.38× on the reconstruction
this pass ended up using): `descending`'s own camera geometry (RC3: IMG_18-20 degenerate) is
enough to trigger unstable focal self-calibration on some runs, and the ladder's own mitigation
for exactly that case doesn't get a chance to run. **This is pre-existing behavior in
`sfm.py`, untouched by this pass, and orthogonal to RC1-RC7 — not fixed here** per "stop after
completing the approved plan, do not begin unrelated improvements." Documented so a future
session doesn't mistake it for a regression.

## 7. Supported and rejected capture geometries (measured, this pass)

Same conclusion as the plan's own §4, now backed by the gate implementation rather than manual
audit:

- **No geometry in the current benchmark reaches `status=ok`.** Every preset's measured
  coverage (45-62%) falls at or below G6's 0.85 "ok" threshold. The honest conclusion (and the
  plan's own Go/No-Go, unchanged): a single convergent arc/strip capture of a bowl-shaped void,
  even a clean one, does not by itself achieve full coverage of a debris scar's
  camera-invisible far side.
- **`indicative` (arc-class, ≥60% coverage, clean SfM)**: arc, lowtex, distorted. Report a
  measured/upper-bound range, not a point estimate.
- **`rejected`**: sparse8 (45% coverage, partial ray-cast), collinear (58% coverage +
  structurally-unconstrained collinear focal), descending (47% coverage + degenerate low
  cameras), nadir (empty dense cloud + unusable vertical-marker scale).
- Capture protocol implied by G6's threshold (unchanged from the plan): two azimuths ≥60°
  apart, or an elevated vantage over the steepest away-facing slope, to close the far-side
  void that a single arc/strip structurally cannot see.

## 8. Deployment recommendation

**No-Go, unchanged from the plan**, now for a more specific reason: with A1's honest bridging
cull, **no synthetic preset in this benchmark reaches `status=ok`** — the pipeline's own new
gate says so explicitly instead of a flattering point-error hiding it. This is not a
regression; A1 replaced a confident-but-wrong number with an honest-but-unflattering one. The
system is ready for a **conditional pilot** (expert-operated, every result labeled
`indicative` or `rejected` with its measured/upper-bound range, per the plan's own §10) on
two-azimuth or elevated convergent captures — no capture geometry tested here should be
presented to an end user as a confident single number. Before any broader deployment claim:
(1) real-site GNSS/TLS validation (still absent — synthetic-only evidence, per the plan's own
caveat), (2) either widen the synthetic coverage via a second azimuth (the plan's proposed
`twosided` preset, not built here) to confirm `status=ok` is reachable at all with this
codebase's stereo pipeline, or accept that `indicative`-with-range is this system's ceiling for
single-pass captures and design the UI/workflow around that.

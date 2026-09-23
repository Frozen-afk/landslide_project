# High-value pass — P1 progress

Implements `POST_AUDIT_HIGH_VALUE_PLAN.md` §2 P1 only ("Splat the orthophoto so it can
actually be traced", H8 ortho half). P2–P6 and V1 are untouched, per the plan's own
"every item is independent" framing and this task's explicit scope.

## What was implemented

`landslide/ortho.py::render_orthophoto` — exactly the plan's design, no deviation:

- Each cloud point is splatted into a `k x k` pixel block instead of one pixel,
  `k = clip(ceil(median_spacing / res), 1, 7)`.
- `median_spacing` is the same k-d tree median-2NN trick `ground.estimate_cell_size`
  already used, now factored out as `geometry.median_point_spacing(u, v, sample)` and
  called from both (`ground.py` couldn't import it from `ortho.py` — `ground.py` already
  imports `ortho.ground_basis`/`select_region_world`, so an `ortho -> ground` import
  would be circular; `geometry.py` has no dependency on either, so the shared helper
  lives there instead — same trick, one implementation, no behavior change to
  `estimate_cell_size`'s existing 0.1 m fallback for a near-empty cloud).
- The splat writes points in the same ascending-height order the single-pixel code
  already relied on for "highest point wins" (`order = np.argsort(h)`); each point's
  block is written as one contiguous slice of that order, so a later (higher) point's
  block always overwrites an earlier (lower) point's block on any shared pixel,
  regardless of which point's block is larger.
- Chunked 200k points at a time so the `(points x k^2)` index/color temporaries stay
  bounded on a multi-hundred-thousand-point cloud instead of scaling with the full
  point count x 49.
- The `[ortho]` log line's covered-cell count now reflects the post-splat image
  (painted-vs-background-color pixel count) instead of the pre-splat unique-pixel count,
  per the plan's "report the post-splat covered fraction" requirement.
- `ortho.json`'s fields (`u0, v0, res, width, height, up, e1, e2, scale`) are untouched —
  the splat only changes which pixels get written, not the coordinate mapping.

## Regression coverage added

`tests/test_ortho.py::test_render_orthophoto_splats_sparse_cloud` — a 15x15 lattice at
exactly 5x the render resolution (so `k` computes to 5, not clamped at either end),
asserts covered fraction >= 0.8, and separately asserts a point marked much higher than
its neighbors still wins the shared pixel after the block-write refactor. Full fast suite
(`pytest -q -k "not e2e"`) still passes: 155 passed (unchanged count + 1 new).

## Validation

Ran `tools/benchmark.py` and two standalone probe scripts against the repo's cached SfM
+ dense-cloud work directories (copied to a scratch dir so the git-tracked
`data/bench/*/artifacts/ortho.jpg` baseline files weren't touched), covering the plan's
six scale-able presets: arc, lowtex, distorted, collinear, descending, sparse8.

**Accuracy / API compatibility — met.** `ortho_vol_err_pct` from a full
benchmark run matched `data/bench/results.md` to two decimal places on every preset
except `descending` (arc 25.90, lowtex 26.33, distorted 23.88, collinear 30.33, sparse8
32.30 — all identical): unreachable without `ortho.json`'s meta fields being pixel-for-
pixel unchanged, since `select_region_ortho` maps polygon pixels through those fields.
This directly confirms the "ortho.json byte-identical" and "volume within ±1pp"
acceptance criteria for arc/lowtex/distorted.

**Runtime — met.** Isolated `render_orthophoto` calls (dense cloud pre-loaded from
cache) measured 0.05–0.30 s across all six presets — well under the 1 s/preset budget.

**Coverage — improved, target not met on this environment's current dense clouds.**
Comparing the OLD (one-pixel-per-point) and NEW (splatted) background fraction computed
from the *same* locally-reconstructed dense cloud for each preset (isolates the splat's
effect from any cloud-to-cloud difference):

| preset | n points | k | raw (old) bg | splatted (new) bg | plan target |
| --- | --- | --- | --- | --- | --- |
| arc | 201406 | 3 | 0.903 | 0.767 | <=0.20 |
| lowtex | 213395 | 3 | 0.902 | 0.771 | <=0.20 |
| distorted | 234643 | 3 | 0.896 | 0.763 | <=0.20 |
| collinear | 398689 | 3 | 0.724 | 0.555 | <=0.20 |
| descending | 166247 | 2 | 0.829 | 0.673 | <=0.20 |
| sparse8 | 142801 | 2 | 0.932 | 0.855 | <=0.20 |

The splat is real and correctly implemented — 13-17 percentage points of background
removed on every preset, matching the plan's formula exactly — but stops well short of
the plan's own <=0.20 acceptance bar everywhere. Root cause: `median_point_spacing` is an
isotropic estimate, but T1.4's multi-view depth-consensus fusion produces clouds that are
locally patchy/anisotropic (this is already documented in `architecture.md` §3.7's raster
cross-check discussion — "real stereo clouds have locally sparse-but-continuous
patches"). A `k` sized off the *median* gap closes the typical gap but leaves the wider
gaps in sparser patches unfilled, and the plan's own `k <= 7` cap (chosen to bound
render cost/memory, not tuned against measured coverage) caps how far a bigger `k` could
even close them.

*Caveat on the absolute numbers:* the dense clouds reconstructed locally in this pass
(142k-399k points) are measurably sparser than whatever produced the committed
`data/bench/*/artifacts/ortho.jpg` files this environment's own git history references —
directly reading those committed JPEGs back gives background fractions of 0.60-0.88,
close to but not exactly the plan's quoted 0.56-0.85 (JPEG compression bleed at the
dark/lit boundary inflates a naive re-measurement slightly). That gap is a pre-existing
local-environment reconstruction/library-version difference, not something this change
introduced — the *relative* before/after comparison above holds regardless, since both
sides of it come from the identical, currently-cached cloud.

**`descending`'s scale/volume numbers in the full benchmark run are not comparable to
`results.md` and are not a P1 regression.** The full-pipeline run showed descending's
scale error jump from the committed 0.52% to 15.26% and photo-mode volume error from
(untracked in this row) to 43.27% — entirely upstream of `ortho.py` (SfM + ArUco scale,
neither touched here). `architecture.md` §7.1 already documents that `descending`'s SfM
convergence is not reproducible run-to-run
(`test_descending_focal_spread_is_stable_across_clean_runs`); this repo's cached
`data/bench/descending/work/` currently holds a worse attempt than the one
`results.md` was generated from, most likely left behind by an earlier session's
non-cached e2e test run. Confirmed independent of the splat change: the probe script
that isolates just `render_orthophoto` shows nothing in the ortho render itself
behaves differently for `descending` than for any other preset.

## Deviations from the plan

None in the implementation. One acceptance criterion (`background fraction <= 0.20`) is
not met on any of the six presets as measured in this environment, for the root cause
above — reported honestly rather than loosened or worked around, since raising `k`'s cap
or switching to an anisotropic spacing estimate is a design change beyond what P1
approved.

## Status

**Implemented, tested, partially meets acceptance.** Safe to ship as-is — zero
compatibility risk confirmed (matches plan's own risk assessment), volumes and
`ortho.json` unchanged, real coverage improvement, no runtime regression. The
`<=0.20 background` target is not reached; flagging as a remaining risk rather than
re-scoping P1 to chase it.

## P1 correction (per `P1_CORRECTION_PLAN.md`) — implemented and validated

The `<=0.20 bg_bbox` target above is withdrawn per `P1_CORRECTION_PLAN.md`'s own
measurement: the traced polygon is only 12-21% of the bounding box, and 39-62% of the
polygon itself is genuinely unobserved ground (>10 cm from any cloud point) — reaching
0.20 on either metric requires painting ground nobody measured. Root cause of the
remaining in-polygon speckle was the old formula's block radius (one spacing *diameter*,
should be one spacing *radius*) and its even-`k` off-centre block, not cloud anisotropy
(measured 10-NN axis ratio ~1.3, no direction to exploit).

**Change applied** — `landslide/ortho.py::render_orthophoto`, one line:

```python
k = int(np.clip(2 * np.ceil(spacing / res) + 1, 1, 7))
```
(replaces `k = int(np.clip(np.ceil(spacing / res), 1, 7))`; docstring updated to
"block radius = one median spacing"). No other line changed — chunked write order,
highest-point-wins logic, `ortho.json` fields, and `geometry.median_point_spacing` are
untouched, as the plan required.

**Tests added** (`tests/test_ortho.py`): `test_render_orthophoto_jittered_cloud_no_speckle`
(jittered lattice at spacing ~1.5x res — k=2 under the old formula — asserts >=99% interior
coverage) and `test_render_orthophoto_keeps_genuine_gap` (same lattice with a >=20-spacing
hole punched out — asserts the hole interior stays background). `pytest -q -k "not e2e"`:
**157 passed** (155 + 2 new), matching criterion 6 exactly.

**Focused validation** — six scale-able presets, probed against each preset's cached dense
cloud in a scratch copy (`data/bench/<preset>/work`, not the tracked `artifacts/`), same
method `P1_CORRECTION_PLAN.md` §Method describes:

| preset | bg_bbox (info) | bg_poly | gap | speckle | fabricated | bg_poly - gap | render (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| arc | 0.733 | 0.409 | 0.403 | 0.0093 | 0.0000 | 0.006 | 0.54 |
| lowtex | 0.738 | 0.409 | 0.403 | 0.0096 | 0.0000 | 0.006 | 0.51 |
| distorted | 0.731 | 0.397 | 0.391 | 0.0104 | 0.0000 | 0.006 | 0.54 |
| collinear | 0.505 | 0.425 | 0.420 | 0.0138 | 0.0000 | 0.005 | 0.85 |
| descending* | 0.601 | 0.630 | 0.615 | 0.0146 | 0.0000 | 0.015 | 0.27 |
| sparse8 | 0.817 | 0.543 | 0.525 | 0.0203 | 0.0000 | 0.017 | 0.19 |

\* `descending`'s cached dense cloud is the same stale/worse SfM attempt already flagged
above; excluded from criterion 4 below per the plan.

`gap`, `bg_poly` and `bg_bbox` reproduce `P1_CORRECTION_PLAN.md`'s own table to within
0.001 on every preset, confirming this environment's cached clouds match what the plan's
analysis used (unlike the P1 pass above, where locally-reconstructed clouds diverged from
the committed baseline).

**Acceptance status against the plan's revised criteria:**

1. **Speckle <= 0.01 on every preset — PARTIALLY MET.** arc (0.0093) and lowtex (0.0096)
   pass; distorted (0.0104), collinear (0.0138), descending (0.0146) and sparse8 (0.0203)
   exceed the bar. All six are a large improvement over pre-correction P1 (0.027-0.068,
   itself already down from the no-splat 0.29-0.50) — roughly another 2-6x reduction — but
   4 of 6 presets do not clear the strict <=1% line. The plan's own "expected 0.000-0.009"
   figure came from a fixed-`k`-parity diagnostic probe (`speckle odd k=5` in the plan's
   table), not a re-run of the exact approved one-line formula; measured directly, the
   approved formula's residual speckle on patchier clouds (collinear, descending, sparse8 —
   all already flagged elsewhere as harder-to-reconstruct presets) lands a few points above
   that figure.
2. **Fabricated fill == 0 — MET.** Exactly 0.0000 on all six presets; no painted pixel
   inside the polygon lies more than 10 cm from a cloud point.
3. **bg_poly - gap in [0, 0.03] — MET.** 0.005-0.017 across all six presets.
4. **`ortho.json` byte-identical + `ortho_vol_err_pct` matches P1 to 2 dp — MET.** Ran
   `tools.benchmark` on the scratch copy for arc/lowtex/distorted/collinear/sparse8:
   25.90 / 26.33 / 23.88 / 30.33 / 32.30 — identical to `results.md` and to the P1 pass's
   own quoted values. Each preset's freshly-written `ortho.json` meta diffs byte-for-byte
   equal (`json.tool`-normalized) against the tracked `data/bench/<preset>/artifacts/ortho.json`.
5. **render_orthophoto <= 1 s/preset — MET.** 0.19-0.85 s across all six.
6. **`pytest -q -k "not e2e"` passes, 157 tests — MET.**
7. **bg_bbox reported for information only — done** (table above; 0.505-0.817, unchanged
   in kind from the P1 pass, no longer a gate).

**Status: implemented, tested, criteria 2/3/4/5/6/7 met, criterion 1 (speckle <=1%)
met on 2/6 presets and missed by 0.4-1.0 pp on the other 4.** Shipping as-is: the fix is
strictly better than pre-correction P1 on every preset and every metric, changes no
volume or `ortho.json` field, and the residual speckle is bounded by the same <=7 px cap
already approved (cannot bridge more than ~2 median spacings). Closing the remaining gap
on collinear/descending/sparse8 would mean revisiting the design (per-point local spacing,
a higher cap, or accepting a different bound) — out of this correction's one-line scope,
so not attempted here.

## Remaining risks / follow-up (not undertaken here — out of P1's scope)

- `k`'s formula (median-spacing-derived, capped at 7) does not close the coverage gap to
  the plan's target on any measured preset. A follow-up would need to either measure
  coverage directly and grow `k` (or use an anisotropic/local spacing estimate) until a
  target is met, or the plan's `<=0.20` bar should be re-derived against what a bounded
  splat can actually achieve — the same "don't chase an uncalibrated threshold" caution
  the plan itself applies to H6's `min_cos` and V1's coverage gate.
- `data/bench/descending/work/` (untracked, local-only cache) currently holds a worse SfM
  attempt than `results.md`'s committed numbers. Not fixed here (out of scope, and no
  application code change would fix a cache directory) — a fresh
  `tools/benchmark.py --presets descending` run, or deleting the stale cache before the
  next full benchmark pass, would resync it.
- P2-P6 and V1 remain as scoped in `POST_AUDIT_HIGH_VALUE_PLAN.md`, untouched.

---

# High-value pass — P4 progress

Implements `POST_AUDIT_HIGH_VALUE_PLAN.md` §2 P4 only ("Report which 'up' was chosen",
H1 reporting half). P1/P2 (above) are untouched by this pass; P3, P5, P6 and V1 are
untouched, per this task's explicit "P4 only" boundary.

## What was implemented

`landslide/densify.py::estimate_up` — matches the plan's design exactly: a new optional
`info: dict | None = None` parameter, filled at every `return` with `up_source`
("scene_plane" | "camera_plane"), `disagree_deg` (the angle between the two candidates,
or `None` in the two branches where no meaningful comparison happens — `scene_up is
None`, or the collinear-path early return, where the camera-plane candidate is itself
degenerate) and `collinearity`. No call-site signature changes: all seven other callers
(`densify.py:745`, `ortho.py:69`, `ground.py:273`, `change.py:104`, `server/routes.py:290`,
`tools/benchmark.py:163`, plus the two `tests/test_up.py` pre-existing calls) pass no
`info` and are byte-for-byte unaffected.

`landslide/pipeline.py::measure` (photo-mode branch only) — passes `info=up_info` to its
existing `estimate_up` call and publishes `res["up_source"]` / `res["up_disagree_deg"]`
right beside the existing `res["region_method"]` assignment.

`landslide/gates.py::evaluate_gates` — new gate: `up_disagree_deg > 20°` (the vote, not
agreement, decided which candidate is "up") flags `indicative` with a reason naming both
the disagreement angle and the source chosen.

`server/static/js/steps/result.js::showResult` — one new row, "up vector source", shown
beside "region selection" when `r.up_source` is present, including the disagreement angle
when there was a genuine one to report.

## Scope note: photo mode only, not ortho mode

The plan's acceptance criterion reads "`up_source` and `up_disagree_deg` present in every
`measure()` result", without a mode qualifier. In practice this pass follows the *existing*
convention already set by `region_method` itself: `pipeline.py` only ever sets
`res["region_method"]` inside the `mode == "photo"` branch (confirmed — grep finds exactly
one assignment site, `pipeline.py:244`); `mode == "ortho"` results have never carried it,
and `result.js` already guards every such field with `if (r.field)`. Ortho mode's `up` is
read from the cached `ortho.json` meta (computed once, at render time, by
`ortho.py:69`'s own `estimate_up` call) rather than recomputed at measure time, so there is
no `info` dict available there without either (a) a second, redundant `estimate_up` call
solely to populate reporting fields, or (b) writing into `ortho.json`'s meta — the latter
is exactly what P1 fenced off as a byte-identical-compatibility boundary for saved jobs.
Given P4's own "zero numeric change" / "compatibility risk: none" framing, this pass adds
`up_source`/`up_disagree_deg` to photo-mode results only, mirroring `region_method`'s own
precedent, and does not touch `ortho.py` or `ortho.json`. Flagged as a remaining-risk item
below rather than silently narrowed.

## Regression coverage added

`tests/test_up.py::test_info_reports_source_and_disagree_angle_on_genuine_disagreement` —
exactly the plan's own §P4 acceptance case: a scene ground plane tilted 25° under an arc of
cameras (high enough that the camera-plane candidate stays near true vertical), asserting
`disagree_deg` is within 2° of the constructed 25° and that `up_source` names the branch
that actually produced the returned vector (checked against the true normal of whichever
candidate `up_source` claims, not just that the string is one of the two valid values).

`tests/test_e2e_synth.py::test_volume_end_to_end` — two assertions added to the existing
photo-mode e2e case: `res["up_source"] in ("scene_plane", "camera_plane")` and
`"up_disagree_deg" in res`, covering the plan's "present in every measure() result (e2e
assertion)" criterion for the photo-mode path.

`.venv/bin/python -m pytest -q -k "not e2e"`: **164 passed, 1 skipped** (163 baseline from
the P2 pass + 1 new `test_up.py` case). No existing assertion changed.

## Focused validation

`.venv/bin/python -m pytest -q tests/test_e2e_synth.py::test_volume_end_to_end -v` — full
SfM + dense stereo + measure() run on the `arc` preset: **1 passed**, confirming
`up_source`/`up_disagree_deg` are present and well-formed in a real pipeline result, not
just the isolated unit test.

**"Zero numeric change" reasoned, not re-benchmarked over all 8 presets.** This pass adds
no code on any path that computes `net_volume_m3`, `cut_volume_m3`, `scale`, or any other
existing numeric field — `estimate_up`'s return value is identical whether or not `info` is
passed (the new parameter only writes into a dict the caller supplies; every existing
`return` statement returns the exact same vector it did before). The new gate can only ever
add an `"indicative"` entry to `reasons`; per `POST_AUDIT_HIGH_VALUE_PLAN.md` §0.2, no
preset in the benchmark reaches `status = "ok"` (G6's coverage gate alone already forces
`indicative` or `rejected` on all eight), so the new gate cannot change any preset's
`status` — only whether one additional reason string is present, which is the change's
entire intended effect. A full `tools/benchmark.py` re-run was not needed to confirm this
and was not run.

## Deviations from the plan

None. `disagree_deg` is `None` (rather than always a float) in the two branches where no
genuine two-candidate comparison happens (`scene_up is None`; collinear camera path) — not
specified either way by the plan, and the only sensible value when the "disagreement"
being asked about was never computed.

## Acceptance status against the plan's criteria

1. **New `tests/test_up.py` case, disagree_deg within 2° of a constructed 25° tilt,
   `up_source` names the branch taken — MET.**
2. **`up_source`/`up_disagree_deg` present in every `measure()` result — MET for photo
   mode** (the mode `estimate_up` is actually called from at measure time); **not extended
   to ortho mode**, per the scope note above.
3. **Zero numeric change across all eight presets' benchmark volume/scale columns — MET
   by construction** (reasoned above; no numeric-producing code path touched).
4. **Compatibility risk: none — MET.** Additive result fields plus one optional parameter
   with a safe default; no `response_model` constrains the measure endpoint's returned
   dict (`server/routes.py`); no existing test's assertions changed.

## Status

**Implemented, tested, acceptance met (with the ortho-mode scope note above, not a
deviation from what was approved but a boundary this pass chose conservatively).** Safe to
ship as-is.

## Remaining risks / follow-up (not undertaken here — out of P4's scope)

- Ortho-mode results do not carry `up_source`/`up_disagree_deg` (see scope note). If this
  is wanted later, the lowest-risk route is a second, cheap `estimate_up(..., info=...)`
  call in `pipeline.py`'s ortho branch purely to populate the reporting fields (discarding
  its returned vector — the actual computation keeps using `ortho.json`'s cached `up`), not
  a change to `ortho.json` itself.
- P4 makes the up-vector disagreement *visible* and flags it; it does not resolve H1's
  original concern (a hillside scene has no `hillside25` preset to validate against, and
  the "level the marker" gravity override remains deferred) — unchanged from the plan's own
  "Deferred from H1" note.
- P3, P5, P6 and V1 remain as scoped in `POST_AUDIT_HIGH_VALUE_PLAN.md`, untouched.

---

# High-value pass — P2 progress

Implements `POST_AUDIT_HIGH_VALUE_PLAN.md` §2 P2 only ("ArUco detection at severe
angles", H2 detection half). P1 (above) is untouched by this pass; P3-P6 and V1 are
untouched, per this task's explicit scope.

## What was implemented

`landslide/scaling.py::detect_marker_corners` / `_detector_params` (new) /
`_refine_corners_full_res` (new) — **not** exactly the plan's original design; one part
of the plan (`CORNER_REFINE_APRILTAG`) was tried, measured to actively regress
detection, and replaced. Full account below.

1. **Detector parameters** (`_detector_params`): `adaptiveThreshWinSizeMin=3,
   Max=53, Step=4` (widened from stock 3/23/10) and `polygonalApproxAccuracyRate=0.06`
   (relaxed from stock 0.03) — as planned. **`cornerRefinementMethod`, however, is
   `CORNER_REFINE_SUBPIX`, not the plan's `CORNER_REFINE_APRILTAG`** — see Deviations.
2. **Full-resolution corner refinement** (`_refine_corners_full_res`, called from
   `detect_marker_corners` when `path` is given): when the shared detection image was
   downscaled (`s < 1.0`), each accepted marker's corners are refined with
   `cornerSubPix` on a crop of the ORIGINAL full-resolution image instead of the
   downscaled one, window size scaled by `round(5 / s)` (capped `[5, 15]`) so the
   window covers the same *relative* fraction of the marker regardless of resolution.
   When no downscale happened (`s >= 1.0` — every photo in this codebase's own
   synthetic benchmark, since all are 1200x900 against a 2200px `max_side`), this path
   is skipped entirely and behavior is byte-identical to the pre-P2 code.
3. `aruco_scale`'s one call site now passes `path=v.path` (positionally, so the
   existing fully-mocked tests in `tests/test_scaling.py` are unaffected).

## Deviations from the plan (both found during validation, not assumed)

**1. `CORNER_REFINE_APRILTAG` was implemented first, per the plan, then reverted after
measurement showed it actively discards valid detections.** Isolated per-photo count
(stock `DetectorParameters()` vs. stock+APRILTAG-only, nothing else changed) on this
codebase's own synthetic photos: `arc` 21/21 -> 10/21, i.e. APRILTAG's own internal
refinement/rejection logic threw out 11 previously-valid detections on the *easiest*
preset in the benchmark, before any of this task's other changes even run. The same
isolated count showed `nadir` unaffected (9 -> 9) and `oblique60` unaffected (0 -> 0),
so APRILTAG's damage was concentrated on the well-conditioned presets, not the two the
plan targets — a straight loss with no offsetting gain. Replaced with
`CORNER_REFINE_SUBPIX`, verified by the same isolated count to match the stock
detector's raw hit count on all 8 presets and gain one view on `nadir` (9 -> 10).

**2. The full-resolution refinement's window size must be scaled by the downscale
factor, not fixed.** The first implementation used a fixed `(11, 11)` window on the
full-res crop regardless of `s`. On a same-reconstruction A/B (see Validation method
below) this measured *worse* than the pre-P2 code on every preset that scales at all —
`arc`'s `reproj_px_mean` went from 1.11px (old) to 3.56px (fixed-window new) and
`side_spread_rel` from 0.54% to 2.87%, propagating into scale error. Root cause,
confirmed by inspecting one view's raw corner shift: with `s == 1.0` (no downscale
occurred — true for this benchmark's 1200x900 photos), "refining on the full-res crop"
re-reads the *same* image and a window of 11px is simply too large relative to this
marker's bit-cell pitch (~11px/cell at this apparent size) — `cornerSubPix` converges
onto an adjacent bit-pattern corner 10px away instead of the intended outer corner.
Fixed by (a) scaling the window to `round(5 / s)` so it covers the same relative
footprint the original tuned `win=5` did, and (b) skipping the full-res path entirely
when `s >= 1.0`, since there is no extra resolution to gain by re-reading an image that
was never downscaled. Re-verified after the fix: `arc`'s `reproj_px_mean` 1.11px (old)
vs. 1.10px (fixed new, `s==1.0` path never engages) — exact parity, as expected.

Both deviations were caught by the same-reconstruction A/B method below, not by the
full pipeline benchmark alone — a full benchmark run mixes SfM's own run-to-run
non-determinism (already documented, `architecture.md` §7.1) into every column, which
would have hidden a detector-side regression of this size inside normal run-to-run
noise on presets like `arc` (baseline table shows plausible-looking but SfM-driven
1.27% -> 1.30%/0.73% swings across otherwise-identical runs; see Validation).

## Validation method

Two complementary checks, chosen because a single fresh `tools/benchmark.py` run
conflates the code change with SfM's own documented non-determinism
(`architecture.md` §7.1 — descending's focal-spread convergence "is not reproducible
run-to-run"; observed directly in this pass too, see below):

**(A) Same-reconstruction A/B (primary — isolates the code change).** For each of the
8 presets, `sfm.reconstruct(reuse=True)` loads the identical cached camera poses from
one SfM run, then `aruco_scale` is run twice against those SAME poses/photos: once
with the pre-P2 `detect_marker_corners`, once with the current code. Any resulting
delta is attributable to the detection code alone, not to which SfM attempt a given
run happened to land on.

**(B) Fresh full-pipeline runs (secondary — end-to-end sanity, not a regression
signal).** `tools/benchmark.py` run twice with a completely fresh SfM
reconstruction each time (no cache reuse): once immediately after the plan's original
APRILTAG design (before Deviation 1/2 were found), once after both fixes, over
`arc, nadir, oblique60` (the two directly-targeted presets plus a control).

## Measured results

**(A) Same-reconstruction A/B, all 8 presets, OLD (pre-P2) vs. NEW (this pass, after
both deviations fixed):**

| preset | old views | old spread% | old scale | new views | new spread% | new scale |
| --- | --- | --- | --- | --- | --- | --- |
| arc | 21 | 0.54 | 3.27241 | 21 | 0.49 | 3.27176 |
| collinear | 21 | 0.61 | 2.16027 | 21 | 0.66 | 2.15970 |
| descending | 19 | 0.72 | 3.40087 | 19 | 0.77 | 3.40104 |
| distorted | 21 | 0.88 | 3.31006 | 21 | 0.81 | 3.31256 |
| lowtex | 21 | 0.50 | 3.28583 | 21 | 0.45 | 3.28518 |
| nadir | n/a | n/a | REFUSED (14% side spread) | n/a | n/a | REFUSED (14% side spread) |
| oblique60 | n/a | n/a | REFUSED (visible in 1 view) | n/a | n/a | REFUSED (no marker found) |
| sparse8 | 8 | 0.88 | 2.77459 | 8 | 0.95 | 2.77444 |

Six working presets: view counts identical, spread/scale deltas all <0.1 percentage
point — the code change is neutral on every preset it wasn't targeting, confirming
Deviation 2's fix actually closed the regression it found (the pre-fix version of this
same table showed spread 4-8x worse on every one of these six).

**(B) Fresh full-pipeline runs** (`arc/nadir/oblique60`, brand-new SfM each time):

| preset | before fix (APRILTAG) | after both fixes |
| --- | --- | --- |
| arc scale err % | n/a (this run: 0.73, a different fresh run: 1.30) | 1.30 |
| nadir | REFUSED, 15% spread | REFUSED, 50% spread |
| oblique60 | REFUSED, no marker found | REFUSED, no marker found |

`arc`'s two "before fix" numbers (0.73% and 1.30%, from two different fresh SfM runs
using the buggy code) already bracket the "after fix" 1.30% and the original committed
baseline's 1.27% — i.e. run-to-run SfM variance on this preset alone (~0.6 percentage
points) is comparable to or larger than the entire effect being measured, which is
exactly why (A) and not raw before/after benchmark deltas is the result that should be
trusted. `nadir`'s spread swinging 15% -> 50% between two fresh runs (both correctly
refused either way) is the same phenomenon — `architecture.md` §3.8a/G4 already
documents nadir's near-collinear-equivalent camera geometry as leaving per-camera
calibration underconstrained, so which SfM attempt wins the retry ladder measurably
changes the marker triangulation quality run to run, independent of detection code.

## Root cause found for `oblique60`: out of P2's scope

`oblique60`'s ArUco failure is **not a detector-tuning problem** and no combination of
`DetectorParameters` fixes it. Reprojecting the marker's known 3D corners
(`ground_truth.json`) through each view's own ground-truth camera pose shows the
marker board's top edge is ABOVE the image's top edge (negative pixel `y`) in 19 of 21
views — e.g. view `IMG_10`: corner `y` range `[-17.9, 97.0]` against a 900px-tall
image. Only the two extreme-yaw frames (`IMG_00`, `IMG_20`) have the whole marker
in-frame at all, and only barely (`ymin = 3.6px`). This is a **camera-path/scene
framing property of `tools/synth.py`'s `oblique60` preset** (its `height=6.0,
radius=10.4` geometry relative to the marker board's fixed world position), fixed at
preset-generation time and identical on every run regardless of reconstruction or
detector code — confirmed by the fresh full-pipeline run in (B) reproducing "no marker
found in any photo" deterministically. Changing it means editing `tools/synth.py`'s
preset geometry, which is out of `scaling.py`-scoped P2 and not attempted here.

## Regression coverage added

`tests/test_scaling.py`, four new tests (one parametrized x5):
- `test_detector_params_tuned_for_severe_angle` — asserts the shipped parameters
  (`CORNER_REFINE_SUBPIX`, widened threshold window, relaxed polygon tolerance) differ
  from stock, pinning the actual shipped choice (not APRILTAG).
- `test_full_res_crop_refinement_beats_downscaled_subpix` — a synthetic marker
  rendered small in a large canvas, downscaled to emulate a real oversized phone photo
  (`s < 1`, unlike this benchmark's own images): confirms the full-res crop path
  strictly reduces mean corner error vs. plain `cornerSubPix` on the downscale, and
  reaches sub-1.5px accuracy. This is the only test that exercises the `s < 1` path at
  all, since no preset in this repo's own benchmark scene needs a downscale.
- `test_tuned_detector_never_loses_a_stock_detection` (parametrized over 5 marker
  size/blur combinations) — regression guard pinning the exact property Deviation 1
  violated: the tuned detector must be a superset of the stock detector's raw hits,
  never a subset.

`.venv/bin/python -m pytest -q -k "not e2e"`: **163 passed, 1 skipped** (157 baseline +
6 new parametrized cases; 1 skip is the `half=10, blur=0` case in the parametrized
guard test, where the stock detector itself misses the synthetic marker so there is
nothing to guard). No existing test's assertions changed.

## Acceptance status against the plan's criteria

1. **`oblique60`: marker detected in >=2 views, scale error <=5% — NOT MET.** Root
   cause is the preset's camera framing (above), not detection sensitivity; no
   detector-side change can find a marker that isn't rendered inside the frame in
   19/21 views. This is a plan-invalidating finding for this one criterion, parallel to
   how A1 invalidated H3's original "arc error <=8%" criterion — recorded here rather
   than silently dropped.
2. **No regression on the six presets that already scale (`|Δ scale err| <= 0.5pp`)
   — MET**, per the controlled same-reconstruction comparison (A): all six deltas are
   <0.1pp. (A raw fresh-benchmark delta would not reliably show this, per (B) above —
   the plan's own 0.5pp bar is tighter than this codebase's measured SfM run-to-run
   noise floor on at least `arc` and `nadir`.)
3. **`nadir`: must either still refuse or produce <=5% — MET.** Refuses in every run
   observed (14-50% side spread across different SfM attempts), never silently
   accepts a marginal detection.
4. **`tests/test_scaling.py` passes unchanged — MET** (all 50 pre-existing cases still
   pass; 4 new test functions added per the plan's "add minimum regression coverage"
   instruction, not part of the plan's original 47-case count).

**Status: implemented, tested, 2 of 3 measurable acceptance criteria met (#2, #3); #1
not achievable by this item's scope (root cause is `tools/synth.py` scene geometry,
not `scaling.py`).** Shipping the detector/refinement changes as-is: they are
measured-neutral on every preset that was already working, strictly better on `nadir`
(one extra usable view, still safely refused), and the parts of the plan that turned
out to be wrong (`CORNER_REFINE_APRILTAG`, a fixed refinement window) were caught by
validation and corrected before shipping rather than merged as originally spec'd.

## Remaining risks / follow-up (not undertaken here — out of P2's scope)

- `oblique60` needs a `tools/synth.py` preset-geometry fix (e.g. a lower marker board,
  a taller image, or a narrower vertical FOV) to ever produce a measurable ArUco
  result — a scene-authoring change, not a detection-algorithm change. Flagging for
  whoever scopes preset fixes; not attempted here per this task's explicit "P2 only"
  boundary.
- The full-resolution refinement path (`s < 1`) is validated only by the targeted
  synthetic unit test, not by this repo's own benchmark scene — every one of its 8
  presets renders at 1200x900, under the 2200px downscale threshold, so `s == 1.0`
  always and that code path never executes in `tools/benchmark.py`. It is real,
  field-photo-relevant behavior (a real phone photo is typically 3000-4000px and does
  get downscaled) that this environment's synthetic scene cannot exercise end-to-end;
  the field-accuracy gap this leaves is the same standing H7 precondition
  (`POST_AUDIT_HIGH_VALUE_PLAN.md` §1) already named as blocked, not new here.
- `nadir`'s side-spread swinging 14-50% across otherwise-identical fresh SfM runs
  (all correctly refused either way) reconfirms the pre-existing G4/`architecture.md`
  §7.1 finding that near-collinear camera paths leave reconstruction quality run-to-run
  unstable — unchanged by this pass, already tracked as out of scope for H2.
- P3-P6 and V1 remain as scoped in `POST_AUDIT_HIGH_VALUE_PLAN.md`, untouched.

---

# High-value pass — P5 progress

Implements `POST_AUDIT_HIGH_VALUE_PLAN.md` §2 P5 only ("DEM alignment: use the dense
cloud, search heading", H5 reduced). P1/P2/P4 (above) are untouched by this pass; P3, P6
and V1 are untouched, per this task's explicit "P5 only" boundary.

## What was implemented

**P5(b) — dense-cache reuse, done first (P5(a) builds on it).**
`landslide/densify.py` — the cache-lookup half of `dense_cloud` (the `ctx.dense is not
None` short-circuit and the on-disk `dense_<w>_v2_<fingerprint>.npz` fingerprint check)
is factored out, unchanged, into a new `load_cached_dense(ctx, log=print,
stereo_width=1280) -> dict | None`. `dense_cloud` now calls it as its first step instead
of duplicating the same two checks — same cache key, same fingerprint contract, zero
behavior change (verified: `dense_cloud`'s own `test_stale_dense_cache_is_rejected`
passes unchanged). `load_cached_dense` never runs stereo — it only reads `ctx.dense` or
the disk cache — so it is safe to call from a request thread; building a dense cloud
stays confined to `dense_cloud`, called only from `server/worker.py` (inside the process
pool) and `pipeline.py`'s own worker-side paths, exactly as the plan requires.

`landslide/dem.py::align_to_dem` — replaces the unconditional `ctx.cloud(dense=True)`
(which silently falls back to sparse because the parent process's `ctx.dense` is always
`None`) with `load_cached_dense(ctx, log=log)`: if it returns a non-empty cloud, align on
that; otherwise fall back to `ctx.sparse` exactly as before, with a log line naming which
path was taken. No `dense_cloud` import in `dem.py` at all — there is nothing to
monkeypatch-forget-to-call, the module simply has no path that can build one.

**P5(a) — yaw sweep.** `align_to_dem` gains a heading search between the existing
gravity-rotation seed and the final ICP refinement: `yaw_starts=12` seeds spaced
30° apart about `up` (new helper `_axis_R`, a general Rodrigues rotation — `_gravity_R`
is kept as-is, it already solves the tilt half), each probed with a short
`yaw_probe_iters=4` trimmed ICP run (translation seeded fresh per yaw, since rotating a
non-origin-centred cloud about `up` moves its centroid); the lowest-probe-RMS heading is
then refined with the existing full 25-iteration `icp_rigid` call, unchanged from before.
`align_to_dem`'s two new parameters (`yaw_starts`, `yaw_probe_iters`) default exactly to
the plan's own numbers; the one existing call site (`server/routes.py:291`) passes
neither, so its call is source-unchanged.

## Scope note: `yaw_probe_iters=4`, not a literal 12 full ICP runs

The plan's wording ("seed 12 starts ... keep the best trimmed RMS") reads as 12 full ICP
runs. Run naively that is ~12x the runtime of one full run, far over the plan's own "≤ 3×
current" budget. Implemented instead as 12 *short* (4-iteration) probes to rank headings,
then one full 25-iteration refinement from the winner — 12×4 + 25 = 73 iterations total
vs. baseline 25, a 2.92x iteration-count ratio, measured at 2.86x wall time (below).
Chosen because ICP's early iterations already separate "roughly the right heading" from
"wrong heading" (correspondences start converging vs. staying scattered) well before full
convergence — confirmed by the yaw-sweep test below recovering a 120° offset to near-zero
RMS with this scheme, while a single full-length gravity-only seed on the same scene
converges to 0.13 m and stays there (see Measured results). Flagged as a deviation rather
than silently narrowed, since "keep the best trimmed RMS" could be read either way and
the literal 12x reading would have blown the plan's own runtime acceptance criterion.

## Regression coverage added

`tests/test_dem.py`, three new cases:
- `test_align_to_dem_yaw_sweep_recovers_120deg_heading` — the plan's own acceptance case:
  a relief scene (road + hill + pile, enough geometry for heading to be observable) is
  DEM'd against itself with a **120° heading offset** applied to the model cloud before
  alignment. Asserts `rms_m < 0.1` (the plan's bar) and that the fallback-to-sparse log
  line fired (no dense cache present in this fixture, exercising that path too).
- `test_align_to_dem_prefers_cached_dense_cloud` — `ctx.dense` set to the true DEM points,
  `ctx.sparse` set to a cloud offset 50 m away (garbage). Asserts alignment succeeds
  (`rms_m < 0.05`, only possible if the dense cloud was used, not the 50 m-off sparse one)
  and that the "cached dense cloud" log line fired.
- `test_align_to_dem_never_builds_a_dense_cloud` — the plan's own acceptance case:
  monkeypatches `landslide.densify.dense_cloud` to raise `AssertionError` if called at
  all, then runs a normal `align_to_dem` call and asserts it still succeeds — proving no
  code path in `align_to_dem` can reach `dense_cloud`.

`.venv/bin/python -m pytest -q tests/test_dem.py`: **9 passed** (6 baseline + 3 new).
`.venv/bin/python -m pytest -q -k "not e2e"`: **167 passed, 1 skipped** (164 baseline from
the P4 pass + 3 new `test_dem.py` cases). No existing assertion changed.

## Focused validation

Ran a standalone probe against the repo's cached `arc` preset SfM + dense-cloud (copied
to a scratch dir, tracked `data/bench/arc/*` untouched), covering the plan's two
acceptance criteria at realistic scale (`arc`'s real dense cloud: 201406 points, capped to
the same 60000-point budget `align_to_dem` itself applies).

**(a) Yaw sweep, 60k points — alignment quality and runtime:**

| seed strategy | recovered rms (120° offset) | wall time |
| --- | --- | --- |
| single gravity-only seed (old behavior, `iters=25`) | 0.134 m — does not converge | 9.28 s |
| yaw sweep (12×4-iter probes + 25-iter refine, new) | 0.000 m — recovers exactly | 26.49 s |

Runtime ratio: **2.86×** — under the plan's `≤ 3×` bar. Alignment quality: the plan's own
`< 0.1 m` bar is met (0.000 m here because the synthetic "DEM" in this probe is the model
cloud itself under a known transform, i.e. a noise-free upper bound — `test_dem.py`'s
independent test above, with a *different* relief scene, also clears the bar cleanly).

**(b) Dense-cache reuse — point count and RMS, dense vs. sparse, same DEM:**

| cloud used | points | rms |
| --- | --- | --- |
| dense (cached, this pass) | 201406 | 0.000 m |
| sparse (fallback, old behavior) | 7140 | 0.060 m |

Confirms the plan's acceptance wording directly: after an ortho/measure run left a dense
cache on disk, DEM alignment now uses **28× more points** and lands on a **lower RMS**
than the sparse cloud it silently used before.

**Existing DEM/change suite:** all 6 pre-existing `tests/test_dem.py` cases pass
unchanged (`test_dem_volume_recovers_pile`, `..._aligned_after_rigid_move`,
`test_load_dem_xyz_text`, `test_icp_rigid_rejects_contamination`,
`test_change_volume_between_epochs`, `test_change_marker_anchored_registration`) — the
plan's "existing 6 DEM/change cases unchanged" criterion.

## Deviations from the plan

One, already flagged above: `yaw_probe_iters=4` short probes per heading seed instead of
12 full-length ICP runs, to meet the plan's own runtime budget. No other deviation —
`load_cached_dense`'s extraction is byte-identical logic to what `dense_cloud` already
did, and the monkeypatch test confirms `align_to_dem` never reaches `dense_cloud`.

## Acceptance status against the plan's criteria

1. **New `tests/test_dem.py` case, 120° heading offset aligns to < 0.1 m RMS (fails
   today) — MET.** Test passes; the focused-validation table above additionally shows the
   old single-seed behavior stalling at 0.134 m on the same kind of offset, confirming the
   "fails today" premise directly rather than just asserting the new behavior.
2. **Existing 6 DEM/change cases unchanged — MET.**
3. **`align_to_dem` wall time ≤ 3× current at 60k points — MET** (2.86× measured).
4. **After an ortho run, DEM upload logs a point count > the sparse count and reports a
   lower RMS on the same DEM — MET** (201406 vs. 7140 points, 0.000 m vs. 0.060 m rms,
   both directly measured and logged).
5. **With no cache present the sparse path still works and says so — MET**
   (`test_align_to_dem_yaw_sweep_recovers_120deg_heading` runs with no dense cache and
   asserts the "no cached dense cloud" log line).
6. **A monkeypatch test asserts `dense_cloud` is never called from the parent process —
   MET** (`test_align_to_dem_never_builds_a_dense_cloud`).

**Status: implemented, tested, all six acceptance criteria met.** Compatibility risk
realized as none: the one call site (`server/routes.py:291`) is source-unchanged, both new
`align_to_dem` parameters default to the plan's own numbers, and `dense_cloud`'s own cache
behavior is provably unchanged (its own pre-existing stale-cache test still passes,
byte-identical logic just relocated).

## Remaining risks / follow-up (not undertaken here — out of P5's scope)

- The literal "12 full ICP runs" reading of the plan, if ever wanted exactly as written,
  would need either a materially larger runtime budget or a cheaper per-probe correspondence
  step (e.g. a coarser voxel subsample per probe) — not attempted here since the
  probe-then-refine scheme already meets every stated acceptance number.
- Moving DEM alignment into a worker job remains explicitly excluded (plan's own
  "Excluded" note) — this pass's runtime win (2.86× not 12×) makes that tradeoff milder
  than the plan assumed but does not revisit the decision.
- P3, P6 and V1 remain as scoped in `POST_AUDIT_HIGH_VALUE_PLAN.md`, untouched.

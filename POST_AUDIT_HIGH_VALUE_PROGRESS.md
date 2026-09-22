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

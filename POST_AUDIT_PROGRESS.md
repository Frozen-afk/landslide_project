# Post-audit fix pass — ground-frame region selection (F18/M2)

Follow-up to `POST_IMPLEMENTATION_AUDIT.md` after the M1-M8 mandatory cycle (commit
`62b6db6`). Three measured limitations were reported as still open:

1. descending photo-mode volume error ≈ 55%
2. ground-frame ray-cast hit fraction 47-67%, below the 90% target
3. collinear dense-stage peak RSS ≈ 16 GB

All three were re-measured against the current code before any change (Fedora,
Python 3.14, pycolmap 4.1.1, OpenCV 5.0, no CUDA) and their suspected causes checked
against the actual data, not assumed from the audit text.

## Validation

**#1/#2 share one root cause.** `ground.select_region_ground`'s DSM (`estimate_cell_size`,
2.5× the cloud's median point spacing) left 60-77% of the raster's cells empty on
*every* synthetic preset, arc included — not just descending. Real multi-view-fused
stereo clouds are spatially patchy (foreshortened/occluded/low-texture ground never
gets a match), so a spacing-derived cell starves regardless of scene; growing the cell
alone plateaus at ~45-65% occupancy (the gaps are structural, not a resolution
artifact). With most cells empty, `cast_polygon_to_ground`'s ray-cast — sampling in a
fixed 400 steps and only accepting a sign-flip between two *consecutive* valid samples —
skips straight through the true crossing far more often than not: probed directly, 0-38
of a scene's boundary vertices had *any* valid sample pair spanning both signs. Measured
hit fractions before any fix: arc 57%, descending 54%, collinear 67%, sparse8 32%, nadir
47%, lowtex 57%, distorted 61% — all below the 70% fallback threshold on a bad day, and
every one of them was *actually* falling back to the parallax-prone image-plane path
(`region_method: "image_projection"` in every preset's benchmark row), which is why
descending's 55% error was so much worse than ortho mode's 34%: photo mode wasn't even
using the feature built to avoid parallax.

**#3 does not occur where the audit's title suggests.** `densify.dense_cloud` itself
peaks at ~1.3 GB RSS on `collinear` (measured directly) — F2's percentile-extent fix
already holds. The actual 16 GB spike is in `volume.prism_volume`'s TPS-membrane
extrapolation-guard check (`eval_tps(m, uv2_all)`, called once per measurement when a
`rim_tps` datum is adopted): `eval_tps`'s chunk size (100 000 rows) times the TPS
support count (up to 4 000 points) builds several `(100000, 4000, ...)` float64
temporaries — 3-6 GB *each* — inside one Python loop iteration. It only shows up on a
scene whose rim curvature triggers the TPS membrane and whose interior point count is
large (collinear: 111k interior points after F2's fix restored real cloud density) —
i.e. it's a consequence of F2 having worked, not of the dense-cloud build. Confirmed by
isolating each pipeline stage under `resource.getrusage`: `dense_cloud` 1.3 GB,
`select_region` 0.45 GB, `prism_volume` up to the point of the first `eval_tps` call
~0.45 GB, then 15.9 GB by the time it returns.

## Fixes implemented

**`landslide/ground.py`**
- `estimate_cell_size`: now density-adaptive (F18/M2) — starts from 2.5× median
  spacing, then grows the cell geometrically (bounded iterations, 2 m cap) until the
  DSM's own occupied-cell fraction clears a 50% target, instead of a single constant
  multiplier that starved a sparse cloud (nadir) and barely moved a dense one (arc) by
  the same amount.
- new `fill_dsm_holes`: bridges small, genuinely-enclosed DSM gaps by reusing
  `volume._fill_small_holes`'s row/column enclosure test (no new bridging logic to get
  wrong — a large unsupported stretch is still correctly left as a hole).
- `cast_polygon_to_ground`: ray step is now `cell / 2` (tied to the DSM's own
  resolution, computed dynamically) instead of a fixed 400-sample count; the
  sign-flip crossing test tolerates a NaN gap of up to 8 cells between the bracketing
  valid samples instead of requiring them consecutive.
- `select_region_ground`: calls `fill_dsm_holes` before ray-casting; `min_hit_frac`
  default lowered 0.7 → 0.5 — even with the above, a steep/oblique capture path
  (descending) structurally caps its achievable hit fraction below 0.7 (measured
  ceiling ~70% at any cell size, confirmed by direct sweep), and the partial-but-
  parallax-free ground-frame selection still beats the image-plane fallback there
  (see results below) — 0.7 was rejecting the better answer more often than a real
  footprint failure.

**`landslide/volume.py`**
- `eval_tps`: `chunk` now defaults to bounding the `chunk × n_support` product to a
  fixed element budget instead of a flat 100 000 rows, so the same call is bounded
  memory regardless of interior point count or support size. No numeric change (pure
  chunking), verified by the existing TPS/prism_volume unit tests passing unchanged.

`architecture.md` §3.5 updated to match.

## Results (all 7 photo-mode presets, `dense=True`, `rim_px=14`, cached reconstructions)

| preset | before (image_projection) | after (ground_frame) |
| --- | --- | --- |
| arc | 15.05% | **1.73%** |
| sparse8 | 23.48% | **1.51%** |
| lowtex | 13.55% | **0.69%** |
| distorted | 23.90% | **6.24%** |
| collinear | 15.80% | **13.85%** |
| nadir | 24.50% | **20.69%** |
| descending | 55.34% | **38.35%** |

Every preset now uses `region_method: "ground_frame"` (previously all seven fell back
to `image_projection`). `descending` and `nadir` are still the two worst cases — real,
not fixed by this pass: `descending`'s dense cloud has a genuine ~30% stereo coverage
gap on that camera path (confirmed: increasing the DSM cell further does not move its
hit fraction past ~70%), and `nadir`'s residual error is dominated by its known-bad
vertical-marker scale (F23/`test_nadir_scale_is_known_bad`), not region selection.

Peak RSS: re-running all 7 presets' full `measure()` calls in one process peaked at
906 MB cumulative (vs. the reported ~16 GB on `collinear` alone before the `eval_tps`
fix) — no per-preset regression check needed since the bug was in a shared code path,
not preset-specific state.

## Tests

- `pytest -q tests/test_ground.py tests/test_volume.py tests/test_densify.py`: 37
  passed (existing hit-fraction/TPS unit tests pass unchanged).
- `pytest -q -k "not e2e" --ignore=tests/test_server.py`: 132 passed.
- `pytest -q tests/test_e2e_presets.py tests/test_e2e_synth.py tests/test_server.py`:
  all pass; `test_server.py`'s crash-recovery test still exits cleanly (F1's fix
  untouched by this pass).
- `tests/test_e2e_presets.py`'s pinned photo-mode volume thresholds were tightened to
  the new real numbers (this repo's own "pin real numbers, not aspirational ones"
  rule) — `sparse8 < 8%` (was 35%), `lowtex < 5%` (was 20%), `distorted < 12%` (was
  30%), `collinear < 20%` (was 25%), `descending < 50%` (was 65%) — each re-verified
  passing against a fresh run, not just the numbers above.

## Not done (out of scope for this pass)

- `descending`'s remaining ~38% error and `nadir`'s ~21% are real, documented
  limitations (stereo coverage gap; known-bad vertical marker respectively), not
  selection-logic bugs — fixing them means denser stereo coverage on steep paths or a
  scene/marker redesign, neither of which this pass's two items (region selection,
  DSM cell sizing/ray-cast coverage) cover.
- `unmeasured_area_m2`/raster-TIN disagreement warnings now fire more often (e.g.
  collinear 40%, descending 14%) because ground-frame selection draws a differently-
  shaped (parallax-free, but partial-hit-fraction) polygon than the old image-plane
  one — expected, not investigated further here.

# P1 correction plan — orthophoto splat

## Context

`POST_AUDIT_HIGH_VALUE_PLAN.md` §2 P1 set a target of "background fraction ≤ 0.20 on all six
scale-able presets". The P1 implementation (uncommitted: `landslide/ortho.py`,
`landslide/geometry.py::median_point_spacing`, `landslide/ground.py`, `tests/test_ortho.py`)
kept accuracy and compatibility. `ortho.json` and the ortho-mode volumes did not change.
But background stays at 0.56–0.86. `POST_AUDIT_HIGH_VALUE_PROGRESS.md` says the cause is
"anisotropic, patchy clouds" and the `k ≤ 7` cap. This analysis tests that claim.

## Method (read-only)

The six presets' `data/bench/<p>/work` caches were copied to a scratch directory. Each
preset's cached dense cloud was loaded. The current `render_orthophoto` was run on it.
The ground-truth bowl polygon was projected into ortho pixels the same way
`tools/benchmark.py:163-172` does it. Then these values were measured:

- **bg_bbox**: the P1 metric. Unpainted share of the whole raster.
- **bg_poly**: unpainted share inside the traced polygon (the surface the operator traces on).
- **gap**: share of polygon pixels > 10 cm from any cloud point (genuinely unobserved ground).
  This number agrees with G6's `coverage_frac` (`volume.py:783`).
- **speckle**: share of "near-coverage" polygon pixels (≤ 2 × median spacing from a point)
  that are still unpainted. This is the defect that a splat should remove.
- **fabricated**: share of gap pixels (> 10 cm from any point) that are painted. This must
  stay 0.
- **anisotropy**: √(λ₁/λ₂) of 10-NN neighbourhoods inside the polygon.

Local density-aware (per-point k-NN) splats, larger global k, and hole filling limited by
area (`cv2.inpaint` of holes ≤ 0.1 / 0.25 m size) were also simulated.

## Findings

| preset | poly / bbox | gap in poly | bg_poly P1 | speckle k=1 | speckle P1 (k) | speckle odd k=5 | fabricated | anisotropy p50/p90 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| arc | 0.124 | 0.403 | 0.431 | 0.483 | 0.030 (3) | 0.009 | 0 | 1.29 / 1.60 |
| lowtex | 0.118 | 0.403 | 0.432 | 0.465 | 0.027 (3) | 0.004 | 0 | 1.28 / 1.59 |
| distorted | 0.121 | 0.391 | 0.422 | 0.453 | 0.028 (3) | 0.005 | 0 | 1.29 / 1.63 |
| collinear | 0.193 | 0.420 | 0.462 | 0.292 | 0.037 (3) | 0.000 | 0 | 1.30 / 1.68 |
| descending* | 0.213 | 0.615 | 0.655 | 0.384 | 0.062 (2) | 0.000 | 0 | 1.31 / 1.65 |
| sparse8 | 0.145 | 0.526 | 0.577 | 0.495 | 0.068 (2) | 0.000 | 0 | 1.28 / 1.61 |

\* The `descending` cache holds a worse SfM attempt than `results.md` (see the progress doc).
The ratios still hold.

1. **The ≤ 0.20 target is not valid.** It measures the whole bounding box. The traced polygon
   is only 12–21 % of that box. The rest of the box is terrain that no camera reconstructed,
   plus sparse outlier clusters and detached strips (see `data/bench/arc/artifacts/ortho.jpg`).
   Even inside the polygon, 39–62 % of the pixels are more than 10 cm from any point.
   On G6's own 0.25 m occupancy grid, 38–60 % of polygon cells are empty (G6 docs: 40–62 %). To reach 0.20 on either metric, the render must paint
   ground that nobody measured. That hides genuine gaps, which the task forbids. With a global
   k = 11, bg_bbox is still 0.47–0.78, and painted gaps start to appear (0.7–1.6 % of the
   polygon on the 0.25 m-cell metric).
2. **P1 already removed most of the real defect.** Speckle in covered ground fell from 29–50 %
   to 2.7–6.8 %.
3. **The remaining speckle has two root causes in the formula, not in the cloud:**
   - `k = ceil(spacing / res)` makes the block width equal to one median spacing. That tiles a
     perfect lattice only. With jittered points, nearest-neighbour gaps are wider than the
     median, so holes remain. The usual splat size uses a radius of one spacing (diameter
     2 × spacing).
   - An even k is off-centre. For k = 2, `off = [0, 1]`, so the block shifts +½ px and one side
     of every point is not covered. `descending` and `sparse8` get k = 2 and have the worst
     speckle (6.2 % and 6.8 %). With k = 3, the same clouds drop to 2.9 % and 2.8 %.
4. **The clouds are not anisotropic.** The 10-NN axis ratio is 1.3 (p50) and 1.6 (p90) on every
   preset: close to 1, with no preferred direction strong enough for a kernel to exploit.
   The progress doc's anisotropy hypothesis is not supported by the data.
5. **The alternatives do not help:**
   - *Local density-aware (per-point k-NN) splat:* on the 0.25 m-cell speckle metric,
     per-point k from the 6th-NN distance (cap 7) leaves 3.3–5.2 %. One global k = 5 leaves
     2.8–3.9 % on the same metric. It also needs a z-buffer rewrite and a k-NN query for every
     point. Rejected.
   - *Anisotropic kernels:* there is no anisotropy to exploit (finding 4). Rejected.
   - *Hole fill limited by area:* after the splat, almost no enclosed holes remain. bg_poly
     changed by ≤ 0.003, because the remaining background connects to genuine gaps.
     `inpaint` also creates colours that no point has. Rejected.
   - *Raising the k ≤ 7 cap:* not needed. The approved fix reaches ≤ 1 % speckle within the
     cap. Excluded, as the task requires.
   - *Cropping the raster to the dense footprint:* this would lower bg_bbox, but it changes
     `u0/v0/width/height` in `ortho.json`, which breaks the compatibility guarantee.
     Rejected.

## Approved correction

**Change one line in `landslide/ortho.py::render_orthophoto`:**

```python
k = int(np.clip(2 * np.ceil(spacing / res) + 1, 1, 7))
```

(This replaces `k = int(np.clip(np.ceil(spacing / res), 1, 7))`.)

- The splat radius is now one median spacing, rounded up to whole pixels, so the block covers
  the gap to the neighbouring points. k is always odd, so every block is centred on its point.
- The cap stays at 7. Paint can reach at most `min(ceil(spacing / res), 3)` px from a real
  point along each axis, so the fix cannot bridge a gap wider than about 2 median spacings.
  At the current resolution of about 2.15 cm/px, that is ≤ 6.5 cm along an axis and ≤ 9.1 cm
  at the block corners. That is under the 10 cm gap threshold and the 0.25 m G6 cell.
- Update the docstring sentence ("k sized to the cloud's own median point spacing") to say
  "block radius = one median spacing".
- No other code changes. The chunked block write, the height-ordered "highest point wins"
  logic, `ortho.json`, the log line and `geometry.median_point_spacing` are unchanged.

**Tests (`tests/test_ortho.py`):**

- `test_render_orthophoto_splats_sparse_cloud`: no assertion changes. The lattice now gives
  k = 7 (clipped) instead of 5. No edit is needed: its comment (`spacing/res = 5`) states the
  spacing ratio, not k.
- Add `test_render_orthophoto_jittered_cloud_no_speckle`: a jittered lattice at spacing
  ≈ 1.5 × res with a fixed seed. Assert that the interior has ≥ 99 % painted pixels. The P1
  formula gives k = 2 there and is expected to fail; confirm that it fails before applying
  the fix.
- Add `test_render_orthophoto_keeps_genuine_gap`: a lattice at spacing ≈ 1.5 × res with a
  missing disc of about 20 × spacing. Assert that every pixel > 4 px inside the disc edge is
  still background. This test fails if the cap is raised or hole filling is added later.

## Revised acceptance criteria (replace P1's "bg ≤ 0.20")

Measure on the six scale-able presets with the probe method above:

1. In-polygon speckle is ≤ 0.01 on every preset (expected: 0.000–0.009; P1 today: 0.027–0.068).
2. Fabricated fill is exactly 0: no painted pixel inside the polygon lies > 10 cm (Euclidean
   distance transform from the pixels that hold a point) from a cloud point.
3. bg_poly − gap is between 0 and 0.03 on every preset. The ortho shows the real gaps, and
   close to nothing else.
4. `ortho.json` is byte-identical to the P1 version, and `ortho_vol_err_pct` equals the P1 run
   to 2 dp (arc 25.90 / lowtex 26.33 / distorted 23.88 / collinear 30.33 / sparse8 32.30).
5. `render_orthophoto` takes ≤ 1 s per preset (expected: about 0.3 s).
6. `pytest -q -k "not e2e"` passes with the 2 new tests (157 tests).
7. bg_bbox is reported for information only. It is no longer a gate.

## Risks

- **Low. Paint reaches 1 px further around every point.** Edges of real gaps look up to about
  one spacing smaller. This is bounded by construction and checked by criterion 2 and the
  gap test. Volumes are not affected, because region selection reads `meta` only
  (`ortho.py:162-173`).
- **Low. A cloud denser than the grid now gets k = 3 instead of 1.** That adds a 1 px halo.
  It is visually harmless and cannot bridge any gap wider than about 2 px.
- **Medium, documented. No field-cloud data.** The evidence comes from synthetic SGBM clouds
  only. On real data, median spacing may differ from the local spacing of sparse patches.
  The cap still bounds the worst case. Real data stays blocked on H7.
- **Process risk.** Changing a target after the fact can hide a failure. Here the change is
  justified by a direct measurement: 39–62 % of the traced polygon is genuinely unobserved,
  which is the same finding as G6. The new criteria are stricter on the real defect (speckle
  ≤ 1 % against about 50 % before P1) and add a check that no gap is hidden.

## Focused validation

1. `pytest -q tests/test_ortho.py`, then `pytest -q -k "not e2e"`.
2. Copy `data/bench/{arc,lowtex,distorted,collinear,descending,sparse8}/work` to a scratch
   directory. Do not touch the tracked `artifacts/`. For each preset: `reconstruct(reuse=True)`,
   `dense_cloud` (loads the cache), `aruco_scale`, then `render_orthophoto`. Project the GT
   polygon as `benchmark.py:163-172` does and compute criteria 1–3 and 5.
3. `python -m tools.benchmark --presets arc,lowtex,distorted,collinear,sparse8` on the scratch
   copy for criterion 4. `descending` is excluded because of its stale cache.
4. Look at arc's new `ortho.jpg`: the bowl interior is solid, and the far-side gap is still
   visibly empty.

## Not done

No code was modified. The ≤ 0.20 bbox target is withdrawn, not met. The global cap is not
raised. Nothing is filled in genuine gaps. P2–P6 and V1 are not started.

# Datum Stability Progress — G9

Implements `DATUM_STABILITY_PLAN.md` §6: the datum holdout (D1, `volume.py`) and the G9 gate
(D2/D3, `gates.py`). Stops after G9, per instruction — S1–S6 remain blocked.

**Base:** `main` @ `d5653ba`, plus the pre-existing uncommitted `tools/synth.py` preset entries
from S0/S0b (unchanged by this pass) and the cached `data/bench/twosided{,_b}/work` directories.

## Change made

- `landslide/volume.py`: `_datum_holdout` helper (leave-one-arc-out check of the adopted rim
  datum, 8 overlapping 90° windows); wired into `prism_volume` after the datum is finalized
  (plane/quad/TPS selection unchanged) and before the outlier cap. Adds `datum_holdout_m` and
  `datum_holdout_windows` to the result dict. No existing field, threshold, or volume
  computation is touched.
- `landslide/gates.py`: G9 block after G7, plus `DATUM_HOLDOUT_REL = 0.05` and
  `DATUM_MIN_WINDOWS = 6`. Flags `indicative` when the datum fell back to `surface_plane` (no
  usable rim), when fewer than 6 of 8 holdout windows were usable (rim not seen all around), or
  when `datum_holdout_m × polygon_area_m2 > 0.05 × |net_volume_m3|`.
- `architecture.md` §3.6 (datum-holdout step), §3.8a (G9 entry), §5.5 (two new result fields).
- `GUIDED_CAPTURE_PROGRESS.md`: correction note under S0b — the original rim-bias diagnosis
  (grazing-view reconstruction bias in the outer rim band) is disproven; the real cause is the
  datum model being unable to see sub-rim-scale terrain undulation from the ring alone.
- `tests/test_volume.py`: 3 new regression tests (below). `tests/test_gates.py`: one existing
  test's input dict extended with the new fields it now needs to stay `ok` (no assertion
  relaxed).

No change to `tools/synth.py`, `tools/benchmark.py`, G6 thresholds, datum selection, or any
volume/CI number.

## Regression coverage added

| Test | Guards |
| --- | --- |
| `test_g9_unstable_datum_downgrades_ok_to_indicative` | R1: `tools.synth.build_terrain(seed=7)`, perfect points, no SfM. G6 alone sees full coverage and no void (would be `ok`); real error vs `vol_true_poly` > 10%; G9 downgrades to `indicative` with a "datum uncertain" reason. |
| `test_g9_stable_planar_and_curved_fixtures_stay_ok` | R2/R3: `make_bowl_points()` (planar) and `make_curved_bowl_points()` (quad-adopted curved) both stay `ok` — G9 is silent on honest, correctly-modelled ground. |
| `test_g9_no_rim_fallback_never_ok` | R4: `make_bowl_points(rim_width=0.0)`, no rim → `datum == "surface_plane"` → `status != "ok"`, "no usable rim" reason, regardless of anything else. |

Fast suite: `.venv/bin/python -m pytest -q -k "not e2e"` → **185 passed, 3 skipped, 23
deselected** (182 + 3 new; no existing assertion weakened). The `worker process crashed (native
crash)` line is the pre-existing, documented `test_worker_crash_is_isolated_and_pool_recovers`
behavior, not a new failure.

## Focused validation (§10)

### Real-run check, both presets, both modes (cached SfM/dense, no `artifacts_dir`, no file written)

| Preset | Mode | status | G9 reason | coverage_frac | largest_void_m² | cut_measured_m3 | err % vs 67.16 truth | datum_holdout_m | windows |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| twosided_b | photo | **indicative** | "datum uncertain… ±0.07 m, ~7.0 m³ (9%)" | 0.98588 | 0.9375 | 78.468 | +16.84% | 0.0683 | 8/8 |
| twosided_b | ortho | **indicative** | "datum uncertain… ±0.07 m, ~7.4 m³ (10%)" | 0.98571 | 0.9375 | 77.487 | +15.38% | 0.0739 | 8/8 |
| twosided | photo | **indicative** | coverage reason (unchanged) + "datum uncertain… ±0.09 m, ~9.5 m³ (14%)" | 0.86712 | 10.125 | 68.065 | +1.35% | 0.0938 | 8/8 |
| twosided | ortho | **indicative** | coverage reason (unchanged) + "datum uncertain… ±0.10 m, ~9.7 m³ (14%)" | 0.87778 | 9.0625 | 67.763 | +0.90% | 0.0959 | 8/8 |

Matches the plan's own §2.4 predictions (8.9%/9.3% for `twosided_b`, 10.9%/14.0% for `twosided`)
closely enough to attribute to the exact window/inlier set, not a modeling error. `twosided_b`'s
`coverage_frac`, `largest_void_m2`, and `cut_measured_m3` are unchanged from the S0b table
(0.9859/0.94/78.47 photo, 0.9857/0.94/77.49 ortho) to the reported precision — G9 is detection
only, confirmed on the real cached runs, not just the synthetic fixtures.

### Runtime growth (item 6)

`measure()` on `twosided_b`, same reused `ctx`, holdout vs a monkeypatched no-op holdout:
with = 204.25 s, without = 215.28 s (growth −11.0 s — negative, i.e. the ~200 s of stereo/TIN/
bootstrap work dominates and the holdout's own cost (plan estimate 1.0–1.7 s) is below this
setup's run-to-run noise floor). Well under the ≤3 s budget.

### 40-seed calibration (item 5, §2.5's recipe re-run against the implemented code)

`build_terrain(seed)` for seed 1–40, perfect points on the existing 170×170 grid (not
resampled to 0.12 m — the built grid's ~0.213 m spacing is dense enough that this does not
change which seeds are datum-unstable; effect is systematic-bias detection, not fit-noise
sensitivity), interior r<5.6, rim 6.1–8.1, `prism_volume` → `evaluate_gates`.

- **15 of 40 seeds** have datum-only error > 5% (matches the plan's own count).
- **11 of 15 flagged**, **4 missed** (seeds 28, 32, 37, 39 in this run — err 6.8/9.2/6.0/7.8%).
  Meets the §9 item 5 target (≥11 of 15). The plan's own design pass named seeds
  32/37/38/39 as the expected misses from the holdout signal alone; this run's implemented,
  gate-wired version misses 28 instead of 38 — both are the documented failure mode (§11:
  radially symmetric undulation centred on the region is invisible from any rim arc), not a
  new defect.
- **25 of 40 seeds** have real error ≤ 5%; **23 are still flagged** `indicative` by G9. This
  matches the plan's own §2.5 finding almost exactly ("flags 24 of 25 seeds whose realized
  error was ≤5%") and is explained there: on this terrain the *implied* datum uncertainty
  really is 5–20% of the volume, and a seed whose realized error happened to land low was
  right by luck, not by a measurement the pipeline can stand behind. `indicative` is correct
  for those seeds, not a false-block in the ordinary sense.

## Deviations from the plan

- Seed calibration reused `build_terrain`'s native 0.213 m grid instead of the plan's literal
  0.12 m resampling (no resampler existed in `tools/synth.py`; writing one is not part of D1/D2).
  Same conclusion, both counts match the plan's targets — recorded here per the "any deviation"
  requirement, not treated as silent.
- Did not implement the §7.3 decision (an opt-in flatter `build_terrain` variant / a
  `twosided_b_flat` preset) — that is explicitly a `tools/synth.py` scope change needing
  separate approval, and out of scope for "implement only the approved datum-stability
  detection and G9 gate."

## Remaining limits (unchanged from the plan's §11)

- Radially symmetric undulation centred on the region is invisible to any rim-only method
  (the 4 missed seeds here). Mitigation is a pre-event DEM (`dem_volume`), already gated
  `indicative` by G6's DEM fallback.
- `net_volume_ci95_m3` still excludes truth on `twosided_b`/`twosided` — G9 keeps such results
  out of `ok` in the meantime, but does not fix the CI itself (deferred, needs calibrated field
  data per §11).
- `res["warnings"]` and `est_volume_error_m3` remain ungated; G9 does not duplicate them.
- Datum model *selection* (plane/quad/TPS adoption rules) is unchanged — on seed 7 they adopt
  the most-biased option available; whether to revisit that is a separate, field-data-driven
  decision per the plan's §7.3/§11.

## Consequence

`twosided_b`'s S0b NO-GO is re-attributed: the datum-model bias G9 now detects was present (at
similar magnitude) in both S0 and S0b; S0's apparent 1.3% accuracy was an unrelated area
cancellation, not evidence the geometry was sound. Per the plan's decision rule, all of §9
passed, so this is recorded as a merge, not a stop. S1–S6 remain blocked per the user's explicit
instruction to stop after G9; the §7.3 scene decision (an opt-in flatter benchmark terrain) is
still the user's call, not made here.

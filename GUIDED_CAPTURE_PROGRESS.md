# Guided Capture Progress — S0 and S0b

Tracks `GUIDED_CAPTURE_PLAN.md` §12 stage S0 ("Calibration data") and §12a stage S0b
("Protocol correction"). S1–S6 not started, per instruction.

**Base:** `main` @ `d5653ba`, plus this pass's one-line `tools/synth.py` change.

## S0 — Corrected `twosided` geometry and benchmark

### What was wrong

`tools/synth.py`'s `PRESETS["twosided"]` comment said "two arcs 90 deg apart... `arc_offset=90`
keeps a ~6 deg overlap between arms", but the dict actually set `arc_offset=180.0`. At 180°
the two 96°-wide arcs (`arc2` path) are on directly opposite sides of the bowl with zero yaw
overlap between them — `arm B: [132°, 228°]` vs `arm A: [-48°, 48°]` — which is outside the
protocol's own 60–120° separation guidance (`GUIDED_CAPTURE_PLAN.md` §3, §6) and would leave
no shared content for SfM to register both arms into one model.

### Fix

`tools/synth.py`: `arc_offset=180.0` → `arc_offset=90.0` (one line). This matches the existing
comment exactly and sits inside the 60–120° protocol range. No other code changed — `n_views`,
`yaw_span`, `radius`, `height`, `f_px` are unchanged from the plan's own "unmodified copy of
arc's geometry" description, and the `middle`-view selection in `synth.py:main` already used
arm A's own centre regardless of `arc_offset`, so the GT polygon framing is unaffected.

### Benchmark commands (clean cache — `data/bench/twosided` did not exist before this pass)

```
.venv/bin/python -m tools.benchmark --presets twosided --out data/bench --md-out /tmp/twosided_bench.md
```

Result (30/30 registered, one `/usr/bin/time -v`-measured subprocess, real 9m01s / peak RSS
1576 MB):

| preset | registered | scale err % | photo vol err % | ortho vol err % | cloud RMS (m) | runtime (s) | peak RSS (MB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| twosided | 30/30 | 1.64 | 1.39 | 0.90 | 0.25 | 540.04 | 1576.44 |

`tools/benchmark.py`'s own table has no status/coverage columns (they were never wired into
`_run_preset_subprocess`'s output row, in any preset). To get the G6 class this stage exists to
record, the same reconstruct → `aruco_scale` → `measure` calls `run_preset` makes were re-run
directly against the now-cached `data/bench/twosided/work` (no new SfM, same cached model
`benchmark.py` had just built), reading the full result dict `measure()` already returns
(`res["status"]`, `res["reasons"]`, `res["coverage_frac"]`, `res["largest_void_m2"]`, …).
This is not a new code path — `pipeline.measure` already computes and returns these fields
(`architecture.md` §3.8a); `benchmark.py` just doesn't print them. No file was changed to get
this reading.

### Measured accuracy, coverage, and gate results

Ground truth: 67.16 m³ cut inside the traced polygon (same as every other preset).

| Mode | status | reasons (gate) | coverage_frac | largest_void_m² | hit_frac | cut_measured–cut_upper (m³) | net CI95 (m³) | scale_rel_error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| photo | **indicative** | "87% coverage, largest unmeasured patch 10.1 m² — the volume interpolates across it" | 0.867 | 10.1 | 1.00 (0 misses) | 68.07–104.69 | net −84.0…−52.1 | 1.30% |
| ortho | **indicative** | "88% coverage, largest unmeasured patch 9.1 m² — the volume interpolates across it" | 0.878 | 9.1 | n/a (ortho) | 67.76–101.07 | net −82.2…−53.3 | 1.30% |

No other gate fired: G1 (dense cloud) fine (registered 30/30, dense build completed), G2 (marker
warnings) none, G3 (focal spread) none, G4 (collinearity) none, G5 (ray-cast) `hit_frac = 1.0`
in photo mode, up-vector disagreement 5.9° (well under the 20° gate). **The only thing keeping
`twosided` out of `ok` is G6's void-size sub-condition** (`largest_void_m2 > 2.0`) — the
coverage fraction itself (86.7–87.8%) already clears the 0.85 `ok` floor.

Accuracy is the headline change from every single-arc preset: `cut_measured` (68.07 photo /
67.76 ortho) sits within 1.3% of the 67.16 truth on both sides — inside scale uncertainty —
instead of single-arc's documented 24–43% low bias (`architecture.md` §7.1, `FINAL_RELEASE_
AUDIT.md` §2). Coverage (86.7–87.8%) is roughly 1.4–1.8× every single-arc preset's 48–62%.

### Resource use

Full clean-cache run (image generation + SfM + dense stereo + both measures, `/usr/bin/time -v`
on the child process): **540 s runtime, 1576 MB peak RSS** for 30 views at defaults (2 threads
at the 3200px retry tier, 4 elsewhere per `sfm.SFM_THREADS`). Well under the worker memory
budget fixed in B4 (`RELEASE_BLOCKER_PROGRESS.md`) on this 23 GB dev box; not tested against a
small-RAM/many-core host in this pass (out of S0 scope — that is §6 item 6 of
`FINAL_RELEASE_AUDIT.md`, a field-validation item, not a calibration-data item).

### Deviations from the plan's S0 description

- The plan's S0 exit check only asks to "Build `data/bench/twosided`, run
  `tools/benchmark.py`. Record its final G6 class." `tools/benchmark.py`'s printed table has no
  status/G6 column for any preset (not just `twosided`), so a direct read of the `measure()`
  result dict was needed to get the class at all — documented above, not a code change, and not
  scope creep since no file besides `tools/synth.py`'s one line was touched to produce it.
- Did not add a status/coverage column to `tools/benchmark.py`'s output table. That would be a
  reporting improvement useful to every preset, not specific to fixing `twosided`'s geometry —
  outside "implement only S0," left for a later pass if wanted.
- Did not attempt a second `arc_offset` value (e.g. splitting the difference between 60–120°) to
  see if a different separation clears the void-size gate. The plan's own evidence base names
  90° specifically (matching the pre-existing, only-slightly-wrong comment); trying other values
  until one passes G6 would be exactly the "adjust thresholds/geometry to fit the result"
  instructed against. The corrected geometry is benchmarked and reported as measured — no
  further tuning was performed.
- Ran the fast suite (`pytest -q -k "not e2e"`) after the change: 182 passed, 3 skipped, 23
  deselected — no regressions. (The `worker process crashed (native crash)` line in the output
  is `test_worker_crash_is_isolated_and_pool_recovers` deliberately killing a worker, a known,
  documented test behavior — not a new failure.) The slow e2e preset suite
  (`tests/test_e2e_presets.py`) was not run for the other 7 presets — this pass touches none of
  their config, and running all 8 presets' SfM+dense+measure is a S0-unrelated multi-preset
  regression pass FINAL_RELEASE_AUDIT.md already covers elsewhere.

### Go/No-Go decision

**NO-GO on the plan's own stop condition** (`GUIDED_CAPTURE_PLAN.md` §12, S0 exit check:
"If `twosided` does not reach G6 `ok`, stop and revisit the protocol before building guidance
toward it.") — `twosided` reaches `status = indicative` in both photo and ortho modes, not
`ok`. Per the plan's explicit instruction, this pass stops after S0. S1–S6 (instant checks,
capture session backend, guided UI, preview SfM, coverage/recapture, result-side mapping) are
**not started**.

This is not a rejection of the two-azimuth idea — the corrected geometry is a large, real
improvement over every single-arc preset (coverage 1.4–1.8× higher, volume error within ~1.3%
of truth instead of a 24–43% low-biased range) — but it does not clear G6's `ok` bar as
currently specified (90° separation, 30 views, one board marker), and the gap is entirely the
void-size sub-condition (10.1/9.1 m² vs the 2 m² cap), not the coverage-fraction sub-condition
(86.7–87.8% already clears 0.85). Before building capture guidance toward this exact protocol
(§5.3's calibration step, S4/S5), the design needs to account for this residual void — for
example more views, a third vantage, or elevation/framing changes aimed specifically at the
void's location — rather than treating "two azimuths ≥ 60° apart" alone as sufficient. That
investigation is protocol design work, i.e. S4/S5+ territory, and is explicitly out of scope
for this pass.

## S0b — `twosided_b` validation (`GUIDED_CAPTURE_PLAN.md` §12a)

**Base:** `main` @ `d5653ba`, plus S0's `arc_offset` fix and this pass's one new `tools/synth.py`
preset dict entry. No other file changed.

### Change made

Added `PRESETS["twosided_b"]` to `tools/synth.py`, exactly the dict §12a.4 specifies:
`n_views=38, yaw_span=123.43, radius=26.0, height=9.0, height_ramp=0.0, f_px=1600.0,
tex_scale=1.0, k1=0.0, arc_offset=90.0` (`arc2` path). This is height 7→9 m (16.5°→~23°
camera elevation above the ROI centroid, computed as `atan2(eye.z − target.z, radius)` per
view against the GT poses — see the "elevation" note below on the number's method) and each
arm widened from 15 to 19 views at the same 6.86° step (96°→123.43° span), both exactly as
§12a.4's decision rule. No other preset, no gate, no threshold, and no application-code file
was touched.

### Benchmark command (clean cache — `data/bench/twosided_b` did not exist before this pass)

```
.venv/bin/python -m tools.benchmark --presets twosided_b --out data/bench --md-out /tmp/twosided_b_bench.md
```

Result (38/38 registered, one `/usr/bin/time -v`-measured subprocess, real 797.26 s / peak RSS
1747.05 MB — within the plan's "~11–13 min, < 2 GB" estimate):

| preset | registered | scale err % | photo vol err % | ortho vol err % | cloud RMS (m) | runtime (s) | peak RSS (MB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| twosided_b | 38/38 | 1.42 | 16.88 | 15.38 | 0.19 | 797.26 | 1747.05 |

As with S0, `tools/benchmark.py`'s own table has no status/coverage/void columns, so the
`measure()` result dict was read directly against the now-cached `data/bench/twosided_b/work`
(same cached model the benchmark run just built — no new SfM), reusing S0's read pattern. To
get the second-largest void (§12a.5 item 3, not returned by `landslide.volume._coverage_gate`
today), a read-only, in-process copy of that function's grid/label logic was run against the
same `(interior, up, polygon_ground)` inputs `prism_volume` already computes internally — this
is a duplicate read of existing values for reporting, not a new code path, and no source file
was changed to produce it.

### Measured gate results, coverage, and accuracy

Ground truth: 67.16 m³ cut inside the traced polygon (identical scene/polygon to every other
preset — `tools/synth.py`'s bowl/polygon constants are untouched).

| Mode | status | reasons | coverage_frac | largest_void_m² | 2nd-largest_void_m² | hit_frac | scale_rel_error | cut_measured_m3 | cut_volume err % vs 67.16 truth |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| photo | **ok** | *(none)* | 0.9859 | 0.94 | 0.125 | 1.00 (0 misses) | 1.17% | 78.47 | **+16.84%** |
| ortho | **ok** | *(none)* | 0.9857 | 0.94 | 0.125 | n/a (ortho) | 1.17% | 77.49 | **+15.38%** |

Registration: 38/38 (100%). Marker: `aruco_scale` `views_used` = 29 (all 19 of arm A, images
0–18, plus the first 10 of arm B, images 19–28; `side_spread_rel` 0.88%, `pnp_scale_spread`
0.68%, no warnings). Up-vector disagreement: 5.96° (photo mode; well under the 20° gate).
Median camera elevation above the ROI centroid (bowl-centre target, from the GT poses): ~23.1°,
uniform across all 38 views (`height_ramp=0`), clear of the ≥ 20° floor and a real rise from
S0's non-compliant ~16.5–19° depending on method — see the note below.

**§12a.5 criterion-by-criterion:**

| # | Criterion | Result |
| --- | --- | --- |
| 1 | Registration 38/38 | **pass** (38/38 both modes) |
| 2 | `status == "ok"` (coverage ≥ 0.85 and largest void ≤ 2.0 m²) | **pass** (0.986 / 0.94 m² both modes) |
| 3 | Second-largest void ≤ 2.0 m² | **pass** (0.125 m² both modes) |
| 4 | No other gate reason fires; up disagreement < 20°; `aruco_scale` clean | **pass** (`reasons = []` both modes; up 5.96°; no `aruco_scale` warnings) |
| 5 | Scale error ≤ 3%, `cut_measured_m3` within ±5% of 67.16 m³ | **scale: pass** (1.17–1.42%). **volume: FAIL** (+16.84% photo, +15.38% ortho — more than 3× the allowed band) |
| 6 | `views_used` ≥ 5, ≥ 2 per arm (images 0–18 / 19–37) | **pass** (19 arm A + 10 arm B = 29) |
| 7 | Median elevation ≥ 20° from GT poses | **pass** (~23.1°) |
| 8 | `pytest -q -k "not e2e"` green; no other preset's output changes | **pass** (182 passed, 3 skipped, 23 deselected — identical counts to S0's own regression run; only `tools/synth.py`'s new dict entry was added, no existing preset's config touched) |

**Item 5 is the only failure, and it is not a marginal miss.** `status` is a clean, unqualified
`ok` in both modes — G6's own coverage/void numbers are in fact *better* than S0's borderline
pass would have needed (98.6% coverage, sub-1-m² voids, vs S0's 86.7–87.8% / 9–10 m²) — but the
measured cut volume is 15–17% high, roughly 3× the criterion's own ±5% accuracy band, and nearly
3× S0's own headline result (S0: 0.90–1.39% error at `indicative`). No gate — G1–G6, up-vector,
or `aruco_scale` — flags this. **A guided capture built to this exact geometry would report a
confident, ungated `ok` on a volume that is wrong by roughly a sixth.**

### Root cause (read-only investigation; no code changed)

Both S0's `twosided` and S0b's `twosided_b` datum fits draw on the same "rim band"
([0.5, 2.5] m outside the traced boundary, ~r = 6.1–8.1 m from the bowl centre) and both log the
same *kind* of internal warning (rim-steepness filtering, a multi-metre rim-height span, outlier
clipping) — `res["warnings"]` from `prism_volume`, which `landslide/gates.py`'s
`evaluate_gates` never reads at all (G2 only checks `ctx.scale_info["warnings"]`; no gate reads
`res["warnings"]`). That gap is pre-existing on `main` and unrelated to this preset — S0's own
`twosided` run hit the same warnings and still landed within 1.35% of truth, so warning
presence alone doesn't explain the S0b regression.

The actual driver, isolated by mapping the cached dense cloud into the GT frame (same
`_umeyama_pose_aware` fit `tools/benchmark.py` already uses) and binning the residual
(`reconstructed_z − true_z`) by radius from the bowl centre, with `dense_cloud(ctx)` populated
first (an initial pass without it silently fell back to `ctx.sparse` via `ReconCtx.cloud`'s
documented sparse fallback and produced nonsense ~1 m RMS in every bin — corrected before this
conclusion was drawn):

| radius from bowl centre | n points | mean bias | RMS |
| --- | --- | --- | --- |
| 0–3 m (bowl floor) | 2253 | +0.003 m | 0.026 m |
| 3–5 m | 3737 | +0.006 m | 0.027 m |
| 5–5.6 m (near GT boundary) | 1320 | +0.007 m | 0.025 m |
| 5.6–6.2 m (bowl/flat transition) | 1527 | +0.010 m | 0.029 m |
| 6.2–7 m | 2169 | +0.009 m | 0.056 m |
| 7–9 m (rim-band outer half) | 6183 | **+0.134 m** | **0.468 m** |

Overall RMS 0.187 m matches the benchmark table's 0.19 m, confirming the corrected methodology.
Inside the bowl and right up to its true boundary, the dense cloud is essentially exact
(2.5–5.6 cm RMS, near-zero bias). **Only the outer half of the rim band (r ≈ 7–9 m), which is
exactly where §12a.4's wider arm span (123.43° vs 96°) pushes each arm's outer-end cameras
toward more grazing views of the background terrain, shows a real bias** (+0.13 m mean, 0.47 m
RMS — about 18× the interior RMS). That biased band feeds directly into `prism_volume`'s
robust rim-plane datum fit; the steepness/outlier filters trim the worst of it but not enough,
and the resulting datum offset is small per point (~0.11 m equivalent, matching the ~11.3 m³
excess over a ~98.5 m² footprint) but applies uniformly across the whole interior integration,
inflating `cut_volume_m3` by 15–17%.

This is the inverse of S0's failure mode, and arguably worse: S0's honest `indicative` result
came from a *known, admitted* 9–10 m² gap the pipeline correctly refused to integrate over with
confidence (`cut_upper_m3` bracketed it). S0b's wider/higher geometry closes that gap — but the
same geometry change (extending each arm outward) also extends *grazing coverage of the
background terrain in the datum's own rim band*, and the pipeline integrates that biased data
with full confidence because nothing gates rim-band reconstruction quality. **Closing G6 by
widening the arms is not automatically safe: it can convert an honest "can't see part of this"
into a confident wrong number**, which is a materially different (and more dangerous) failure
for a capture protocol whose whole purpose is trustworthy numbers.

**Note on the "elevation" figures.** §12a.2 reported S0's median elevation as 16.5° above the
ROI centroid; this pass's from-GT-poses re-derivation (`atan2(eye.z − target.z, radius)` against
the bowl-centre target) gives ~19.25° for S0's 7 m height and ~23.06° for S0b's 9 m height — the
two methods disagree on the absolute number (likely a different reference point for "ROI
centroid," e.g. the bowl's true post-cut floor vs. its pre-slide terrain height) but agree on
the direction and clear both sides of the 20° threshold in the same way S0/S0b's own pass/fail
already shows, so criterion 7 is unaffected either way.

### Deviations from the plan's S0b description

- Did not run the one permitted escalation (`n_views=42, yaw_span=137.14`, 3 photos per arm
  end). That escalation is gated specifically on "fails only because a void > 2 m² remains on
  the SE wall" (§12a.6). `twosided_b`'s voids are both well under 2 m² (0.94 / 0.125 m²) — G6
  passes outright — so this run does not match the escalation's precondition. The failure is
  accuracy (item 5), a mode the fixed decision rule does not name an escalation for.
- Per the decision rule's remaining branches ("a void remains... somewhere new → the elevation
  model is wrong, stop and re-analyse", "any other gate fires → stop and report, the change
  must not trade G6 for another gate") and the instruction to escalate *only* for the named
  SE-wall-void case and otherwise stop and re-analyse: this pass stops here. No second
  `tools/synth.py` preset, no threshold change, and no gate change was made or attempted.
- Root-cause work (the rim-band radial bias table) is read-only analysis against the already-
  cached `data/bench/twosided_b/work` and `data/bench/twosided/work` directories — no new SfM
  run, no file changes beyond the one preset dict entry.
- Did not wire `res["warnings"]` into `evaluate_gates` (which would have caught this run's rim-
  steepness/outlier warnings as a gate reason) — that is an application-code change to
  `landslide/gates.py`/`landslide/volume.py`, out of scope for a `tools/`-only S0b pass, and the
  instruction was explicit: do not weaken *or otherwise change* gates.
- Ran `pytest -q -k "not e2e"` after the change: 182 passed, 3 skipped, 23 deselected — identical
  to S0's own count, confirming no regression from adding the one preset dict entry. The slow
  e2e preset suite was not re-run for the other 7 presets, for the same reason S0 gave: this
  pass touches no other preset's configuration.

### Go/No-Go decision

**NO-GO.** `twosided_b` reaches `status = "ok"` in both photo and ortho modes — registration,
coverage, both void sizes, up-vector agreement, and marker-view criteria all pass cleanly, most
with wide margin — but §12a.5 item 5 (accuracy) fails: `cut_measured_m3` is 15.4–16.8% off the
67.16 m³ truth against a ±5% requirement, driven by a rim-band dense-reconstruction bias the
wider/higher geometry introduces and that no existing gate detects. Per the fixed decision rule,
this is not the "void > 2 m² on the SE wall" case the one permitted escalation is scoped to, so
no escalation was run. This pass stops after S0b, as instructed: **S1–S6 remain blocked.**
`twosided_b` is not promoted to the S5 calibration target, and §3 of `GUIDED_CAPTURE_PLAN.md` is
not edited (§12a.7's consequences apply only "after §12a.6 passes," which it did not).

This does not mean two-azimuth capture is unsound — S0 already showed the azimuth idea itself
gets volume error down to ~1% when honestly gated as `indicative`. It means the *specific*
§12a.4 fix (height 9 m, arms widened ±2 views each end) trades one known, well-understood
problem (a real void, honestly flagged) for a different, silent one (rim-band reconstruction
bias, not flagged at all) — worse for a protocol whose entire premise is that the user can trust
what the gates tell them on site. Before any further attempt at closing S0's void, the design
needs either (a) a way to close the void that doesn't push the outer rim band toward grazing
angles (e.g. a dedicated close-in pass at the void's location, height alone without widening the
arms, or a wider marker-side rim band the datum fit can afford to trim more aggressively), or
(b) a gate that can detect rim-band reconstruction quality independently of the interior
coverage gate G6 already checks — this pass's radial-bias table is one candidate signal, not a
validated gate. Both are protocol/gate-design work outside a `tools/`-only S0b pass and are not
attempted here.

### Correction (post-S0b): the rim-band radial-bias diagnosis above was wrong

`DATUM_STABILITY_PLAN.md` (implemented as G9, `DATUM_STABILITY_PROGRESS.md`) re-investigated the
root cause with the ground truth available and found the diagnosis above — "grazing coverage of
the background terrain in the datum's own rim band" biases the reconstructed rim cloud — **does
not hold**. Mapping the cached dense cloud against the true synthetic terrain (not the GT camera
poses alone) shows: every one of the 13 632 rim points with a height residual above 0.3 m sits on
the ArUco board and its mat (`MARKER`: x 17.5, y 26.0, a vertical 2.5 m board 7.5 m north of the
bowl centre, inside the 0.5–2.5 m rim band) — and the robust plane fit's own sigma-clipping
already rejects all 13 632 of them. Outside the board footprint, the rim cloud's median residual
against true terrain is +0.006 m (`twosided_b`) / +0.024 m (`twosided`) — not a grazing-angle
bias. Replacing every rim point with its true height changes `twosided_b`'s cut by 0.4%, not the
15–17% observed.

The actual cause is the datum **model**, not the rim data: `tools/synth.py`'s `build_terrain`
adds value noise up to 0.35 m amplitude at ~7 m wavelength, so the true pre-slide surface is not
planar at the scale of the rim ring. A plane fitted to the rim's *true* height (perfect points,
no reconstruction error at all) still sits +0.099 m above the true surface averaged over the
polygon — on ~100 m² that alone is +10 m³, ≈+15% of the 67.16 m³ truth. `twosided`'s "accurate"
1.3% result was a cancellation, not evidence against this: the same +0.13 m datum bias is present
there too, offset by the 16 m² of the polygon (16%) its wider TIN-bridging gap never integrated
before S0b closed that gap.

Practically: the wider/higher §12a.4 geometry did not introduce a new reconstruction defect. It
closed S0's void (correctly) and, in doing so, removed the accidental area-cancellation that had
been masking a pre-existing datum-model bias — present, at similar magnitude, in *both* S0 and
S0b. No rim-quality fix (robust segmentation, view-support filtering, a different rim band) closes
this: see `DATUM_STABILITY_PLAN.md` §2, §5 for the full evidence and the options considered. The
correction is detection, not mitigation — see `DATUM_STABILITY_PROGRESS.md` for G9, the
holdout-based datum-stability gate that now catches this class of error on both presets.

## S0c — `twosided_cal`, a calibration scene whose datum the rim can predict (`GUIDED_CAPTURE_PLAN.md` §12b)

**Base:** `main` @ `9bb999d`, plus the working tree's own uncommitted `GUIDED_CAPTURE_PLAN.md`
revision (pre-existing at session start, not authored by this pass — see its own diff for
§3/§3a/§3b/§7a/§12b). This pass changed `tools/synth.py`, `tools/benchmark.py` and
`tests/test_volume.py` only, exactly as §12b.3 specifies. No application code (`landslide/`,
`server/`) and no gate or threshold was touched. `GUIDED_CAPTURE_PLAN.md` was not edited by this
pass (see "Deviations" below).

### Change made

Per §12b.3:

- `tools/synth.py`: `build_terrain` gained an `octave_amps: tuple[float, float, float, float] =
  DEFAULT_TERRAIN_AMPS` parameter (new module constants `DEFAULT_TERRAIN_AMPS = (0.35, 0.15,
  0.08, 0.04)`, `TERRAIN_OCTAVE_CELLS = (6, 12, 24, 48)`). The loop still calls `value_noise` once
  per octave regardless of its amplitude (so a zeroed octave still consumes the RNG stream
  identically), matching §12b.3's requirement that the default reproduce every existing preset
  byte-for-byte.
- `PRESETS["twosided_cal"] = dict(PRESETS["twosided_b"], terrain_amps=(0.0, 0.0, 0.08, 0.04))` —
  `twosided_b`'s cameras/bowl/marker/lens verbatim, only the two long-wavelength terrain octaves
  (6, 12 cells) zeroed.
- `main()` passes `p.get("terrain_amps", DEFAULT_TERRAIN_AMPS)` to `build_terrain` and records
  `terrain_amps` in `ground_truth.json`, **only when the preset dict overrides it** (`"terrain_amps"
  in p`) — so `arc`, `twosided_b`, and every other existing preset keep a byte-identical
  `ground_truth.json` (§12b.2 requirement 4); only `twosided_cal`'s gains the new key.
- `tools/benchmark.py`: `run_preset`'s `cloud_rms_m` computation now calls `build_terrain(seed,
  tex_scale=..., octave_amps=PRESETS[preset].get("terrain_amps", DEFAULT_TERRAIN_AMPS))`, so the
  RMS-scoring surface always matches the preset's own terrain, not just the benchmark default.
- `tests/test_volume.py`: added `test_g9_calibration_terrain_stays_ok_seed7`, the positive-control
  complement of the existing R1 fixture test — same recipe (perfect points, no SfM) on
  `twosided_cal`'s terrain, seed 7, asserting `status == "ok"`, `reasons == []`,
  `datum_holdout_windows == 8`, `|cut error| ≤ 1.5%`, G9 ratio ≤ 0.03 (§12b.5 item 1).

### Benchmark command (clean cache — `data/bench/twosided_cal` did not exist before this pass)

```
.venv/bin/python -m tools.benchmark --presets twosided_cal --out data/bench --md-out /tmp/twosided_cal_bench.md
```

Result (38/38 registered, `/usr/bin/time -v`-measured, real 11m31s / peak RSS 1722 MB — inside
the plan's "~13 min, < 2 GB" estimate):

| preset | registered | scale err % | photo vol err % | ortho vol err % | cloud RMS (m) | runtime (s) | peak RSS (MB) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| twosided_cal | 38/38 | 0.02 | 1.48 | 1.71 | 0.20 | 689.57 | 1721.97 |

As with S0/S0b, the full `measure()` result dict was read directly against the now-cached
`data/bench/twosided_cal/work` (no new SfM), reusing the S0/S0b read pattern. Full per-mode gate
results, and the item 3/4/5/10/11 geometry/marker/residual/DEM checks, needed three further
read-only scratch scripts against the same cached model (no source file changed to produce any of
these readings) — none are committed.

### §12b.5 objective pass criteria, item by item

**Item 1 — pre-check (fast test, new, in `pytest -q -k "not e2e"`).** Seed 7 on `twosided_cal`'s
terrain (perfect points, no SfM): `status == "ok"`, `reasons == []`, `datum_holdout_windows == 8`,
cut error 0.49% (≤ 1.5%), G9 ratio 0.0212 (≤ 0.03). **Pass.**

The 40-seed sweep (re-run once, not a test): max\|cut error\| = 1.36%, max G9 ratio = 0.0254,
**40/40 seeds `ok`**. Matches §12b.4's own recorded evidence (1.36% / 0.025 / 40-of-40) exactly.

**Item 2 — no regression.** `arc` and `twosided_b` regenerated to a scratchpad dir (`tools.synth`,
seed 7) are byte-identical — `ground_truth.json` and every `IMG_*.png` — to the committed
`data/bench/arc` and `data/bench/twosided_b`. `pytest -q -k "not e2e"`: **186 passed, 3 skipped,
23 deselected** (185 pre-existing + the 1 new test above; S0b's own baseline was 182 before the
gate/thread-pool/deadlock commits between `d5653ba` and `9bb999d` added the rest). **Pass.**

**Items 3–9 (both modes, clean-cache `twosided_cal`):**

| # | Criterion | photo | ortho | Verdict |
| --- | --- | --- | --- | --- |
| 3 | Registration 38/38; G4 silent; median elevation ≥ 20° (GT poses); arm separation 80–100° (model) | 38/38, reasons=[], **22.9°**, **90.0°** | same ctx | **pass** (collinearity 0.602, far above the 0.10 G4 floor) |
| 4 | `aruco_scale` clean; `views_used` ≥ 5, ≥ 2/arm; `side_spread_rel` ≤ 5% | warnings=[], views_used=29 (19 A / 10 B), side_spread_rel **1.98%**, pnp_spread 1.00% | same ctx | **pass** |
| 5 | Dense residual vs true terrain: interior (r<5.6 m) RMS ≤ 0.05 m, \|mean\| ≤ 0.02 m; rim outside board footprint \|median\| ≤ 0.02 m | RMS **0.0256 m**, mean **0.0050 m**; rim median **0.0043 m** | same dense cloud | **pass** |
| 6 | Coverage ≥ 0.85; largest + 2nd-largest void ≤ 2.0 m²; area/polygon_area ≥ 0.97 | 0.9874 / 1.06 / 0.0625 / 0.978 | 0.9873 / 1.125 / 0.0625 / 0.978 | **pass** |
| 7 | `datum` in {rim_plane,rim_quad,rim_tps}; `datum_holdout_windows==8`; G9 ratio ≤ 0.04 | rim_plane, 8/8, **0.0212** | rim_plane, 8/8, **0.0261** | **pass** |
| 8 | `status=="ok"`, `reasons==[]`; up-disagree < 20°; `hit_frac`≥0.85, `max_miss_run`≤3 | ok, [], up 5.98°, hit_frac 1.0, miss 0 | ok, [] | **pass** (`measurement_path` not checked — S6a not shipped) |
| 9 | Scale error ≤ 3%; cut within ±5% of 67.16 m³; scale-removed cut within ±2% | scale err **0.0154%**, cut err **1.48%**, scale-removed err **1.43%** | scale err 0.0154%, cut err **1.71%**, scale-removed err **1.66%** | **pass** |

**Item 10 — path 2 on the scene path 1 cannot measure (cached `twosided_b`, no new SfM).** True
pre-slide surface (`build_terrain(7)`'s `h`, before the bowl cut, default amps — `twosided_b`'s own
terrain) written as an XYZ grid; `align_to_dem` → `measure(dem=…)`: `rms_m = 0.0228` (≤ 0.05),
`datum == "dem"`, `status == "indicative"` with the DEM-mode reason as the only reason (no G9),
cut err **3.27%** (≤ 5%), scale-removed err **1.02%** (≤ 2%). **Pass.**

**Item 11 — negative control.** `twosided_b` re-measured with the post-S0c `tools/synth.py`:
`indicative` in both modes with `G9_datum_unstable`-class reasons, and every number equal to the
`DATUM_STABILITY_PROGRESS.md` table to its own printed precision — photo: coverage 0.98588, void
0.9375 m², cut 78.468 m³ (+16.84%), holdout 0.0683 m, 8/8 windows; ortho: coverage 0.98571, void
0.9375 m², cut 77.487 m³ (+15.38%), holdout 0.0739 m, 8/8 windows. **Pass** (identical to the
committed table; `twosided_b`'s images/`ground_truth.json` were independently confirmed
byte-identical under item 2, so this also confirms `build_terrain`'s default path is unchanged).

### Deviations from the plan's S0c description

- **`GUIDED_CAPTURE_PLAN.md` was not edited.** §12b.7's GO branch names two direct consequences —
  "`twosided_cal` becomes the S4/S5 path-1 reference and replaces `twosided_b` in that role. §3
  drops 'provisional'." Those are plan-document wording changes, not `tools/`-file or
  application-code changes, and the instruction for this pass was explicit: implement only S0c,
  do not begin S1–S7, and record results/deviations/the decision in
  `GUIDED_CAPTURE_PROGRESS.md`. Since §3's protocol wording feeds directly into the S1/S3 capture
  UI text, editing it is left for whoever starts S4/S6a to do alongside that work, rather than
  edited here as a side effect of a benchmark pass. The GO decision itself (below) is the
  authoritative record for that edit to act on.
- Building the second-largest-void, per-arm marker split, elevation/azimuth, and dense-residual
  numbers (items 3–5 above) needed three read-only scratch scripts that duplicate small pieces of
  `landslide/volume.py::_coverage_gate`'s label logic and `tools/benchmark.py::run_preset`'s own
  reconstruct→scale→measure sequence, run against the already-cached `data/bench/twosided_cal` and
  `data/bench/twosided_b` work dirs (no new SfM beyond the one clean-cache run above) — the same
  read-only pattern S0/S0b/G9 used. None of these scripts are committed.
- One of those scripts initially mis-measured item 5 (interior RMS 0.81 m instead of 0.026 m) by
  calling `ctx.cloud(dense=True)` on a freshly-`reconstruct`ed `ReconCtx` without first populating
  `ctx.dense` from the on-disk cache — `ReconCtx.cloud()` silently falls back to the sparse cloud
  when `ctx.dense is None`, exactly the trap `DATUM_STABILITY_PROGRESS.md`'s own S0b investigation
  hit and self-corrected. Fixed by calling `landslide.densify.load_cached_dense(ctx)` first, as
  that investigation's correction did; the corrected numbers are what's reported in item 5 above.
- Did not attempt the θ-proxy sweep or any other geometry variant — §12b.3's terrain design was
  applied exactly as specified, with no tuning to the result (§12b.1: "the fix must be in the
  scene, never in a gate").
- Did not run the slow e2e preset suite (`tests/test_e2e_presets.py`) for the other 7 presets —
  this pass touches no other preset's configuration or images (item 2 confirms `arc` and
  `twosided_b` byte-identical), matching S0/S0b's own scoping of that check.

### Go/No-Go decision

**S0c: GO**, per the fixed decision rule (§12b.7, "All items pass"). Every item of §12b.5 (1
through 11, both photo and ortho mode where applicable) passed with margin — most with
substantial margin (G9 ratio ≤ 0.026 against a 0.04 scene-validity budget and a 0.05 gate; dense
residual RMS 2.6 cm against a 5 cm budget; cut error ≤ 1.7% against a 5% budget).

Consequences, per §12b.7's GO branch:

- **`twosided_cal` is the S4/S5 path-1 reference**, replacing `twosided_b` in that role.
  `twosided_b` keeps its role as the **negative datum control** (item 11: still `indicative`
  through G9 on identical cameras/marker/bowl, differing only in terrain).
- **S4 (preview SfM) is unblocked.** S5 (coverage/recapture calibration) was already gated on S4
  as well as S0c, so it is unblocked in the same sense — both still need S4's own preview-report
  code, which does not exist yet.
- **§3's "provisional" wording is not yet dropped** — see "Deviations" above. This GO record is
  what authorizes that edit; it was not made in this pass.

Per the instruction for this pass, **S1–S7 remain not started.** This pass touched only
`tools/synth.py`, `tools/benchmark.py`, and `tests/test_volume.py`, and changed no existing
preset's output, no gate, no threshold, and no application behavior.

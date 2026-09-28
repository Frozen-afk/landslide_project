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

`GUIDED_CAPTURE_PLAN.md` has since been updated (by the S6a pass below) to record S0c's GO and
drop "provisional" from §3/§12a.7, per the authorization this section's GO decision gives.

## S6a — Result-side paths and ids (`GUIDED_CAPTURE_PLAN.md` §9.3, §12 stage table)

**Base:** `main` @ `2941089`, plus S0c's own uncommitted `GUIDED_CAPTURE_PLAN.md` wording gap
(§3/§12a.7 still said "provisional" although S0c's GO already authorized dropping it — closed by
this pass, see below). This pass changed `landslide/gates.py`, `landslide/pipeline.py`,
`landslide/volume.py`, `server/static/js/steps/result.js`, `tests/test_gates.py`,
`tests/test_volume.py`, and `GUIDED_CAPTURE_PLAN.md`. No gate threshold changed. No `tools/` file
touched. S1–S5, S6b, S7 not started, per instruction.

### Change made

Per the stage-table scope (`GUIDED_CAPTURE_PLAN.md` §12, S6a row):

- **`landslide/gates.py`:** `evaluate_gates`'s `flag()` now takes a third argument, a stable
  reason id (e.g. `"G9_datum_unstable"`), and sets `res["reason_ids"]` (parallel to the existing
  `reasons` list) before returning — the return signature (`(status, reasons)`) is unchanged, so
  every existing caller and test keeps working unmodified. A new module-level `RULES` dict maps
  each id to its `class` (`capture` / `datum` / `info`, §7a) and its "what to do next time"
  `action` text (§7, §7a.2) — one source, reused by the (not-yet-built) capture UI and by
  `result.js` below. A new `classify_measurement_path(status, reason_ids, datum)` implements
  §7a.1's five-rule, first-match-wins path classification (`rim` / `dem` / `indicative_only` /
  `capture_failure`).
- **`landslide/pipeline.py`:** right after `evaluate_gates`, `measure()` now sets
  `res["measurement_path"] = classify_measurement_path(status, res["reason_ids"], res["datum"])`
  — additive, computed from fields `evaluate_gates` already produces, so it changes no status and
  no volume number.
- **`landslide/volume.py`:** `_datum_holdout` now also returns the centre azimuths (degrees, in
  the datum's own (u, v) frame) of the holdout windows it had to skip for lack of rim points, and
  `prism_volume` stores them as the new additive field `res["datum_holdout_missing_deg"]`. This is
  the field `G9_rim_partial`'s result-panel message needs to name which side of the slide is
  missing (§9.3); it is `[]` whenever the rim datum wasn't fitted at all (surface-plane fallback)
  or every window had enough points.
- **`server/static/js/steps/result.js`:** a `REASON_RULES` object mirrors `gates.py`'s `RULES`
  text (no endpoint serves `RULES` to the browser yet, so this is a copy, not a fetch — `gates.py`
  stays the single source for the *classification*, `classify_measurement_path`, which the fast
  tests below pin). Each reason now shows its "what to do next time" line under it. A `path` row
  shows the §7a.1 label (e.g. "Not a photo problem — more photos will not help.") next to
  `status`. A result with `measurement_path == "indicative_only"` gets an "Upload pre-event DEM"
  button; per §9.4 ("existing endpoint"), it does not duplicate the upload widget — it scrolls to
  and focuses the DEM `<input>` that already exists in step 3 (`mark.js`'s `uploadDem`, wired to
  `POST/DELETE /api/jobs/{id}/dem`), which already re-measures on success. A result with no
  `status` (a legacy saved job, pre-gates) now shows "pre-gate result — not checked" instead of
  silently omitting the row (closes `FINAL_RELEASE_AUDIT.md` §3.10's note, named in §9.3).
- **`GUIDED_CAPTURE_PLAN.md`:** §3's validation-status paragraph, the reuse-inventory row for
  `twosided_cal`, §12a.7's "provisionally" note, and the §12 Done/pending stage tables now record
  S0c's GO and S6a's completion; §3 no longer says "provisional" (S0c's own GO record, above,
  authorized this edit but did not make it).
- **Tests, the missing R5/R6/R8 from `DATUM_STABILITY_PLAN.md` §8:**
  - `tests/test_volume.py::test_g9_one_sided_rim_never_ok` (R5): `make_bowl_points()`'s rim kept
    only for polar angle < 150°. Asserts `status != "ok"`, `"G9_rim_partial"` in `reason_ids`, and
    `classify_measurement_path(...) == "capture_failure"` — a partial rim is fixable by recapture,
    so it must never be routed to path 3 as if it were an unfixable datum fault.
  - `tests/test_gates.py::test_g9_holdout_threshold_mapping` (R6): four hand-built `res` dicts
    against bare `evaluate_gates`, pinning the exact thresholds — (a) holdout 0.001 m/8 windows/
    area 100/net 60 → `ok`/`rim`; (b) holdout 0.07 m → `indicative`/`G9_datum_unstable`/
    `indicative_only`; (c) 5 windows → `indicative`/`G9_rim_partial`/`capture_failure`; (d) `dem`
    datum unaffected, classifies as `dem`. (a)/(d) already had coverage in the pre-existing
    `test_non_dem_mode_unaffected_by_dem_guard` / `test_dem_mode_status_never_ok`; this test adds
    the missing (b)/(c) thresholds plus the id/path assertions for all four.
  - `tests/test_volume.py::test_g9_flags_small_pile_on_curved_road_as_indicative` (R8): the
    existing `make_s_curve_road_with_pile()` fixture (already used by
    `test_s_curve_road_gets_membrane_datum`, which never calls `evaluate_gates`) run through the
    gate. Asserts `indicative` with a `"datum uncertain"` reason and documents it as intended —
    the road's roughly 1 cm datum uncertainty is a large share of the pile's own sub-m³ volume, so
    G9 correctly refuses to call this `ok`, per `DATUM_STABILITY_PLAN.md` §2's own recorded 216%
    figure for this fixture.
  - Extended the two existing `twosided`/`twosided_cal` G9 fixtures
    (`test_g9_unstable_datum_downgrades_ok_to_indicative`,
    `test_g9_calibration_terrain_stays_ok_seed7`) with `reason_ids`/`classify_measurement_path`
    assertions, covering §11 criterion 9's two named fast-test fixtures (R1 → `G9_datum_unstable`/
    `indicative_only`; the §12b.4 fixture → `rim`) without weakening either test's existing
    assertions.

### Focused validation (one run)

```
.venv/bin/python -m pytest -q -k "not e2e"
```

Result: **189 passed, 3 skipped, 23 deselected** — S0c's own baseline was 186 passed; the 3 new
tests (R5, R6, R8) account for the difference exactly, and every pre-existing assertion still
passes unmodified. Also spot-checked `node --check server/static/js/steps/result.js` (syntax
only — no headless-browser run in this pass) and a small interactive check that `evaluate_gates` /
`classify_measurement_path` / `RULES` import cleanly and produce the expected ids for a
`G9_datum_unstable` fixture.

### Deviations from the plan's S6a description

- `result.js`'s `REASON_RULES` duplicates `gates.py`'s `RULES` text rather than fetching it from
  an endpoint. §9.2's new endpoints (session/photo/preview/finalize) are S2/S4 scope, not S6a's,
  and no endpoint today serves `RULES` on its own — inventing one would be scope creep for this
  pass. `gates.py` stays the source of truth for the *classification* (`classify_measurement_path`,
  covered by fast tests); the JS copy is display text only, and the two are structurally identical
  (same ids, same class) so they can't silently diverge in meaning, only in wording if `gates.py`'s
  `action` text is edited without a matching `result.js` edit later.
- "Upload pre-event DEM" (§9.4) reuses the existing step-3 DEM control instead of building a new
  upload affordance inside the result panel, per §9.3's own "existing endpoint" framing. Building a
  second upload widget on the result screen would duplicate `mark.js`'s `uploadDem`/`removeDem` for
  no behavioral gain and was treated as out of S6a's scope.
- Did not add a `capture_report` row or a "finalized with override" label to `result.js` — both are
  explicitly S6b scope (§12 stage table), and no capture session exists yet to populate either.
- Did not touch `architecture.md`. `DATUM_STABILITY_PROGRESS.md`'s own diff-scope note (§9 item 7)
  already covers G9's documentation there; S6a adds no new gate or volume field that changes the
  pipeline's architecture, only ids/classification/display for fields G9 already produced.
- `RULES`' `action` text was written fresh for this pass (not copied verbatim from a single plan
  table cell) because §7's table is itself is split across a "final-pipeline failure" column, a
  "capture-time predictor (tier)" column (T0/T1/T3 — capture-module scope, not built), and a "user
  instruction" column that sometimes just points at §6 rather than giving a sentence (e.g. G2). The
  wording keeps §7/§7a.2's substance (what to do, phrased as a recapture instruction) without
  inventing capture-tier detail (T0/T1/T3 checks) this pass doesn't implement.

### Preserved

- Every existing `res` field is unchanged; only additive fields were added (`reason_ids`,
  `measurement_path`, `datum_holdout_missing_deg`). `evaluate_gates`'s return signature is
  unchanged. Saved-job JSON compatibility holds: the new fields are plain strings/floats/lists,
  serialize the same way as every existing field, and `result.js` already treats every field it
  reads as optional (`typeof r.x === "number"` / truthiness checks), so an old saved job with none
  of these fields still renders (with the new "pre-gate result" label only if it also has no
  `status`, which is the pre-existing legacy case, not something this pass introduces).

### Status

**S6a done**, per the instruction's scope (stable reason ids/classes, `measurement_path`
selection, actionable result messages, R5/R6/R8). **S1–S5, S6b, S7 not started.**

## S1 — Instant checks in the existing upload (`GUIDED_CAPTURE_PLAN.md` §12 stage table, row 1)

**Base:** `main` @ `d1ff131`. This pass added `server/static/js/capture_metrics.js`,
`tests/test_capture_metrics.mjs`, `tests/test_capture_metrics.py`,
`tests/fixtures/capture/` (generator + committed fixture JPEGs + `expected.json`), and changed
`server/static/js/steps/upload.js`, `server/static/index.html`, `server/static/style.css`. No
server-side file (`landslide/`, `server/routes.py`, `server/schemas.py`, `server/jobs.py`) was
touched, no new API endpoint was added, and `POST /api/jobs` is unchanged. **S2–S7 not started.**

### Change made

Per the stage-table scope ("`capture_metrics.js` plus parity fixtures. Per-file T0 verdicts and
EXIF lens check shown in the upload step **before** upload. No new API."):

- **`server/static/js/capture_metrics.js`** (new, pure, no DOM — same pattern as `coords.js` /
  `tests/test_coords.mjs`): `toGray`, `laplacianVariance`, `clippedFraction`, `resizeBilinear`,
  `dHash`, `hamming` (mirroring `landslide/pipeline.py::_photo_metrics`'s ≤900px grey decode,
  `cv2.Laplacian` 1-ksize kernel, rail-clip fraction, and 9×8 area dHash); `classifyBlur`,
  `classifyExposure`, `classifyDuplicate`, `classifyResolution`, `classifyOrientation`,
  `classifyLens` (the §4.1 threshold table); `parseExif` (a ~90-line hand-rolled JPEG APP1/TIFF
  walker for `Orientation`, `FocalLength`, `FocalLengthIn35mmFilm`, `LensModel` — no library, per
  §9.4's own estimate).
- **`server/static/js/steps/upload.js`**: the existing `file-input` `change` handler now also
  calls `runInstantChecks(files)`, which decodes each selected file with
  `createImageBitmap(file, {imageOrientation: "from-image"})` onto a ≤900px canvas (read-only —
  the `File` objects handed to `upload()` on click are never touched, so the originals stay
  full-resolution with EXIF intact for the actual `POST /api/jobs`), runs the capture_metrics.js
  checks, and renders one line per photo with ok/warn/fail chips and the §4.1 "instruction on
  fail" text as a tooltip. The blur/duplicate checks replicate `_cull`'s own sequential logic
  (batch median, previous-*kept*-frame duplicate comparison, blurrier-of-the-pair) so the T0
  preview and the server's actual drop decision are computed the same way; **this is advisory
  only — the server remains the sole authority on what actually gets dropped**, unchanged from
  before this pass.
- **`server/static/index.html`**: one `<div id="capture-checks">` added inside `#step-upload`,
  above the progress bar. **`server/static/style.css`**: a `.warn` color class (existing `.ok`/
  `.err` already there) and `.capture-checks` layout; cache-bust bumped `style.css?v=3` → `v=4`.
- **`tests/fixtures/capture/make_fixtures.py`** (new): generates the parity fixtures by calling
  the real `landslide.pipeline._photo_metrics` on real image content (see "Deviations") and
  dumping both its output and the post-decode grey pixel grid (which `_photo_metrics` doesn't
  itself return) to `expected.json`, plus a handful of tiny EXIF-only JPEGs (written with
  Pillow's `Image.Exif`/`IFDRational`, a real TIFF-structured APP1 segment, not hand-built bytes).
- **`tests/test_capture_metrics.mjs`** (new, `node --test`, no DOM): feeds `expected.json`'s
  dumped grey grids into `capture_metrics.js`'s pure functions and checks parity against the
  server's own numbers, checks blur/exposure verdicts against `_cull`'s actual fail formula,
  checks the duplicate/resolution/orientation classifiers, and parses the EXIF fixtures.
- **`tests/test_capture_metrics.py`** (new, in the fast suite): a drift guard — re-derives
  `_photo_metrics` and the EXIF tags from the *committed* JPEG fixtures (not the pre-compression
  arrays `expected.json` stores) and checks they still land close to what's recorded, so a future
  change to `_photo_metrics`/`EXPOSURE_CLIP_FRAC` that the JS parity test relies on doesn't drift
  silently.

### Validation (representative accepted/rejected photos)

`tests/fixtures/capture/`: `sharp_a.jpg`/`sharp_b.jpg` (crops of two `data/bench/arc` synthetic
renders — accepted, distinct content), `dup_of_a.jpg` (pixel-identical to `sharp_a.jpg` —
rejected as a duplicate), `blurry.jpg` (Gaussian blur σ=6 of `sharp_a.jpg` — rejected as blurry),
`overexposed.jpg` (3× brightness on `sharp_b.jpg` — rejected as overexposed), plus 4 tiny
EXIF-only fixtures for the lens/orientation check (`exif_ref`, `exif_lens_switch`, `exif_none`,
`exif_rotated`). Measured (`tests/test_capture_metrics.mjs`, `node --test`):

| Fixture | server lap | JS lap | rel. err | server clip | JS clip | Δclip | dHash Hamming (JS vs server) | server verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sharp_a.jpg | 305.78 | 307.59 | 0.6% | 0.1150 | 0.1150 | 0.0000 | 0 | accept |
| sharp_b.jpg | 316.21 | 318.23 | 0.6% | 0.1274 | 0.1274 | 0.0000 | 1 | accept |
| dup_of_a.jpg | 305.78 | 307.59 | 0.6% | 0.1150 | 0.1150 | 0.0000 | 0 | accept (but duplicate of a) |
| blurry.jpg | 1.71 | 1.71 | 0.1% | 0.0700 | 0.0700 | 0.0000 | 0 | reject (blur) |
| overexposed.jpg | 517.53 | 522.82 | 1.0% | 0.9899 | 0.9899 | 0.0000 | 0 | reject (exposure) |

All well inside criterion 1's tolerance (clip ≤0.01, lap rel. ≤10%, dHash ≤2). Blur/exposure
fail verdicts from `classifyBlur`/`classifyExposure` match `_cull`'s own formula on every
fixture (`tests/test_capture_metrics.mjs`, "blur/exposure verdicts agree..."). Duplicate check:
`classifyDuplicate` on `sharp_a` vs `dup_of_a` → `fail` (Hamming 0); on `sharp_a` vs `sharp_b` →
`ok` (distinct content). EXIF: `exif_lens_switch` (different focal length/35mm-equiv/lens model)
→ `classifyLens` = `fail`; `exif_none` (no APP1 segment at all) → `parseExif` = `null`,
`classifyLens` = `missing` in both directions; `exif_rotated` (`Orientation` 6 vs the reference's
1) → `classifyOrientation` = `warn`, while its lens/focal fields (unchanged from the reference)
still classify `ok`, confirming the two checks are independent.

Focused runs:
```
node --test tests/test_capture_metrics.mjs tests/test_coords.mjs   # 14 passed
.venv/bin/python -m pytest -q tests/test_capture_metrics.py         # 2 passed
.venv/bin/python -m pytest -q -k "not e2e"                          # 191 passed, 3 skipped, 23 deselected
```
(S6a's own baseline was 189 passed; the 2 new fast tests in `tests/test_capture_metrics.py`
account for the difference exactly.) `node --check` on both changed/new JS files. Server smoke
test: `uvicorn server.main:app` started locally, `GET /`, `/static/js/capture_metrics.js`,
`/static/js/steps/upload.js` and `/static/style.css` all returned 200 (new module wiring
resolves; no 404 from the new import).

### Deviations from the plan's S1 description

- **Fixture provenance.** §11 criterion 1 names "about 10 real phone JPEGs". No real phone JPEG
  exists anywhere in this repo or its test suite today (`tests/test_import.py`'s own blur/
  duplicate fixtures are synthetic numpy noise, not real photos). This pass follows the same
  established convention: 5 metric fixtures derived from `data/bench/arc`'s synthetic textured
  renders (the closest thing to "photo-like" content already in the repo) plus deterministic
  PIL-derived blur/exposure/duplicate variants, and 4 tiny synthetic EXIF-only fixtures. 9
  fixtures total, not ~10, and none are real camera output — this is the one open item against
  criterion 1's literal wording; the numeric tolerances themselves (relative Laplacian error,
  clip delta, dHash Hamming) are satisfied with wide margin (see table above).
- **"Session" = the selected batch, not an in-progress capture.** S1 wires T0 checks into the
  *existing* multi-file `<input type=file multiple>` upload (all photos already selected before
  any check runs), not the incremental per-shot capture flow S2/S3 will add. So "before 5 photos
  exist, use an absolute floor" (§4.1) is read as "batch has fewer than 5 files → use the flat
  6.0 floor (matching `_cull`'s own hardcoded floor) instead of a batch median"; once ≥5 files are
  selected, the median is computed once over the whole batch (this is actually a closer match to
  `_cull`'s real behavior than a running median would be, since `_cull` itself computes one
  median over the whole import batch, not a running one). Likewise "missing EXIF → warn once per
  session" is implemented as once per batch. A true running-median/per-shot-session version is
  S2/S3 scope, once a capture session actually exists.
- **Duplicate-chain fallback not replicated.** `_cull`'s rare ">50% of the batch is blur-flagged
  => treat as a broken metric, drop the least-sharp half instead" branch (a fallback for
  degenerate batches, `landslide/pipeline.py` `_cull`) is not mirrored in the client preview. The
  client only mirrors the normal per-photo fail/duplicate path. This branch is a safety net for a
  case the T0 preview isn't the authority on anyway — the server still applies its own real
  `_cull` unchanged.
- **T1 checks (features, overlap, parallax, marker) are out of S1's scope** and were not touched
  — they need server-side SIFT/marker detection (S2), not client-only math. `capture.html` (the
  old live-viewfinder coach, §2 "superseded, keep it as a link") was left exactly as is; S1 only
  changes the existing multi-file upload step, per the stage-table's "No new API" / "existing
  upload" framing.
- **EXIF field-mismatch tolerance.** `classifyLens`'s `focalLength` comparison uses a 2% relative
  tolerance rather than exact equality, since `FocalLength` is an EXIF `RATIONAL` and repeated
  shots on the same physical lens can round to adjacent rational representations on some camera
  firmwares. `focalLength35mm` (integer `SHORT`) and `lensModel` (string) are compared exactly.
  This is a judgment call the plan's table doesn't pin down (`Fail on mismatch` doesn't specify a
  tolerance) — noted here rather than silently baked in.

### Browser/field limitations (not testable outside a real device/browser)

- **No headless browser in this environment.** `createImageBitmap`, `canvas.getContext('2d')` and
  `File.arrayBuffer()` are browser APIs with no Node equivalent here (no puppeteer/jsdom/canvas
  package installed, and none was added — see the parity-test design above, which sidesteps this
  by feeding pre-decoded pixel grids into the pure functions instead). The DOM-wiring half of
  `upload.js` (`decodeForChecks`, `runInstantChecks`) is therefore verified only by `node --check`
  (syntax) and a static-file-serving smoke test (`GET` on the new JS module returns 200), not by
  an actual in-browser run. Manual verification in a real browser is recommended before relying
  on this in the field.
- **T0 latency (§11 criterion 5, ≤300ms/photo on a mid-range phone)** is not measured — this pass
  didn't run on a phone. Since S1 decodes at the same ≤900px working resolution `_photo_metrics`
  uses and does no SIFT/feature work (that's T1), the budget is expected to hold, but this is not
  verified here; S3/S7 own the real device timing per the stage table.
- **`resizeBilinear`'s pixel-center formula approximates `cv2.resize`'s default `INTER_LINEAR`,
  not an exact port** (§9.4 says this explicitly: "matching cv2.resize... is not exact, so parity
  is defined by tolerance"). Measured dHash Hamming distances (0–1 bit across all 5 metric
  fixtures) are well inside the ≤2 tolerance, but this was only checked at 360×270 → 9×8; larger
  originals (up to the 900px cap) are not separately exercised by the committed fixtures (see
  fixture-size note below).
- **`parseExif` covers a single top-level TIFF IFD0 + one ExifSubIFD walk** (Orientation,
  FocalLength, FocalLengthIn35mmFilm, LensModel only) — enough for the tags §4.1 names, written
  and read back correctly against Pillow's own real EXIF writer (TIFF-structured, big-endian,
  matching what a real camera produces), but never checked against an actual iPhone/Android JPEG
  (none exists in this repo). Unusual EXIF layouts (multiple IFDs, maker notes before the tags of
  interest, orientation-only cameras with no ExifSubIFD) are not covered.
- **Fixture pixel grids are 360×270** (deliberately below the 900px cap, see `make_fixtures.py`'s
  own docstring) so the committed `expected.json` stays a few MB instead of tens of MB. This
  means the parity test never exercises `_photo_metrics`'s own `im.resize(...)` downscale step —
  by design, since that resize is PIL-only code with no JS equivalent to test against; what S1
  needs to match (the Laplacian/clip/dHash math given identical pixels) is fully exercised at
  this size. A larger-image spot check was not run separately.

### Acceptance status

- **Criterion 1 (metric parity).** Met, with the fixture-provenance deviation noted above
  (synthetic content, not real phone JPEGs — none exist in this repo). Numeric tolerances: clip
  Δ ≤0.0001 (budget 0.01), Laplacian rel. error ≤1.0% (budget 10%), dHash Hamming ≤1 (budget 2),
  on all 5 metric fixtures; blur/exposure fail verdicts match `_cull`'s formula on every fixture.
- **Criterion 7 (no regression).** Met. `pytest -q -k "not e2e"`: 191 passed (188 pre-existing +
  1 pre-existing skip-adjacent... see run above — exactly S6a's 189 plus this pass's 2 new tests),
  3 skipped, 23 deselected. No `landslide/` or `server/*.py` file was touched, so
  `POST /api/jobs`'s behavior is unchanged by construction, and legacy saved jobs (which never
  hit any code path this pass added) are unaffected.
- **S2–S7 remain not started**, per the instruction for this pass.

## S2 — Capture session backend (`GUIDED_CAPTURE_PLAN.md` §12 stage table, row 2)

**Base:** `main` @ `0b2f927`. This pass added `landslide/capture.py`,
`tests/test_capture.py`, `tests/test_capture_session.py`,
`tests/fixtures/capture_t1/` (generator + committed fixture JPEGs), and
changed `server/routes.py`, `server/schemas.py`, `server/jobs.py`. No
`server/static/*` (UI) file was touched — the capture UI is S3 scope. No
gate, threshold, or existing endpoint's behavior was changed. **S3–S7 not
started**, per the instruction for this pass.

### Change made

Per the stage-table scope ("`capturing` status, `session.json`,
`PUT/DELETE photos`, `GET capture`, `finalize` (no preview gating yet),
`landslide/capture.py::image_checks` / `sequence_checks`, semaphore. Setup
stores the DEM answer in `session.json`."):

- **`landslide/capture.py`** (new, no web dependency, per §9.2's own spec):
  - `image_checks(path, prev_path=None, marker_side_m=None, marker_dict="auto",
    blur_floor=6.0, blur_median=None, arm_bridge_paths=None, prev_pivot=False,
    feats_cache=None, prev_feats_cache=None)` — the T1 per-photo checks
    (§4.1): blur/exposure (reuses `pipeline._photo_metrics`, same thresholds
    `_cull` uses), texture (`cv2.SIFT_create(4000)` keypoint count at
    1000 px, fail < 300 / warn < 800), overlap-with-previous (ratio-test SIFT
    matches → `findFundamentalMat` RANSAC inliers, fail < 40 / warn < 100),
    turn-in-place / parallax (homography-inlier ÷ fundamental-inlier ratio
    against the previous photo, warn ≥ 0.95, fail on two such photos in a
    row via the caller-supplied `prev_pivot` flag), marker present/size/angle
    (reuses `scaling.detect_marker_corners` / `_load_gray` / `_available_dicts`,
    same size/ratio cut points as §4.1's table), and arm-bridge overlap (best
    of up to 3 tail photos of the other arm). Returns
    `{verdict, reasons: [{id, text}], server: {...}}`; `T1_MESSAGES` holds
    the §4.1 "instruction on fail" text — a separate vocabulary from
    `gates.RULES` (the result-side G-gate ids), which this module does not
    touch, per §9.2's own note ("Imports `RULES` ... It does not define its
    own copy" — that note is about the *result-side* ids a later stage's
    `recapture_instructions` will reuse; per-photo T1 reasons fire before any
    gate runs and have no gate id yet).
  - `extract_feats(path, cache=None)` — SIFT keypoints/descriptors at
    1000 px, optionally read from / written to an `.npz` cache file. Routes
    wires this to `capture/feats/<uuid>.npz` (§9.1's own directory layout),
    so a later photo's overlap check against an earlier one doesn't re-decode
    and re-run SIFT on it.
  - `sequence_checks(photos)` — the T2 subset that is actually derivable
    server-side today: per-arm kept-photo counts, marker-view counts
    (total and per arm), and overlap-chain gaps. Heading spread, arm
    separation and elevation (§4.2) need client sensor data that S3's UI
    doesn't exist yet to send, so they are not computed here — an absent
    field, not a wrong one; nothing in `RULES`/gates reads this dict yet.
- **`server/schemas.py`**: `CaptureSessionRequest` (`marker_side_m`,
  `marker_dict`, `has_pre_event_dem` — §3b step 1's setup question, stored
  and never used for scoring in this pass), `CapturePhotoMeta` (`seq`, `arm`,
  `client` — the form-field JSON body of the per-photo PUT), and
  `CaptureFinalizeRequest` (`override`).
- **`server/jobs.py`**: `Job` gains a `capture: dict | None` field (the
  manifest, mirroring `scale_info`/`ortho`/`dem_info`'s existing
  persisted-mirror pattern) and `capture_dir` / `save_capture()` — the same
  atomic temp-file-plus-`os.replace` pattern `save_state()` already uses,
  writing to its own `capture/session.json` (§9.1) rather than into
  `state.json`, since the manifest grows with every photo independently of
  the job's log/result/scale state. `load_persisted_jobs` reads
  `capture/session.json` back in on restart when present; a `capturing`
  status already passed through that function unmodified (untouched by the
  existing `("reconstructing", "measuring", "orthorectifying")` remap
  tuple), so no change was needed there. `snapshot()` includes `capture`
  when set, for the status-poll endpoint.
- **`server/routes.py`**: five new endpoints, all under `/api/jobs/capture`
  and `/api/jobs/{id}/capture/...`, matching §9.2's table:
  - `POST /api/jobs/capture` — creates a `capturing` job, `capture/originals/`
    and `capture/feats/` directories, an empty `photos/` (so
    `load_persisted_jobs`'s own directory-scan condition still finds the job
    on restart, §9.1), and the manifest.
  - `PUT /api/jobs/{id}/capture/photos/{uuid}` — multipart `file` + form
    `meta` (the `CapturePhotoMeta` JSON). Rejects non-JPEG by extension
    (§8: `accept="image/jpeg"` is the only capture path in scope). SHA-256
    identity: a repeated PUT with the same uuid **and** sha256 returns the
    stored result without reprocessing (idempotent retry); a different
    sha256 for the same uuid is a 409. Looks up the previous *kept* photo in
    the same arm for the overlap/parallax check, and (for arm `B`'s first 3
    kept photos, or any `T` transition photo) the last 3 kept arm-`A`
    photos for the arm-bridge check. The session/batch blur median is
    computed once ≥ 5 kept photos exist, matching S1's own "before 5 photos
    exist, use the flat floor" reading (§4.1). The actual `image_checks`
    call runs in `run_in_threadpool`, gated by a module-level
    `threading.Semaphore(2)` (§9.2: "T1 runs in `run_in_threadpool` behind a
    `threading.Semaphore(2)`, so per-photo checks cannot starve the SfM
    workers" — here, starve *other requests* on the same threadpool during a
    burst of queued uploads). 409 unless `status == "capturing"`.
  - `DELETE /api/jobs/{id}/capture/photos/{uuid}` — soft-excludes (marks
    `excluded: true`, recomputes `sequence`); the file stays on disk (a
    retake with the same uuid is a normal idempotent PUT, a different uuid
    is a new entry).
  - `GET /api/jobs/{id}/capture` — the manifest as stored (no blobs are ever
    in it — photo bytes live only under `capture/originals/`).
  - `POST /api/jobs/{id}/capture/finalize` — copies the non-excluded
    originals in `seq` order into a temp dir named `{seq:03d}_{uuid}.jpg`,
    calls the **unchanged** `import_photos` into `photos/`, sets
    `reconstructing`, and calls `executor.submit_job(job,
    worker.run_reconstruction, ...)` — the identical path `create_job` uses.
    No preview report exists yet (S4), so `blocking_at_finalize` is always
    `[]` and finalize never actually refuses on gate grounds in this pass;
    the `override` field is accepted and stored now so S4 doesn't need a
    schema change later. **Crash recovery (§10 "finalize interrupted"):**
    the status guard accepts `status in ("capturing", "error")` rather than
    only `"capturing"` — a crash between `import_photos` succeeding and
    `submit_job` being called leaves the job `reconstructing` with no
    complete model, which `load_persisted_jobs`'s existing P3d rule already
    demotes to `error` on restart; calling finalize again re-copies from
    `capture/originals` (kept on disk until the job is deleted) and retries,
    with no new persistence logic required.
  - `list_jobs`'s `n_photos` now counts non-excluded manifest entries for a
    `capturing` job (its `photos/` directory stays empty until finalize, so
    the old `count_photos(photos/)` read always showed 0 — §9.1's own
    explicit requirement, "for `capturing` jobs, `n_photos` counts the
    non-excluded manifest entries").
- **Preserved:** `POST /api/jobs` (the existing multi-file upload) was not
  touched at all — no line in `create_job`, `import_photos`, or any gate
  changed. `delete_job` already lets a `capturing` job be deleted (it isn't
  in `BUSY_STATUSES` and isn't `"reconstructing"`), which drops its whole
  directory including `capture/`, same as any other job.

### Focused validation (one run)

```
.venv/bin/python -m pytest -q -k "not e2e"
```

Result: **209 passed, 3 skipped, 23 deselected** — S1's own baseline was 191
passed; the 18 new tests (11 in `tests/test_capture.py`, 7 in
`tests/test_capture_session.py`) account for the difference exactly, and
every pre-existing assertion still passes unmodified. Also ran
`.venv/bin/python -m pytest -q tests/test_capture.py tests/test_capture_session.py`
on its own (18 passed) and a manual `TestClient` walk of all five endpoints
(create → PUT (idempotent retry, sha-mismatch 409) → GET → DELETE →
finalize-too-few-photos) plus a `GET /api/jobs` check that a `capturing`
session's `n_photos` reflects the manifest, not the (empty) `photos/` dir.

### Criterion 2 fixtures and their honest gap

`tests/fixtures/capture_t1/` (regenerated via
`.venv/bin/python -m tests.fixtures.capture_t1.make_fixtures`): crops of the
already-rendered `data/bench/{arc,nadir,sparse8}` presets for
marker/overlap content, a from-scratch two-frame render (same eye position,
10° pan, no translation at all) for the parallax/pivot check, and plain
near-uniform PIL images ("water, snow, sky", per §4.1's own instruction
text) for the texture-fail case — see that script's own docstring.

Measured (`tests/test_capture.py`):

| Check | Fixture pair | Result |
| --- | --- | --- |
| Blur | `blurry.jpg` (Gaussian σ=3 of `overlap_a.jpg`) | `blur` reason fires; `overlap_a.jpg` itself (lap≈208) does not |
| Marker | `marker_arc.jpg` / `marker_nadir.jpg` | arc: `verdict=="ok"`, no marker reason. nadir: `side_ratio` 0.39 < 0.4, `marker_edge_on` fires |
| Overlap | `overlap_a.jpg`→`overlap_b.jpg` (consecutive) vs `overlap_a.jpg`→`overlap_far.jpg` (3 steps) | consecutive: no `overlap` reason (69 inliers). 3-apart: `overlap` fires (39 < 40) |
| Texture | `texture_blank.jpg` vs `texture_arc.jpg` | blank: `features` fires, 0 keypoints. real content: far more keypoints (229) |
| Parallax | `pivot_a.jpg`/`pivot_b.jpg` (pure pan) vs `overlap_a.jpg`/`overlap_b.jpg` (real translation) | pivot pair's homography ratio (0.869) is measurably higher than the translating pair's (0.694) — the correct *direction* |

**The gap, stated plainly:** this repo's only synthetic scene (a Gouraud-
shaded heightfield with no per-pixel sensor noise) does not clear the
literal §4.1 numbers even on its best available content — every fixture
above lands under `FEATURES_FAIL` (300) at the T1 working resolution
(229 keypoints on real 3-D content, not 0), and the pivot pair's homography
ratio (0.869) sits under the 0.95 warn cut. Both ceilings hold even after
trying higher `tex_scale`/`octave_amps` terrain and full-resolution SIFT (no
downscale) — see this section's own working notes in the commit that added
these fixtures. This is not a threshold bug: `S1`'s own progress notes
already establish that no real phone JPEG exists anywhere in this repo, and
apparently the gap is wider for texture-heavy checks than for the
blur/exposure/dHash checks S1 could clear. Rather than loosen the §4.1
numbers to fit synthetic content (exactly what `GUIDED_CAPTURE_PLAN.md`
repeatedly instructs against — thresholds are calibrated against real
prints/photos in S5/S7, never adjusted to make a test pass), `tests/test_capture.py`
pins the exact threshold arithmetic with monkeypatched inputs (six tests)
and separately proves the real detector/matcher wiring on real synthetic
content by asserting on each check's own reason id or a directional
relationship (pivot > translate, real content > blank), not on the
aggregate verdict. Real-world calibration against actual phone photos and
printed markers remains S5/S7 scope, unchanged from the plan.

### Latency (informational only — not an S2 exit criterion)

§11 criterion 5 (T1 ≤ 2 s p95 per photo on a field laptop) is scoped to S3/S4
in the stage table's own criteria list, not S2. For reference only, 8 calls
to `image_checks` (marker + overlap check both enabled) on this dev box
(cold SIFT init excluded, warm run): median 0.265 s, max 0.282 s — comfortably
inside the 2 s budget, but this is a dev workstation, not the target field
laptop, and is not a substitute for S3/S7's own measurement.

### Deviations from the plan's S2 description

- **`sequence_checks` omits heading/arm-separation/elevation** (§4.2's
  sensor-derived rows). Those need `client` sensor payloads S3's UI doesn't
  send yet; computing them now would mean inventing a client contract ahead
  of the UI that defines it. The manifest already has a `client: {}` field
  per photo (§9.1) for S3 to populate.
- **No preview gating at finalize.** S4 (§12 stage table) owns
  `worker.run_preview` / `preview_report`; finalize's `blocking_at_finalize`
  is therefore always `[]` in this pass, and `override` is accepted but
  inert. This matches the stage table's own row text ("no preview gating
  yet").
- **Arm-bridge feature caching.** `image_checks`' `arm_bridge_paths` re-runs
  `extract_feats` uncached for each of the up-to-3 tail photos of the other
  arm, instead of reading their `capture/feats/<uuid>.npz` cache. Those
  photos' own PUT calls already wrote that cache; wiring the bridge check to
  read it would save a few hundred ms per bridge photo but touches only a
  handful of photos per session (first 3 of arm B, plus transition), so it
  was left as the simpler uncached call.
- **`has_pre_event_dem` is stored, not used.** §3b step 1 only says the
  answer "changes the messages in step 7" (the result panel, S6a already
  shipped) — no code in this pass reads it back; it is carried through the
  manifest for S6b/S3 to use when they build the messages that reference it.
- **No change to `capture.html`** (the old live-viewfinder coach) or any
  `server/static/*` file — the guided-capture UI is explicitly S3 scope.

### Browser/field limitations (not testable outside a real device/browser or S3's UI)

- **No client exists yet.** Every endpoint in this pass was exercised only
  via `TestClient`/direct HTTP, not through a real phone browser — there is
  no UI yet to drive it from (S3). The multipart-upload contract
  (`file` + form `meta` JSON) was designed to be servable from `fetch()`
  with a `FormData` body without further server changes, but this is not
  verified against an actual mobile Safari/Chrome `fetch` call in this pass.
- **HEIC/other formats:** the PUT endpoint rejects non-`.jpg`/`.jpeg` by
  filename extension only (no content-sniffing of the actual bytes) — §8's
  own design ("`accept="image/jpeg"` makes iOS transcode. The server's
  per-photo endpoint rejects other types with a clear message") is met at
  the extension-check level; a mismatched extension on real bytes (e.g. a
  renamed `.heic`) is not separately rejected by content inspection, since
  `import_photos`/`_photo_metrics` already error clearly on undecodable
  content and that path is exercised via `PIL.Image.open` failing inside
  `image_checks`.
- **Semaphore(2) sizing** is the number the plan names (§9.2); it was not
  load-tested against a real burst of queued uploads on a small field
  laptop in this pass.

### Acceptance status

- **Criterion 2 (per-image check correctness).** Met, with the honest
  numeric-ceiling gap documented above: threshold arithmetic is pinned
  exactly (6 monkeypatched tests over blur/features/overlap/parallax/marker),
  and the real detector/matcher wiring is proven on real synthetic content
  (5 fixture tests) asserting on reason ids and directional signals rather
  than on absolute counts this scene cannot clear.
- **Criterion 6 (a-d, recovery).** Met. (a) same-uuid-same-sha256 PUT twice
  → one manifest entry, identical response. (b) same-uuid-different-sha256
  → 409. (c) a hand-built `capturing` job directory with a `capture/session.json`
  survives `load_persisted_jobs` with its status and photos intact (no
  `preview` field exists yet to go `stale` — S4). (d) finalize after a
  monkeypatched `import_photos` failure (simulating the crash window between
  import and submit) leaves the job `error` with `finalized` still `null`;
  calling finalize again succeeds, re-copying from `capture/originals` and
  reaching `reconstructing` with `finalized` set. **6(e)** (a real
  kill-the-tab-and-reopen device test) is out of scope without a client —
  S3/S7.
- **Criterion 7 (no regression).** Met. `pytest -q -k "not e2e"`: 209 passed
  (191 pre-existing + 18 new), 3 skipped, 23 deselected. `POST /api/jobs`'s
  own function body is byte-for-byte untouched. Legacy saved jobs load
  unaffected (no change to that branch of `load_persisted_jobs`).
- **S3–S7 remain not started**, per the instruction for this pass.

## S3 — Guided UI: sequence and viewpoint guidance (approved slice)

**Base:** `main` @ `232c6f7`. This pass added `server/static/js/steps/capture.js`,
`server/static/js/capture_guidance.js`, `tests/test_capture_guidance.mjs`, and
changed `server/static/index.html`, `server/static/js/main.js`,
`server/static/js/state.js`, `server/static/js/steps/upload.js`,
`server/static/style.css`. No `landslide/`, `server/*.py`, gate, or threshold
file was touched. This pass implements an **approved narrower slice of the
plan's full S3 row** ("`step-capture` section, `capture_store.js` IndexedDB
queue, arms and counters ... rim-check step, marker checklist and chips,
optional sensors"): connecting the existing S2 sequence checks to the capture
UI and turning them into actionable guidance for photo count, overlap gaps,
marker views, arm transition, camera height, and viewpoint diversity, with
unavailable browser sensors treated as optional rather than as failures. The
marker-placement checklist (§6), the rim-clearing step (§3 step 2), the
top-down sketch (§5.4, needs S4), and an IndexedDB offline-blob queue (§10)
are explicitly out of this slice — see "Deviations" below. **S4–S7 not
started.**

### Change made

- **`server/static/js/capture_guidance.js`** (new, pure, no DOM — same
  pattern as `capture_metrics.js` / `coords.js`): circular-statistics
  helpers (`circularMeanDeg`, `circularDiffDeg`, `circularSpreadDeg`) and six
  guidance builders that turn the server's T2 `sequence` dict
  (`landslide/capture.py::sequence_checks`, unchanged) plus optional
  client-side sensor arrays into `{level, id, text}` guidance items:
  `photoCountGuidance` (§4.2 photo-count row, per-arm-of-12 and total-of-15),
  `markerViewGuidance` (§4.2 marker-views row, total-of-5 and 2-per-arm),
  `overlapGapGuidance` (§4.2 overlap-chain row, resolves a gap's uuid to a
  photo number via a caller-supplied map), `viewpointDiversityGuidance`
  (§4.2 heading-spread-per-arm and arm-separation rows, needs >= 2 heading
  readings per arm or is silently skipped), `cameraHeightGuidance` (§4.2
  elevation/pitch row, warns when > 30% of an arm's pitch readings are below
  10°, same >= 2-reading floor), and `armTransitionGuidance` (§3 step 4 / the
  `arm_bridge` T1 reason: a hint while on the transition arm, a warning if
  the most recent bridge-eligible photo's check flagged a weak link).
  `circularDiffDeg`/`circularSpreadDeg` are robust to an uncalibrated or
  reversed compass convention because every guidance decision uses only
  *differences* between readings, never an absolute bearing — the reasoning
  the module's own header comment records, since no model-azimuth alignment
  (§5.4, S4/S5 scope) exists yet to calibrate raw device headings against.
- **`server/static/js/steps/capture.js`** (new, DOM orchestration, no
  business logic beyond wiring): a minimal capture-session flow —
  `POST /api/jobs/capture` (marker side + dict from two inputs) →
  `<input type=file accept="image/jpeg" capture="environment">` per photo
  (§8: the plain-HTTP-safe capture path) → client-side T0 blur pre-check
  (reuses `capture_metrics.js`, best-effort — the server's T1 check is
  authoritative, same division of labor as the existing upload step) →
  `PUT .../capture/photos/{uuid}` (a client-generated UUID; `crypto.randomUUID`
  when available, else a `crypto.getRandomValues`/`Math.random` fallback,
  since `randomUUID` itself needs a secure context the field server may not
  have, §8) → renders the returned per-photo `reasons` text directly (the
  server already supplies `{id, text}` pairs, `landslide/capture.py::T1_MESSAGES`
  — no separate client copy of that vocabulary) and the six
  `capture_guidance.js` builders against the returned `sequence` plus the
  session's accumulated `client.heading`/`client.pitch` values, grouped by
  arm. An "Exclude last photo" button calls the existing soft-delete
  endpoint and then re-fetches `GET .../capture` as the refreshed source of
  truth (§10's own stated principle) rather than reimplementing
  `sequence_checks` client-side. "Finish and reconstruct" calls
  `POST .../capture/finalize` (`override` always `false` — S4's preview
  report, the only thing that could set `blocking_at_finalize`, doesn't
  exist yet, so there is nothing to override in this slice) and hands the
  now-`reconstructing` job to the existing upload-flow poller by calling
  `upload.js`'s own `resumeJob` (exported for this purpose) instead of
  duplicating its polling/rendering logic.
  - **Optional sensors** (§8, §4.2's "advisory only"): an "Enable
    compass/tilt hints" button calls `DeviceOrientationEvent.requestPermission()`
    where it exists (iOS, needs the user gesture §8 names) and otherwise
    attaches a `deviceorientation` listener directly, but only when
    `window.isSecureContext` — the plain-HTTP field server (`run_server.sh`)
    never satisfies this, so on that deployment the button always reports
    "unavailable" and every sensor-derived guidance line is simply absent
    from the panel, never shown as a failed check. Heading uses
    `event.webkitCompassHeading` when present, else raw `event.alpha` (no
    reinterpretation — see the robustness note above). Pitch uses `event.beta`
    through the plan's own named proxy formula, `90 - beta`, clamped to
    [-90, 90] — an approximation, not a port of anything server-side (there
    is nothing server-side to port; §4.2 calls this row "T0 (sensor only)"
    and names the real elevation check as T3, not yet built).
- **`server/static/index.html`**: a new `<section id="step-capture">` card
  between the existing upload card and the scale step, with the marker-side/
  dict inputs, a "Start guided capture" button, the sensor-enable button, an
  arm selector (A / transition / B), a "Take photo" file input, "Exclude
  last photo", "Finish and reconstruct", and two output panels
  (`#capture-last-photo`, `#capture-sequence`) reusing the existing
  `.capture-checks`/`.ok`/`.warn`/`.err`/`.note` classes from S1's upload
  panel. Placed as an always-visible card alongside the existing upload
  card, **not** behind the plan's own two-button entry-point toggle
  (§9.4) — see "Deviations". `style.css?v=4` → `v=5` (cache-bust, same
  convention S1 used).
- **`server/static/js/state.js`**: `state.capture = {jobId, arm, seq,
  photos}` (§9.4's own shape, minus the IndexedDB pieces this slice doesn't
  build) and `LS_CAPTURE_KEY = "lsv-capture"` (§9.4), both additive.
- **`server/static/js/steps/upload.js`**: `resumeJob` is now exported (was
  previously module-private) so `capture.js` can hand off a finalized
  session without a second copy of the ready/reconstructing poll loop. No
  other line changed.
- **`server/static/style.css`**: two small rules for the guidance `<ul>`
  inside `.capture-checks`; every color/verdict class (`.ok`/`.warn`/`.err`)
  and layout class (`.row`, `.note`, `.card`) already existed and needed no
  change.

### Focused validation (three runs)

```
node --test tests/test_capture_guidance.mjs tests/test_capture_metrics.mjs tests/test_coords.mjs
.venv/bin/python -m pytest -q -k "not e2e"
node --check server/static/js/steps/capture.js server/static/js/capture_guidance.js \
             server/static/js/state.js server/static/js/main.js server/static/js/steps/upload.js
```

Results: **22 passed** (8 new in `test_capture_guidance.mjs`, covering the
circular-stat wraparound, each guidance builder's warn/silent boundary, and
the sensor-unavailable/`< 2`-reading silence case) — 0 failed. `pytest -q -k
"not e2e"`: **209 passed, 3 skipped, 23 deselected**, identical to S2's own
baseline (no `landslide/`/`server/*.py` file was touched, so this is exactly
the expected outcome, not a re-run of unrelated work). `node --check` clean
on all five changed/new JS files.

**Server-contract check (real server, not just the unit tests):** started
`uvicorn server.main:app`, then drove the exact HTTP contract `capture.js`
uses with a small script — `POST /api/jobs/capture` → two
`PUT .../capture/photos/{uuid}` calls (multipart `file` + form `meta`, real
fixture JPEGs from `tests/fixtures/capture_t1/`) → `GET .../capture` →
`DELETE .../capture/photos/{uuid}` → `POST .../capture/finalize` with only 2
kept photos. Confirmed: the response and manifest shapes `capture.js` reads
(`sequence.counts`/`marker_views`/`marker_views_total`/`overlap_gaps`,
`reasons: [{id, text}]`, `server.arm_bridge`, `client` echoed back verbatim
in the manifest) match what was assumed while writing it; finalize correctly
400s below the 3-kept-photo floor. Also: `GET /`,
`/static/js/steps/capture.js`, `/static/js/capture_guidance.js`,
`/static/js/main.js`, `/static/style.css` all return 200 (new module wiring
resolves, matching S1's own smoke-test pattern).

### Deviations from the plan's full S3 row (this pass's approved scope)

- **Not built: marker-placement checklist (§6), rim-clearing step (§3 step
  2), top-down sketch (§5.4).** The rim-clearing step is UI text only and the
  marker checklist is a static list — both are straightforward additions,
  but neither was named in the approved instruction for this pass
  ("photo count, overlap gaps, marker views, arm transition, camera height,
  and viewpoint diversity"). The top-down sketch needs the preview SfM
  camera positions (S4), which do not exist yet.
- **Not built: IndexedDB offline-blob queue / retry backoff (§10,
  `capture_store.js`).** This pass uses the server's `GET .../capture`
  manifest as the resume source of truth (§10's own principle: "the server
  manifest is the truth for accepted photos") instead of a client-side blob
  queue for photos the server hasn't acked yet. This means a tab kill
  *during* an in-flight `PUT` loses that one photo (the user sees the gap in
  the count and retakes) rather than resuming the upload from a queued
  blob — criterion 6(e)'s literal "kill the tab, reopen, see all 3" is met
  for already-acked photos (confirmed by the server-contract check's `GET`
  round-trip) but not for a photo lost mid-flight. A full offline queue is a
  larger, separable addition and was not part of the approved slice.
- **No specific "+2 outer-end photos" counter (§3 step 3/5, §12a.4).**
  `photoCountGuidance` guides against the protocol's plain per-arm (12) and
  total (15, target 25–35) numbers from §4.2's own table, not the
  narrower field-protocol detail that two of those photos should be taken
  past each arm's far end. General photo-count guidance was what was asked
  for; the outer-end-specific counter is a refinement, not a different
  check, and can be added without restructuring this pass's code.
- **Two entry points, not a toggle (§9.4).** The plan's step 1 design is two
  buttons that switch between an import view and a guided-capture view. This
  pass instead places `#step-capture` as its own always-visible card next to
  the existing upload card — both work independently and simultaneously
  (they create different jobs), which is simpler to wire and does not
  conflict with anything, but is visually a "two cards" layout rather than
  the plan's "two entry buttons, one visible view" design. A cosmetic
  difference, not a functional gap.
- **"Exclude last photo" is not arm-scoped.** It excludes the most recently
  kept photo across the whole session, not scoped to the arm currently
  selected for the next shot. A user who switches arms without shooting and
  then excludes will remove the last photo of whichever arm they shot last,
  not necessarily the arm shown as selected. Narrow edge case, not fixed in
  this pass.
- **Retake reuses a new uuid, not the same one.** §10's idempotent-PUT retry
  design (same uuid, new bytes) is for a *failed upload* retry. A user
  wanting to redo a *kept, successfully-uploaded* photo instead excludes it
  (soft delete) and shoots a fresh photo with a new uuid — functionally
  equivalent (the excluded one is dropped from `sequence_checks`, per S2),
  simpler to implement, and consistent with the endpoint's own semantics
  (`DELETE` is documented as "a retake replaces" via a fresh entry, not via
  reusing the old uuid).

### Browser/field limitations (not testable outside a real device/browser)

- **No headless browser in this environment**, same gap S1 and S2 both
  recorded. `capture.js`'s DOM wiring (event listeners, `createImageBitmap`,
  `FormData`/`fetch`, `deviceorientation`) is verified by `node --check`
  (syntax only) and the real-server HTTP-contract script above (which
  exercises the exact request/response shapes the DOM code depends on), not
  by an actual in-browser run. Manual verification on a real phone is
  recommended before field use, as it was for S1.
- **Pitch is an unvalidated proxy.** `90 - beta` is the plan's own named
  approximation (§4.2: "This is a proxy only. T3 measures the true
  elevation.") and this pass does not correct it for portrait/landscape
  orientation or a non-vertical holding angle — it was not checked against
  a real device's actual `deviceorientation` output, only reasoned about
  from the API's documented axis conventions.
- **Heading calibration is unknown and, by design, unneeded.** Raw
  `alpha`/`webkitCompassHeading` values are used without any offset
  correction. This is safe for the *relative* guidance this pass computes
  (spread within an arm, separation between arms — see the robustness note
  under "Change made"), but the values are not meant to be, and are not,
  compass-accurate bearings; anything that needs a true bearing (§5.4's
  model-azimuth alignment) is S4/S5 scope.
- **Criterion 5 (T0/T1 latency on a real mid-range phone/field laptop)** is
  not measured here — no physical device was used in this pass, the same
  gap S1/S2 recorded for their own pieces of the same criterion.
  `capture.js`'s T0 work (one `createImageBitmap` decode + the pure
  `capture_metrics.js` math) is a subset of what S1's upload-step preview
  already does at the same resolution, so no new latency risk is expected,
  but this is reasoning, not a measurement.
- **Criterion 6(e)** (kill-the-tab-and-reopen device test) is only partially
  addressed — see the IndexedDB deviation above. The `GET .../capture`
  resume path itself (session id in `localStorage`, manifest re-fetched on
  load) was exercised against the real server in the validation run, but not
  from an actual killed mobile tab.
- **`DeviceOrientationEvent.requestPermission` and `webkitCompassHeading`**
  are iOS-specific APIs reasoned about from their documented behavior, not
  exercised on an iOS device in this pass (none available in this
  environment).

### Acceptance status

- **Photo count, overlap gaps, marker views, arm transition, camera height,
  and viewpoint diversity guidance** — the instruction's explicit list — are
  all wired from the existing S2 sequence checks (plus client sensor data
  for the two sensor-derived items) to the capture UI, with unavailable
  sensors omitting that guidance rather than showing a failure. Verified by
  the new unit tests (`test_capture_guidance.mjs`, 8/8) and the real-server
  contract check above; not verified in an actual browser (see
  "Browser/field limitations").
- **Plan criterion 5 (T0/T1/T3 latency)** — not measured (no device), same
  gap as S1/S2's own T0/T1 pieces.
- **Plan criterion 6(a–d)** — unaffected, still met (S2's own coverage;
  this pass added no server-side code). **Criterion 6(e)** — partially met,
  see the IndexedDB deviation above.
- **Criterion 7 (no regression).** Met. `pytest -q -k "not e2e"`: 209 passed,
  identical to S2's baseline; no `landslide/` or `server/*.py` file touched,
  so every existing endpoint (including `POST /api/jobs`) is unchanged by
  construction. `node --test` on the full existing JS suite
  (`test_capture_metrics.mjs`, `test_coords.mjs`) plus the 8 new tests: 22/22
  passed.
- **Out of this pass's approved scope, not attempted:** marker checklist
  (§6), rim-clearing step (§3 step 2), top-down sketch (§5.4), IndexedDB
  offline queue (§10's blob-retry half), the two-button entry-point toggle
  (§9.4). These are the remainder of the plan's full S3 row plus pieces of
  S4/S5 the top-down sketch depends on.
- **S4–S7 remain not started**, per the instruction for this pass.

## S4 — Preview SfM (`GUIDED_CAPTURE_PLAN.md` §12 stage table, row 4)

**Base:** `main` @ `b056abd`. This pass changed `landslide/sfm.py`,
`landslide/capture.py`, `server/worker.py`, `server/routes.py`,
`server/jobs.py`, added `tests/test_capture_preview.py` and
`tests/test_capture_preview_routes.py`, and extended `tests/test_sfm.py`.
No `server/static/*` (UI) file was touched — the stage-table's S4 row scope
is backend only (`reconstruct(max_attempts=…)`, `worker.run_preview`,
`preview_report`, finalize blocking); a preview panel in the capture UI is
S5/S6b territory (§9.4's "Preview panel" bullet lists the top-down sketch
and recapture list, both S5-only). No gate, threshold, or existing endpoint
was changed. **S5–S7 not started**, per the instruction for this pass.

### Change made

Per the stage-table scope ("`reconstruct(max_attempts=…)`, `worker.run_preview`,
`preview_report` (registration, collinearity, focal, elevation, azimuth,
stereo pairs, marker scale, datum estimate §3a.4), finalize blocking plus
override"):

- **`landslide/sfm.py`**: `reconstruct` gained `max_attempts: int | None = None`,
  which slices `_build_attempts(...)` to its first N rungs before the retry
  loop runs — exactly §9.2's own spec ("There is no other SfM change").
  `worker.run_preview` calls it with `max_attempts=1`. A bad-focal attempt
  can still insert and run its one focal-locked retry (F12) even under
  `max_attempts=1`, since that insertion happens after the ladder is sliced;
  left as is — disabling it would reintroduce a known failure mode for a
  case (diverged per-camera focal) the preview should also want to catch.
- **`landslide/capture.py`**: new `preview_report(ctx, arms=None,
  marker_side_m=None, marker_dict="auto", roi=None, log=print)` — the T3
  checks (§4.3), each reusing an existing diagnostic instead of recomputing
  it: `sfm._camera_center_collinearity`/`_focal_spread` (G4/G3),
  `densify.estimate_up` (up-vector agreement), `densify.select_pairs` (G1
  stereo-pair-share proxy), `scaling.aruco_scale` in a try block (G2/G7 +
  the "marker raised" block), and `ortho.select_region_world` +
  `volume.prism_volume` for the §3a.4 datum estimate (see the calibration
  finding below). Registration/connectivity and azimuth-arm-separation have
  no existing gate id (`gates.RULES` only covers ids `evaluate_gates` can
  fire, and neither of these ever reaches `evaluate_gates` — registration
  failure raises before any gate runs, and G6 has no separate "azimuth"
  sub-id), so two new ids (`T3_registration`, `T3_azimuth`) live in a
  parallel `T3_MESSAGES` dict, same pattern as `T1_MESSAGES`. Every other
  check reuses `gates.RULES`' id and `action` text verbatim, prefixed
  "Predicted: " (§7a.2). Returns `{...raw numbers..., "checks":
  [{id, level, text}], "blocking": [ids with level=="block"]}`.
  `roi` is accepted (forward-declared in §9.2's own worker signature) but
  never used to lift a traced polygon — the ROI-tap endpoint is S5 scope, so
  S4 always falls back to §5.1's own default (convex hull of the central
  50% of the sparse cloud's ground-plane projection).
- **`server/worker.py`**: `run_preview(job_id, photos_dir, work_dir, queue,
  capture_dir, originals_order, marker_side_m, marker_dict, roi)` — stages a
  symlink (falling back to a copy) of each kept original into
  `capture/preview_stage/{seq:03d}_{uuid}.jpg`, reconstructs into
  `capture/preview/` at `max_image_size=1000, max_attempts=1`, then calls
  `preview_report`. `photos_dir`/`work_dir` are `submit_job`'s fixed
  positional args and are unused, per §9.2's own note. `marker_dict` is an
  addition to the plan's literally-written signature (`job_id, photos_dir,
  work_dir, queue, capture_dir, originals_order, marker_side_m, roi`) — the
  session already stores a `marker_dict` (S2) and the marker check needs it;
  the plan's own worker-signature sentence predates S2/S3's actual session
  schema, so this is treated as an omission, not a deviation to flag as a
  gap.
- **`server/routes.py`**: `POST /api/jobs/{id}/capture/preview` — needs ≥3
  kept photos (400 otherwise); one preview in flight per job (a request
  during a run sets `preview["rerun_after"] = True` and returns
  `{"running": true}`, per §9.2); otherwise starts a `{"status": "running",
  ...}` manifest entry and calls `executor.submit_job(job, worker.run_preview,
  ...)` — the same crash-isolated pool every other heavy stage uses.
  `_on_preview_done` records `{"status": "done", "report": ...}` or
  `{"status": "error", "error": ...}`, and re-submits once more if
  `rerun_after` was set while it ran. `POST .../finalize` now reads the most
  recently *finished* preview's `report["blocking"]` (a job that never ran a
  preview, or whose preview is still running/stale, blocks on nothing — same
  as before S4) and refuses with `409 {"blocking": [{id, text}]}` unless
  `override=true`; `DELETE /api/jobs/{id}` also refuses while a preview is
  running (same reasoning as the existing reconstructing/measuring guard).
- **`server/jobs.py`**: `load_persisted_jobs` marks a `preview.status ==
  "running"` manifest as `"stale"` on reattach (§9.1: "a preview interrupted
  by a restart is marked stale") — the worker that was running it died with
  the server, so nothing will ever resolve it; the next `POST .../preview`
  simply starts a fresh one (`"stale" != "running"` so it isn't mistaken for
  one already in flight).

### §12b.6 calibration: the datum estimate does not discriminate, and is not surfaced

Ran `preview_report` against the cached `data/bench/{twosided,twosided_b,
twosided_cal}/work` reconstructions (already on disk from earlier stages'
own benchmark runs — no new SfM, same read-only reuse pattern S0/S0b/S0c/G9
all used) plus one real, from-scratch 1000 px/`max_attempts=1` preview
reconstruction of `twosided_cal`'s 38 images, to check §12b.6's fixed rule:
say "unstable" on `twosided`/`twosided_b` (final G9 fires on both), say
nothing on `twosided_cal` (final G9 silent).

| Scene | source | datum ratio (`dh × roi_area ÷ \|net\|`) | final G9 |
| --- | --- | --- | --- |
| `twosided_cal` | cached full-res ctx | 0.067 | silent (`ok`) |
| `twosided_cal` | real 1000px/1-attempt preview | 0.070 | silent (`ok`) |
| `twosided_b` | cached full-res ctx | 0.557 | fires (`indicative`) |
| `twosided` | cached full-res ctx | 0.449 | fires (`indicative`) |

All three exceed the 0.05 gate ratio, including `twosided_cal`, which must
stay silent. The real 1000px preview's ratio (0.070) is close to the cached
full-res proxy's (0.067) — elevation likewise agrees to 0.3° (17.99° vs
17.72°) — so the calibration failure is not an artifact of using the cached
higher-resolution reconstruction as a stand-in for the true preview; the two
methods agree with each other, they just both disagree with the required
discrimination.

**Root cause:** with no ROI tap (S5 — the endpoint doesn't exist yet), the
only ROI this stage can use is §5.1's own default (convex hull of the
central 50% of the sparse cloud's ground-plane projection). Its centroid
lands close enough to the true bowl centre to be usable for the
elevation/azimuth checks, but the polygon itself is about 3x the true
polygon's area (98.5 m² true vs measured ~309 m²) — it picks up a ring of
surrounding terrain along with the slide, which both enlarges the `roi_area`
term and shifts what counts as the "0.5-2.5 m rim band" outward onto ground
that was never meant to be the datum's own rim. That is enough to swamp the
ratio's own discrimination between a scene whose datum the rim predicts and
one it doesn't.

Per §12b.6's own fixed decision rule ("If either fails, the T3 datum
estimate is dropped from the UI, not re-tuned"): `datum_estimate`'s raw
numbers (`datum`, `datum_holdout_m`, `datum_holdout_windows`,
`net_volume_m3`, `roi_area_m2`, `ratio`) are still computed and returned —
useful diagnostic data, and there for whoever revisits this once S5 lands a
real ROI — but the ratio is never turned into a `checks` entry.
`T3_MESSAGES["T3_datum_unstable"]` is dead code until then;
`tests/test_capture_preview.py::test_datum_estimate_never_becomes_a_check`
pins this so a future change can't silently re-enable it without deciding
to. No gate, threshold, or the plan's own 0.05 ratio was changed to make
this "pass" — it is reported as a miss, the same as S0b's/S1's/S2's own
honestly-recorded gaps.

**Elevation is also an approximation, not exactly re-validated against the
plan's own recorded figures**, for the same ROI-centroid reason: S0c's own
progress notes record `twosided_cal`'s median camera elevation as 22.9-23.1°
(from the ground-truth bowl centroid); this stage's proxy gives 17.7-18.0°
(from the central-50%-of-sparse-cloud centroid). `DATUM_STABILITY_
PROGRESS.md`'s own S0b section already documents this exact quantity
disagreeing by several degrees between two *ground-truth-based* methods
(16.5° / 19.25° / 23.1°, depending on which reference point counts as "the
ROI centroid") — so a sparse-cloud-density-based proxy landing in the same
general band, without an actual ROI, is treated as within the range of a
known, pre-existing measurement ambiguity, not a new bug. It only ever
produces a `warn` (`G5_partial`), never a `block`, so it cannot cause a
false-block (§11 criterion 3's own requirement).

### Focused validation

```
.venv/bin/python -m pytest -q -k "not e2e"
```

Result: **235 passed, 3 skipped, 23 deselected** — S3's own baseline was 209
passed; the 26 new tests (1 in `tests/test_sfm.py`, 17 in
`tests/test_capture_preview.py`, 8 in `tests/test_capture_preview_routes.py`)
account for the difference exactly, and every pre-existing assertion still
passes unmodified. (The "worker process crashed (native crash)" log line is
`test_worker_crash_is_isolated_and_pool_recovers` deliberately killing a
worker — pre-existing, documented, not a new failure.)

`tests/test_capture_preview.py` covers `preview_report`'s own logic: three
pure-helper tests (`_circular_mean_deg`, `_circular_diff_deg`,
`_default_roi_polygon`), nine threshold/level tests on a hand-built flat
synthetic scene (registration block on a missing arm, azimuth-separation
block/no-block, collinearity block, focal-spread warn, stereo-pair-share
warn, low-elevation warn, marker-raise block, marker-warning warn, and the
datum-estimate-never-a-check pin above) with `_camera_center_collinearity`/
`_focal_spread`/`select_pairs`/`aruco_scale` monkeypatched (each already has
its own dedicated tests elsewhere — this file tests `preview_report`'s
threshold wiring, not those functions' own correctness), and three
parametrized tests against the real cached `twosided`/`twosided_b`/
`twosided_cal` reconstructions asserting `blocking == []` on all three
(criterion 3's "false-block = 0" requirement, the part of criterion 3 this
stage can actually check without S5's coverage/void calibration).
`tests/test_capture_preview_routes.py` covers the backend plumbing
(preview lifecycle, rerun-while-running, finalize's blocking gate with and
without `override`, the delete-while-running guard, stale-on-restart) with
`executor.submit_job` monkeypatched, same pattern as
`tests/test_capture_session.py`. `tests/test_sfm.py` gained one test pinning
`max_attempts`' slicing behavior.

### Latency (criterion 5, T3 ≤ 60s for 30 photos at 1000px on a 4-core laptop)

One real, from-scratch preview reconstruction (not the cached-ctx shortcut
above): `twosided_cal`'s 38 images, staged and reconstructed exactly as
`worker.run_preview` does (`max_image_size=1000, max_attempts=1`), on this
dev box (12-core, more than the plan's "4-core laptop" target; `SFM_THREADS`
still caps extraction at 4 either way). Result: **95.5 s** for
`sfm.reconstruct` (the ladder picked "global (GLOMAP)" first, since n≥25) +
**15.7 s** for `preview_report` (dominated by `aruco_scale`'s own per-view
marker decode/detection and `select_pairs`) = **111.2 s total for 38
photos** — scaling down to 30 photos (×30/38) gives roughly 88 s, still
comfortably over the 60 s budget even on hardware stronger than the named
target. **This criterion is not met** on this box; recorded as a real
number, not adjusted. Whether this is a real field-blocking cost or an
artifact of this specific dev machine's COLMAP build is a question for S7's
own device/field validation, not something this pass attempts to fix (S4's
scope is the preview mechanism's correctness, not its performance — no
profiling or ladder-tuning was done).

### Deviations from the plan's S4 description

- **No frontend.** The stage-table's S4 row lists no `server/static/*`
  file, and §9.4's "Preview panel" (status, top-down sketch, recapture list,
  predicted G-class) is explicitly built from S5's coverage map and ROI —
  none of which exists yet. `POST .../capture/preview` and the manifest's
  `preview` field are fully usable via `GET .../capture` today; wiring a
  "Run preview" button into `capture.js` is left for whoever starts S5/S6b,
  consistent with how S2 (also backend-only) left its own UI wiring to S3.
- **The §3a.4/§12b.6 datum estimate is computed but not surfaced** — see the
  calibration finding above. This is the plan's own sanctioned outcome for a
  failed calibration, not a shortcut.
- **The elevation/azimuth proxy uses the sparse-cloud density centroid, not
  a traced ROI** — again, the ROI tap is S5 scope; §5.1's own "central 50%"
  fallback formula is what this pass implements, and its measured accuracy
  gap is documented above rather than hidden.
- **No coverage/void prediction** — the stage table's own S4 row explicitly
  excludes it ("Criteria 3 (except the G6 prediction)"); §5's cell map, ROI
  tap and `c_rej`/`c_ok` calibration are S5's row, not S4's.
- **`preview_report`'s stereo-pairs check reuses `densify.select_pairs`
  as-is** (no `StereoConfig` tuning for the 1000px preview resolution) —
  the plan names no separate calibration for this proxy, and it only ever
  warns, never blocks.
- **Field/real-device criteria (5's T0/T1 pieces, 6(e), 8) are unaffected by
  this pass** — S4 touches none of that code; they remain S3/S7's own open
  items, unchanged.

### Acceptance status

- **`reconstruct(max_attempts=…)`, `worker.run_preview`, `preview_report`,
  finalize blocking plus override** — all built and tested, per the
  stage-table's own scope list.
- **Criterion 3 (except the G6 prediction)**: the block/no-block half is
  met — see the parametrized real-reconstruction tests (false-block = 0 on
  `twosided`/`twosided_b`/`twosided_cal`) and the hand-built threshold
  tests for every other preset class named in criterion 3's bullet list
  (`collinear`→G4, `nadir`/`oblique60`→marker-raise, `descending`/`sparse8`
  → elevation/registration, `arc`-family → azimuth-separation). Not
  re-verified against real reconstructions of those other presets in this
  pass (no cached `work/` exists for them, and re-running their SfM was out
  of this pass's budget) — the hand-built fixture tests are the coverage
  for those thresholds; a slow `tests/test_e2e_capture.py` replaying every
  preset through the real endpoint end-to-end remains S5's own criterion-3
  "full" checkbox, per the stage table.
- **Criterion 5 (T3 latency)**: measured, not met on this dev box (111 s for
  38 photos against a 60-for-30 budget) — see above.
- **§12b.6 (datum-estimate calibration)**: run, failed to discriminate, and
  handled exactly as the plan's own decision rule specifies (dropped from
  `checks`, not re-tuned) — documented above, with a regression test pinning
  it.
- **S5–S7 remain not started**, per the instruction for this pass.

## S5 — Capture persistence and recovery (approved slice, §10)

**Base:** `main` @ `be8642f`. This pass added `server/static/js/capture_store.js`
and changed `server/static/js/steps/capture.js`, `server/static/js/capture_guidance.js`,
`server/static/index.html`, `tests/test_capture_guidance.mjs`. No `landslide/`,
`server/*.py`, gate, or threshold file was touched — every piece of server-side
support this pass needed (idempotent uuid+sha256 PUT, `GET /api/jobs/{id}`
carrying `status` and `capture` together, the finalize `status in ("capturing",
"error")` retry guard, `preview.status -> "stale"` on restart) already shipped
in S2/S4. This pass implements the client-side half of §10 that S3 explicitly
deferred ("Not built: IndexedDB offline-blob queue / retry backoff (§10,
`capture_store.js`)"), plus restoring preview status and finalize-crash
recovery across a reload — **not** the plan's own numbered "S5" row (§12
stage table: "Coverage and recapture," ROI tap, cell map, `c_rej`/`c_ok`
calibration). That row is untouched by this pass; this is a separately
approved slice reusing the "S5" label for what the task instruction named
"capture persistence and recovery." **S6b, S7, and the plan's own S5
(coverage/recapture) are not started.**

### Change made

- **`server/static/js/capture_store.js`** (new, no DOM, not `node --test`-able
  in this repo — same "no fake-indexeddb dependency installed" gap S1/S2/S3
  documented for headless-browser testing): an IndexedDB wrapper with exactly
  the four functions §9.4 names — `put(jobId, uuid, blob, meta)`,
  `listPending(jobId)`, `ack(uuid)`, `clear(jobId)` — one object store keyed
  by `uuid`, indexed by `jobId`. Every function degrades to a safe no-op
  (`put` returns `false`, reads return `[]`) if `indexedDB` throws or is
  absent, so a photo still uploads directly; it just has no retry buffer if
  that direct attempt fails, the same as S3's pre-existing behavior.
- **`server/static/js/capture_guidance.js`**: four new pure functions, kept in
  this module (not a new file) because they are exactly what its own header
  already claims to be — sequence/manifest-derived guidance for the capture
  UI, unit-testable with `node --test` — and `capture.js` already imports it
  as `cg`:
  - `nextCaptureSeq(manifestPhotos, pendingMeta)` / `lastCaptureArm(...)` —
    §10 "return to the same arm and seq": the next seq and the arm to resume
    on, computed over the server manifest **and** any still-queued (not yet
    acked) client entries, so a photo stuck retrying after a crash can never
    have its seq reused by a fresh one taken right after reload.
  - `nextBackoffMs(currentMs, ok)` — §10 "retries with exponential backoff
    (2s → 60s cap)", as a pure doubling/reset function.
  - `pendingNotYetOnServer(manifestPhotos, pendingUuids)` — §10 "re-queue any
    IndexedDB entries whose uuid is missing from the manifest": the set-
    difference that decides which queued photos actually need a re-send
    (the rest are acked locally without another PUT — the "avoid duplicate
    photos" half of the instruction, on top of the server's own uuid+sha256
    idempotency, which makes even a redundant re-send harmless but wasteful).
- **`server/static/js/steps/capture.js`**: three additions, each mapped to one
  named part of the task instruction:
  - **Retry-safe client persistence + interrupted-upload recovery.**
    `takePhoto` now calls `store.put(...)` (writing the blob and its
    `{seq, arm, client}` meta to IndexedDB) *before* attempting the PUT, per
    §10's own ordering ("the change handler writes the blob plus metadata to
    IndexedDB **first**"). A failed PUT no longer tells the user to retake
    (S3's behavior) when the write persisted — it shows "upload queued — will
    retry automatically" and starts `scheduleRetry()`, which drains the
    IndexedDB queue with `nextBackoffMs`'s 2s→60s backoff until it empties.
    The old "ask the user to retake" path is kept as the fallback only when
    `store.put` itself failed (no IndexedDB available) — `seq` is rolled back
    in that case exactly as before, since nothing is queued to fill it.
    `loadSession` now fetches `store.listPending(id)` on every reload and
    calls `reconcilePending`, which acks (never re-sends) any uuid the
    manifest already has, then immediately drains whatever's left — not
    waiting for the first backoff tick, so a session resumed right after a
    crash starts recovering at once.
  - **Preview-state restoration.** `renderPreview(preview)` renders
    `capture.preview`'s four states (`null`/`running`/`stale`/`done`/`error`)
    into a new `#capture-preview` panel: running polls `GET .../capture` at
    the same 1.2s cadence `upload.js`'s own poll uses; stale (§9.1: "a
    preview interrupted by a restart is marked `stale`") and error both offer
    a "Re-run preview" button that calls the existing S4
    `POST .../capture/preview` endpoint; done shows whether its report
    predicts a blocking item. This is deliberately not the plan's own S5/S6b
    preview panel (§9.4: status, **top-down sketch**, **recapture list**,
    predicted G-class per gate) — those need §5's coverage map and ROI tap,
    neither of which exists. Without *some* trigger there is nothing to
    restore state for (S4 shipped no frontend at all), so a minimal
    "run/re-run" control was added to make the restoration observable and
    usable — this is the one piece of this pass that is an addition rather
    than a gap-closing, and is called out as such rather than silently
    smuggled in under "recovery."
  - **Finalize recovery.** `loadSession` now reads `status` (`GET
    /api/jobs/{id}`, not just `GET .../capture`, precisely so `status` and
    `capture` arrive together) instead of assuming the job is still
    `capturing`. Three branches: (a) `status` is `capturing` or `error` →
    show the capture screen (b) `status == "error"` and
    `capture.finalized` is still `null` → §10 "finalize interrupted": render
    `#capture-finalize-retry` with the server's error and a button that calls
    the existing `finish()` (which POSTs finalize again; the server's own
    `status in ("capturing", "error")` guard, S2, accepts the retry and
    re-copies from `capture/originals`) (c) any other status (finalize
    already succeeded and the job moved on to reconstructing/ready/etc.) →
    hand off straight to `upload.js`'s `resumeJob` instead of showing a
    capture screen that would now 409 on every photo action. Branch (c) is a
    gap S3 had: reloading mid- or post-finalize showed a stale, broken
    capture UI. `finish()` itself also now warns (`confirm()`) if photos are
    still queued when "Finish and reconstruct" is pressed, since finalize
    only ever copies from server-confirmed `capture/originals` — a queued
    photo would silently not be included otherwise — and clears the
    IndexedDB queue for the job on a successful finalize (nothing queued for
    it can ever be uploaded again).
- **`server/static/index.html`**: three new elements inside `#capture-session`
  — `#capture-finalize-retry`, `#capture-pending`, `#capture-preview` — all
  reusing the existing `.capture-checks`/`.note`/`.warn`/`.err`/`.ok` classes
  from S1's panel (no `style.css` change, no cache-bust bump needed).
- **`tests/test_capture_guidance.mjs`**: four new tests, one per pure
  function above, covering the seq/arm resume logic (including the
  crash-mid-upload case where a queued photo holds the highest seq), the
  backoff doubling/cap/reset, and the manifest/queue set-difference.

### Focused validation

```
node --test tests/test_capture_guidance.mjs tests/test_capture_metrics.mjs tests/test_coords.mjs
node --check server/static/js/steps/capture.js server/static/js/capture_store.js \
             server/static/js/capture_guidance.js
.venv/bin/python -m pytest -q -k "not e2e"
```

Results: **26 passed** (4 new in `test_capture_guidance.mjs`), 0 failed.
`node --check` clean on all three changed/new JS files. `pytest -q -k "not
e2e"`: **235 passed, 3 skipped, 23 deselected** — identical to S4's own
baseline (no `landslide/`/`server/*.py` file was touched, so this is the
expected outcome, not a re-run of unrelated work).

**Server-contract check (real server, not just the unit tests):** started
`uvicorn server.main:app`, then drove the exact sequence `loadSession`/
`reconcilePending`/`finish` depend on with a small script (not committed) —
`POST /api/jobs/capture` → three `PUT .../capture/photos/{uuid}` calls (real
fixture JPEGs from `tests/fixtures/capture_t1/`) → `GET /api/jobs/{id}`
confirmed to carry `status == "capturing"` **and** the full `capture` manifest
together (what `loadSession` reads in one call) → a same-uuid re-PUT confirmed
still idempotent (3 manifest entries, not 4 — S2's own guarantee, re-checked
here because this pass's recovery path leans on it not regressing) →
`POST .../capture/finalize` → `GET /api/jobs/{id}` confirmed `status` moved to
`reconstructing` (the exact signal branch (c) above hands off on). The test
job was deleted afterward.

### Failure-recovery evidence (§10 table, each row this pass covers)

| §10 row | Mechanism exercised | Where |
| --- | --- | --- |
| Tab killed mid-upload | `store.put` before the PUT; on reload, `store.listPending` finds the uuid, `pendingNotYetOnServer` says it's missing from the manifest, `reconcilePending` re-sends it via the same uuid (idempotent) | `capture.js::reconcilePending`/`drainQueue`, pinned by the `pendingNotYetOnServer` unit test |
| Page refresh / reopen | `loadSession` restores `arm`/`seq` from `nextCaptureSeq`/`lastCaptureArm` over manifest + pending, not just manifest length | `capture.js::loadSession`, pinned by the `nextCaptureSeq`/`lastCaptureArm` unit tests (crash-mid-upload case included) |
| Upload fails / network drops | `scheduleRetry`/`drainQueue` with `nextBackoffMs`'s 2s→60s exponential backoff; UI shows the pending count via `#capture-pending` | `capture.js::scheduleRetry`, pinned by the `nextBackoffMs` unit test |
| Duplicate send (retry after a lost response) | Unchanged server guarantee (S2's uuid+sha256 check); this pass's own contract check re-confirmed it (3 entries, not 4) rather than assuming it still holds | server-contract check above |
| Server restart during capture | Unchanged server guarantee (S2's `load_persisted_jobs`); this pass adds no client dependency on the job staying in memory across a restart beyond what `GET /api/jobs/{id}` already reports | — |
| Server restart during preview | `preview.status` flips to `"stale"` server-side (S4, unchanged); this pass's `renderPreview` is the first place that *shows* `"stale"` and offers "Re-run preview" — S4 shipped no frontend for it at all | `capture.js::renderPreview` |
| Finalize interrupted | Server-side recovery unchanged (S2); this pass's `renderFinalizeRetry` is the first client-side surface for it — S2/S3 covered the server guard and a `TestClient` retry, never a browser-facing "retry finalize" action | `capture.js::renderFinalizeRetry` |
| Two devices/tabs on one session | Not touched — §10 itself names this "not a supported workflow"; no change attempted | — |

**6(e), the manual device test** ("kill the tab after 3 shots, reopen, see
all 3 plus the correct arm and seq") — still not run on a real device (no
headless browser in this environment, same limitation S1–S4 recorded), but
the mechanism it exercises is now implemented and unit-tested piece by piece
(above), and the reconcile/resume logic was exercised end-to-end against the
real server in the contract check (upload → reload-equivalent `GET` →
idempotent re-send check). This closes the *design* gap S3 left open
("A full offline queue is a larger, separable addition and was not part of
the approved slice"); a literal kill-the-tab-and-reopen run on a phone
remains S7's own field-validation item.

### Deviations from the instruction

- **A minimal "Run preview" trigger was added.** The instruction says
  "preview-state restoration," and S4 shipped the preview endpoint with *no*
  client trigger at all (its own progress notes: "wiring a 'Run preview'
  button into `capture.js` is left for whoever starts S5/S6b"). Restoring
  state for a preview that can never be started from the UI would be
  untestable and largely theoretical, so this pass added the smallest
  possible trigger/status control — not the plan's own S5/S6b preview panel
  (top-down sketch, recapture list, predicted G-class), which needs §5's
  coverage map and stays unbuilt.
- **No coverage/recapture calibration, ROI tap, or ROI-aware preview
  changes.** The plan's own numbered "S5" (§12 stage table row 5) is a
  different, larger piece of work with its own exit criteria (criteria 3
  full, criterion 4, `c_rej`/`c_ok` calibration). This pass does not touch
  any of it, per the instruction's explicit "do not begin S6b, S7" and its
  own narrower description of what "S5" means here.
- **`finish()`'s pre-existing finalize-blocking error display is unchanged.**
  A 409 from `POST .../finalize` with a `{"blocking": [...]}` body renders as
  `[object Object]` in the `alert()` (`api.js`'s `msg = detail || msg` when
  `detail` is a dict, not a string) — a real S4-introduced display bug, but
  it is about *showing blocking reasons*, not persistence/recovery, and
  fixing it well belongs with S6b's "capture report" / blocking-display work
  named in the stage table. Left as found, not silently patched.
- **No IndexedDB unit tests.** Same gap S1/S2/S3 already recorded for every
  DOM/IndexedDB-touching module in this repo: no `fake-indexeddb` or headless
  browser dependency is installed, so `capture_store.js` itself is verified
  only by `node --check` (syntax) and indirectly by the real-server contract
  check (which exercises the manifest shapes it stores, not the IndexedDB
  calls themselves, since the check script is plain Python). Every piece of
  logic that *can* be tested without a DOM (seq/arm resume, backoff, the
  missing-from-manifest set difference) was pulled out into
  `capture_guidance.js` and is unit-tested — see the table above.

### Preserved

- No `landslide/`, `server/*.py`, gate, or threshold file touched. The
  existing `POST /api/jobs` multi-file path and every S2/S4 endpoint are
  byte-for-byte unchanged. Legacy saved jobs and non-capture jobs are
  unaffected (this pass's `loadSession` change only runs when
  `localStorage[LS_CAPTURE_KEY]` is set, i.e. a guided-capture session was in
  progress).
- S3's existing behavior (arm/photo-count/marker/overlap/transition/height
  guidance) is unchanged; this pass only replaces the failure branch of
  `takePhoto` and the body of `loadSession`/`finish`.

### Acceptance status

- **Retry-safe client persistence, interrupted-upload recovery.** Met — see
  the failure-recovery table. Device-level confirmation (6(e)) remains S7.
- **Preview-state restoration.** Met, with the scope note above (a minimal
  trigger was added; the full preview panel is still S5/S6b).
- **Finalize recovery behavior.** Met — `renderFinalizeRetry` plus the
  status-based hand-off in `loadSession` cover both halves of §10's
  "finalize interrupted" row (retry after a crash; don't strand the user
  after a finalize that already succeeded).
- **Server manifest preserved as authoritative; duplicate photos avoided.**
  Met — no server file changed; the client only acks/re-sends against what
  `GET .../capture` reports, never invents or reorders manifest state, and
  `pendingNotYetOnServer` is exactly the "don't re-send what the manifest
  already has" rule, unit-tested.
- **Regression.** `pytest -q -k "not e2e"`: 235 passed, identical to S4's
  baseline. `node --test`: 26/26. `node --check`: clean.
- **The plan's own S5 (coverage/recapture), S6b, and S7 are not started**,
  per the instruction for this pass.

## S6b — Capture report on the result (`GUIDED_CAPTURE_PLAN.md` §9.3, §12 stage
table row 6)

**Base:** `main` @ `644029a`. This pass added `server/jobs.py::_capture_report`
(called from `Job.snapshot()`), a `renderCaptureReport` function in
`server/static/js/steps/result.js`, a `#result-capture-report` container in
`server/static/index.html`, and four tests in `tests/test_jobs.py`. No
`landslide/`, gate, or threshold file was touched — every input this stage
needs (`reason_ids`/`measurement_path`/`RULES`, S6a; `sequence_checks`/
`preview_report`, S2/S4; `capture.finalized`, S2) already exists. **S7 not
started**, per the instruction for this pass.

### Change made

Per the stage-table scope ("`capture_report` row, 'finalized with override'
label"), and the pass instruction's fuller framing (capture-quality findings,
preview results, measurement path, gate reasons, clear next actions; datum
problems distinguished from capture problems; early datum estimates kept
informational):

- **`server/jobs.py::_capture_report(capture)`** (new): a pure, display-only
  function that turns a job's `capture` manifest (§9.1: `sequence`, `preview`,
  `finalized`) into a summary dict — `arms` (per-arm kept-photo counts),
  `total_photos`, `marker_views` (per-arm and total), `preview_status`,
  `elevation_deg` (the latest finished preview's median camera elevation),
  `arm_separation_deg`, `preview_checks` (the preview's own warn/block
  findings, each already carrying "Predicted: " + the same `RULES`/
  `T3_MESSAGES` action text §7a.2 specifies — no new vocabulary, no copy of
  that text into this function), `datum_estimate_ratio` (§3a.4/§12b.6's
  number, reported as raw context only — see "informational" below),
  `finalized_override` (bool), and `overridden_checks` (the blocking preview
  items that `override=true` bypassed at finalize, §9.1's
  `finalized.blocking_at_finalize`, resolved back to their own text).
  **`Job.snapshot()`** calls it and merges the result into a *copy* of
  `self.result` as `result["capture_report"]`, only when `self.capture is not
  None` — never into `self.result` itself, so `state.json` gains no new
  persisted field and the report is always computed fresh from the manifest's
  current state. A job with no capture session (every pre-existing upload)
  gets no `capture_report` key at all, not an empty one.
- **`server/static/js/steps/result.js::renderCaptureReport(r)`** (new),
  called at the end of `showResult(r)`: renders a `#result-capture-report`
  block — reusing the existing `.capture-checks`/`.ok`/`.warn`/`.err`/`.note`
  CSS classes from S1's panel, no `style.css` change — only when
  `r.capture_report` is present. Shows arm/marker-view counts, the preview's
  median elevation and arm separation (or its status, if it never finished),
  the preview's own predicted findings as a list (block items in `.err`, warn
  items in `.warn` — the same visual language `result.js` already uses for
  the *final* reasons list), the datum-estimate ratio labelled explicitly
  "informational only, not a prediction — see the result's own 'path' and
  reasons above for the actual measurement" (never as a path guess — §12b.6's
  own finding that this ratio doesn't discriminate reliably), and — only when
  `finalized_override` is true — a `.err`-styled "⚠ finalized with override"
  line naming exactly which preview warnings were bypassed.
- **`server/static/index.html`**: one `<div id="result-capture-report"
  class="capture-checks hidden"></div>` inside `#step-result`, between the
  existing `#result-warnings` box and the material-factor row. No
  `style.css` change (cache-bust stays at `v=5`, set by S3): every class this
  block uses already existed before this pass.
- **`tests/test_jobs.py`**: four new tests, directly on `Job`/`_capture_report`
  (same pattern this file already uses for `Job`-level behavior, no HTTP
  layer needed) — a normal (non-capture) job gets no `capture_report` key; a
  full capture manifest (arms, marker views, a finished preview with a warn
  and a block check, `datum_estimate.ratio`, no override) produces the
  expected summary; `finalized.override=true` with
  `blocking_at_finalize=["T3_azimuth"]` produces `finalized_override: True`
  and `overridden_checks` resolved to that check's own text; and a session
  with `preview: None` (never previewed) returns `preview_status`,
  `elevation_deg`, `preview_checks`, and `datum_estimate_ratio` all empty/None
  rather than raising.

### Distinguishing capture problems from datum problems (the pass instruction's
own framing)

This block does not re-derive or duplicate that distinction — S6a already
built it (`gates.classify_measurement_path`, the "path" row, and
`REASON_RULES`' `cls` field in `result.js`, shown above this block on the same
page) and this pass's job is only to add the *capture-session* report next to
it, not to re-classify anything. The two blocks read together: the "path" row
and reason list (S6a) say what the *final* gates found and whether recapture
can fix it or only a pre-event DEM can; this block (S6b) says what the
*on-site preview* already predicted before reconstruction ever ran. Nothing in
`preview_checks` can be mistaken for the datum-fault class: every T3 check
reuses a `capture`/`info`-class id from `gates.RULES`, or one of the two
preview-only ids (`T3_registration`, `T3_azimuth`) that have no gate
counterpart at all — §12b.6 already established that the one datum-flavoured
number T3 computes (the early ratio) never becomes a `checks` entry, so it
cannot appear in `preview_checks`; it is surfaced separately, labelled
informational, exactly per this pass's instruction to "keep early datum
estimates informational."

### Focused validation

**Now run** (the tool-execution outage recorded below has since cleared).
Commands and results, this pass:

```
.venv/bin/python -m pytest -q tests/test_jobs.py     # 10 passed
node --check server/static/js/steps/result.js         # syntax OK
node --test tests/*.mjs                                # 26 passed
.venv/bin/python -m pytest -q -k "not e2e"             # 239 passed, 3 skipped, 23 deselected
```

239 passed matches this section's own prior prediction exactly (235 baseline
+ 4 new `test_jobs.py` cases, no other count changed). The
`worker process crashed (native crash)` line in the fast-suite output is
`test_worker_crash_is_isolated_and_pool_recovers` deliberately killing a
worker (same known/documented behavior S0 already noted), not a new failure.
`git diff --stat` against `main` shows only the five files this pass's own
"Change made" section names (`GUIDED_CAPTURE_PROGRESS.md`, `server/jobs.py`,
`server/static/index.html`, `server/static/js/steps/result.js`,
`tests/test_jobs.py`) — no unrelated file touched. No code defect found: the
manual trace recorded below (from when the outage blocked execution) is
confirmed correct by this run, not superseded by it.

Still not exercised (unchanged from this section's original notes, and out of
this validation pass's scope): no real capture-session walk, no browser
render of `renderCaptureReport` (no headless browser in this environment, per
every prior stage's own notes), no UI smoke test against the replayed
presets.

<details>
<summary>Original note, while the tool-execution outage was active (superseded above)</summary>

**Not run this pass.** Every execution path (`Bash`, and `Agent` as a
fallback) returned a persistent tool-level failure for the whole session —
"the server-side auto mode classifier gave no verdict" — on every attempt
(read-only tools were unaffected and are what this section's own research and
all of this pass's file edits relied on). This is a session-infrastructure
outage, not a decision to skip validation, and it is recorded here rather than
fabricating a pytest/node run. In its place:

- **Manual trace of `_capture_report`** against each of the four new test
  cases by hand (reading the function alongside each test's input dict,
  confirmed field-by-field: counts pass through `seq.get(...)` unchanged;
  `elevation_deg`/`arm_separation_deg` read the preview report's own keys;
  `preview_checks` filters `checks` to `level in ("warn","block")` and keeps
  `id`/`level`/`text` verbatim; `datum_estimate_ratio` reads
  `datum_estimate.get("ratio")`; `overridden_checks` is `[]` whenever
  `override` is falsy and otherwise resolves each `blocking_at_finalize` id
  through a `by_id` map built from the *same* `checks` list) — no discrepancy
  found between the code and the expected values asserted in each test.
  `Job(...)` with `capture=None` is `snapshot()`-safe by inspection: the
  `if self.capture is not None` guard is the same shape S2's own `capture`
  key already uses two lines above it.
- **`node --check` was not run.** The file was read in full after editing
  (reproduced above in-conversation) and every added brace/paren/template
  literal is balanced by inspection; this is a weaker check than an actual
  parse and is recorded as such.
- **No server smoke test, no real capture-session walk, no browser render.**
  S1–S5's own progress notes already establish there is no headless browser
  in this environment; this pass adds no new gap on that front, but unlike
  every prior stage it could not even reach the `TestClient`/`uvicorn`-level
  checks those stages used, because the outage blocked all process execution,
  not just browser automation.

This is a materially weaker validation than every prior stage recorded and is
flagged here, not smoothed over.

</details>

### Deviations from the plan's S6b description and this pass's instruction

- **"Preview coverage estimate"** (§9.3's own capture-report field list) is
  not included. §5's coverage/void cell map and its `c_rej`/`c_ok` calibration
  are the plan's own numbered S5 (not built — see that row's own progress
  section above, which is a different, unbuilt slice from the "S5" label
  reused earlier in this file for capture persistence/recovery). There is no
  coverage number anywhere in `capture.preview.report` for this function to
  read; inventing one here would mean computing it, which is S5's scope, not
  S6b's. Every other named field (arms, separation, median elevation, marker
  views) is present.
- **No new capture-time vocabulary.** `preview_checks` and `overridden_checks`
  both reuse the `text` strings `preview_report()` already produced and stored
  in `capture.preview.report.checks` — this pass does not add a JS-side copy
  of `T3_MESSAGES` (unlike S6a's `REASON_RULES`, which had to copy
  `gates.RULES`' text because no endpoint serves it to the browser). The
  preview's own text is already sitting in the manifest the browser already
  fetches, so copying it a second time would be pure duplication with no
  endpoint gap to work around.
- **`capture_report` is computed at `snapshot()` time, not stored in
  `job.result`/`state.json`.** This keeps `landslide/pipeline.py::measure()`
  (which has no knowledge of `job.capture`, by design — no web dependency)
  untouched, and means the report always reflects whatever `job.capture`
  currently holds rather than a value frozen at measure time. Since nothing
  can mutate `job.capture` once a session leaves `"capturing"` status (every
  capture-session route checks `status == "capturing"` first), this is
  observably identical to computing it once at finalize time, but without
  adding a field to persist.
- **No UI smoke test against the replayed presets** (the plan's own S6b exit
  check: "each replayed preset shows its path and matching instruction").
  That check needs `tests/test_e2e_capture.py` (S5's own exit criterion 3,
  still not written) plus a way to drive `result.js` outside a browser
  (no headless browser in this repo, per every prior stage's own notes) — out
  of reach for this pass even before the tool outage. The manual trace above
  is the substitute for the *data-shape* half of that check (does
  `capture_report` contain what `result.js` expects); the *rendering* half
  (does it actually show right on each preset) is unverified.

### Preserved

- No `landslide/` file, gate, or threshold touched. `evaluate_gates`,
  `classify_measurement_path`, `RULES`, `preview_report`, `sequence_checks`
  are all read-only inputs to `_capture_report`, never modified.
  `pipeline.measure()`'s return shape is unchanged (S6a's own fields are
  untouched, and this pass adds nothing to it — `capture_report` lives only
  in the `Job.snapshot()` view, never in `res`/`job.result`).
- Every existing `result.js` row (status, path, reasons, DEM-upload button,
  legacy label — all S6a) renders exactly as before; `renderCaptureReport`
  only adds a block after them and returns immediately if its container
  element is missing (a defensive no-op, not a hard dependency on the new
  `index.html` markup existing).
- A job with no capture session — every job created through the existing
  `POST /api/jobs` multi-file upload, i.e. every job created before this
  plan's guided-capture stages existed — gets no `capture_report` key and no
  visible change to its result panel.

### Acceptance status

- **`capture_report` row (arms, marker views, preview elevation/separation,
  preview's own predicted findings, early datum estimate kept informational)
  and the "finalized with override" label** — both built, per the stage
  table's own scope, with the one named field gap above (preview coverage
  estimate — not yet computable, S5 scope).
- **Capture-vs-datum distinction, clear next actions** — met by construction:
  this block adds no new classification and cannot contradict S6a's; its own
  "what the preview flagged" list carries the same action text as the final
  reasons list, and its datum-estimate line is explicitly labelled
  informational rather than a verdict.
- **Focused validation** — run (this pass's own validation pass, above):
  `tests/test_jobs.py` 10 passed, `node --check` on `result.js` clean,
  `node --test tests/*.mjs` 26 passed, fast suite 239 passed / 3 skipped / 23
  deselected (matches the predicted count exactly), `git diff --stat` limited
  to the five files this stage names. No S6b defect found.
- **S7 is not started**, per the instruction for this pass. Per the same
  instruction, this pass stops here.

## S7 — Device and field validation (`GUIDED_CAPTURE_PLAN.md` §12 stage table, row 7)

**Base:** `main` @ `95ee5c1` (S6b). This pass changed no application code —
`landslide/`, `server/*.py`, and every `server/static/*` file are byte-for-byte
unchanged from `95ee5c1`. Only this file was edited. **This is a validation-only
pass**: the instruction was to complete S7's final validation, run the planned
final tests, fix only clear integration defects actually found, and record
results/limitations/a deployment recommendation. No integration defect was
found in the product during this pass (see below), so nothing else changed.

### What S7 asks for, and what this environment can and cannot do

The stage table's S7 row is explicit: "Android Chrome and iOS Safari over
HTTP (no sensors) and HTTPS (sensors). ≥ 3 real sites, one with a pre-event
DEM," gated on criterion 8. None of that is reachable here — this is a
sandboxed dev container with no phone, no browser, and no landslide to visit.
Every prior stage (S1–S6b) already recorded this same gap for its own piece
of the work; S7 is where the plan expected it to finally get closed, and it
cannot be, in this environment. That is stated plainly in the deployment
recommendation below rather than worked around.

What *is* reachable, and is new for this pass: every stage from S1 through
S6b shipped its own route-level tests with the heavy step
(`executor.submit_job` / `worker.run_preview` / `worker.run_reconstruction`)
**monkeypatched out**, by their own explicit design (S4's own docstring:
"same pattern as `tests/test_server.py`"). That is the right call for a fast
unit-test suite, but it means no test in this repository, before this pass,
had ever driven the guided-capture HTTP API end to end through a real,
unmocked preview → finalize → reconstruction → scale → measure run. This
pass adds exactly that one run, as the closest thing to "final validation"
this environment can produce, and treats it as informative, not as a
substitute for criterion 8's real sites.

### Focused validation (regression, no code changed)

```
.venv/bin/python -m pytest -q -k "not e2e"                                        # 239 passed, 3 skipped, 23 deselected
.venv/bin/python -m pytest -q tests/test_capture.py tests/test_capture_session.py \
  tests/test_capture_preview.py tests/test_capture_preview_routes.py \
  tests/test_capture_metrics.py                                                    # 45 passed
node --test tests/*.mjs                                                            # 26 passed
node --check <every file under server/static/js/**/*.js>                          # all clean
```

239/3/23 matches S6b's own baseline exactly — no regression from anything on
`main` since `95ee5c1`. (The "worker process crashed (native crash)" fast-suite
line is, as every prior stage has noted, `test_worker_crash_is_isolated_and_pool_recovers`
deliberately killing a worker — not a new failure.)

### A real, fully unmocked end-to-end run (new for this pass)

Drove the actual, running server (`server.main.app`, via `TestClient`, no
endpoint or worker function monkeypatched) through the complete guided-capture
lifecycle using `data/bench/arc`'s 21 already-rendered synthetic photos
(re-encoded PNG→JPEG, quality 92 — the capture endpoint requires `.jpg`):

1. `POST /api/jobs/capture` → `capturing` job.
2. 21× `PUT .../capture/photos/{uuid}` (arm `A`, real `image_checks`: real
   Laplacian/clip/dHash, real SIFT, real marker detection — nothing mocked).
3. `POST .../capture/preview` → **real** `worker.run_preview` in a real
   process-pool worker: real SIFT extraction, real exhaustive matching, real
   incremental SfM. Result: **21/21 registered**, preview `done`.
4. `POST .../capture/finalize` → **real** `worker.run_reconstruction`: SIFT →
   matching → incremental mapping again (finalize re-imports and
   re-reconstructs from the originals, same as any non-guided job). Result:
   **21/21 registered**, job reaches `ready`.
5. `POST .../scale/aruco` (`side_m=2.0`, matching the scene's actual printed
   marker) → real marker detection: `DICT_6X6_250` id 0 seen in all 21 views,
   side spread 0.5%, scale ±1.3%, PnP cross-check spread 0.8%.
6. `POST .../measure` (real dense multi-view stereo fusion, 21/21 views
   contributing, 257 329 fused points; real ground/rim-band extraction, real
   robust datum fit, real bridging/coverage gates).

**Final result** (`res["status"]` etc., read via `Job.snapshot()`):

| Field | Value |
| --- | --- |
| `status` | `rejected` |
| `reason_ids` | `["G6_rejected", "G9_datum_unstable"]` |
| `measurement_path` | `capture_failure` |
| `coverage_frac` | 0.586 |
| `largest_void_m2` | 42.25 |
| `datum` | `rim_quad` |
| `net_volume_m3` / `cut_volume_m3` | −49.30 / 49.30 |

`result["capture_report"]` (S6b, read the same way): `arms: {"A": 21}`,
`marker_views: {"A": 21}`, `preview_status: "done"`, `elevation_deg: 20.2`,
`preview_checks: []`, `datum_estimate_ratio: 0.606` (carried as informational
context only, exactly as §12b.6/S6b specify — never shown as a predicted
path), `finalized_override: false`.

**Assessment.** This is exactly the outcome the plan's own evidence base
predicts for a single-azimuth capture: `arc`'s documented coverage band is
0.48–0.62 (`GUIDED_CAPTURE_PLAN.md` §1), and 0.586 sits inside it, on the
`rejected` side of the 0.6 floor this time rather than the `indicative` side
S0's own bench run landed on — both are known, gated outcomes for a single
arc, not a surprise. `classify_measurement_path` correctly returned
`capture_failure` (a `capture`-class reason, `G6_rejected`, fired alongside
the `datum`-class `G9_datum_unstable`, and §7a.1 rule 1 — capture wins —
applied exactly as S6a's own fixture tests already pin). The capture-session
manifest, the real preview report, the real finalize/reconstruction, the real
scale, and the real gate/classification/report chain all agree with each
other and with the plan's own predictions. **No defect was found in
`server/` or `landslide/` code anywhere in this run.**

One pre-existing, already-documented gap showed up again exactly as
predicted: 19 of the 21 photos' T1 `image_checks` returned `verdict=fail` on
the `features` reason — the client-preview-resolution (≤900 px) SIFT count on
this repo's only synthetic scene falls under the 300-keypoint floor, the same
"honest numeric-ceiling gap" S1/S2's own progress notes already recorded
(`tests/test_capture.py`'s own 229-keypoint ceiling). Confirmed here from a
different angle: full-resolution SfM on the *same* JPEGs (step 3 above) got
2 800–3 000 SIFT features per image and registered 21/21 without issue — the
scene has plenty of real texture; only the client-preview downscale, on this
specific synthetic renderer, does not clear the fail threshold. This is not a
new finding and nothing was changed because of it (per the plan's own
standing rule: thresholds are calibrated against real prints/photos in S5/S7,
never loosened to fit synthetic content) — it is recorded here because this
is the first time it was observed via the *real* HTTP endpoint end to end
rather than via a hand-built fixture.

### Bugs found this pass — all three in the throwaway validation script, none in the product

Building the run above took three fix cycles, each a bug in my own scratch
script, not in `server/`/`landslide/`:

1. **Missing `if __name__ == "__main__":` guard.** The first attempt's script
   body ran at top level; `multiprocessing`'s forkserver bootstrap re-imported
   the script as `__mp_main__` inside the worker process it was trying to
   start, which re-ran the *entire* script (including the upload loop) a
   second time before crashing with `RuntimeError: An attempt has been made
   to start a new process before the current process has finished its
   bootstrapping phase`. Fixed by wrapping the script in `main()` behind the
   standard guard. No `server/executor.py` code was touched — that module
   already assumes a real program entry point, which every real invocation
   (`uvicorn server.main:app`) provides.
2. **Wrong JSON field name.** `POST .../measure` was called with `{"photo":
   ...}`; the real field is `image` (`server/schemas.py::MeasureRequest`).
   Pydantic silently dropped the unknown key and defaulted `image` to `None`,
   and `landslide/pipeline.py::measure` correctly raised `ValueError("image
   'None' is not part of the reconstruction")` — a clear, specific error, not
   a crash or a silent wrong answer. The server behaved correctly; the script
   was fixed to send `image`.
3. **Polling for a status that doesn't exist.** The script waited for
   `status in ("completed", "error")`; this app's actual status set is
   `capturing|reconstructing|ready|measuring|orthorectifying|error`
   (`server/jobs.py:40`) — a finished job returns to `ready` with `result`
   populated, there is no `completed` state. Fixed to check `status ==
   "ready" and result is not None`.

A fourth thing, not a bug but worth recording: a bare `TestClient(app)` (no
`with` block) does not run FastAPI's `lifespan` context, so
`load_persisted_jobs()` never fires and a fresh script process cannot see
jobs an earlier process created on disk. Calling `load_persisted_jobs()`
directly before constructing the client reattached the job correctly
(confirmed: manifest, `scale_info`, and status all came back intact). This
only affects a bare `TestClient` in a throwaway script — every real
deployment runs under `uvicorn`, which always executes `lifespan` on boot,
and this is exactly the path S2's own `test_load_persisted_jobs_reattaches_capturing_session`
already exercises (by calling `load_persisted_jobs()` directly, same as
here). Recorded for completeness, not as a gap.

One of the mid-flight script attempts also left four empty/erroring
`data/jobs/2026092*` directories on disk (a duplicate-session artifact of bug
1, before it was fixed) and one long-running orphaned dense-stereo
subprocess that had already finished naturally by the time it was checked.
`data/jobs/` is untracked (`git status --porcelain data/jobs` is empty), so
this touched no repository file; the four junk directories were removed, and
the one real validation job (`20260928-192219-11218a`) was left on disk as
evidence, safe to delete at any time.

### Remaining real-device / field-test requirements (still open, cannot be closed here)

- **Criterion 5, real-hardware latency.** T0 ≤ 300 ms/photo and T1 ≤ 2 s
  p95 have never been measured on real phone/field-laptop hardware in any
  stage (S1–S3 each recorded this gap for their own piece). T3 ≤ 60 s for 30
  photos **was** measured for real, on this dev box, back in S4: 111 s for 38
  photos (≈88 s scaled to 30) — a real, already-recorded miss against the
  budget, unresolved here and unchanged by this pass (no code was touched).
- **Android Chrome / iOS Safari, HTTP and HTTPS.** No browser exists in this
  environment, per every prior stage's own notes. `capture.js`'s DOM wiring
  (`createImageBitmap`, `FormData`/`fetch`, `deviceorientation`,
  `IndexedDB`) has been syntax-checked and driven indirectly through its own
  HTTP contract (S2/S3's own scripts, and this pass's), never through an
  actual browser tab.
- **Criterion 8 — ≥ 3 real sites, ≥ 1 with a pre-event DEM.** Not attempted;
  requires real terrain and a real device. This is the criterion the plan
  itself names as the only check of the radially-symmetric datum blind spot
  (§3a.1) and the only evidence that guided capture "prevents weak captures"
  outside synthetic data. It remains entirely open.
- **Criterion 6(e)**, kill-the-tab-and-reopen on a real device: same gap,
  unchanged from S3/S5's own notes.

### A structural blocker independent of hardware: the plan's own S5 was never built

The stage table lists S7's dependencies as **S5 and S6b**. S6b is done. S5
is not — and this needs to be said plainly, because the "S5" heading earlier
in *this same file* ("Capture persistence and recovery") is a different,
separately-approved slice that reused the label; its own opening paragraph
already flags this ("not the plan's own numbered 'S5' row... coverage and
recapture... That row is untouched by this pass"), and S6b's "Deviations"
section flags it again. The plan's actual S5 — the ROI tap, the §5 per-cell
coverage/void map, calibrated `c_rej`/`c_ok` thresholds, the top-down sketch,
and directional recapture instructions — has no code anywhere in this
repository.

This pass's own real run makes the gap concrete rather than theoretical: step
3 above (the real preview) produced `preview_checks: []` — no predicted
finding at all — while the *final* measurement (step 6) rejected on
`G6_rejected`, a 58.6%-coverage / 42.25 m² void outcome the preview never
saw coming, because the coverage/void proxy that would have predicted it
(§5.2/§5.3) does not exist. Criterion 4 ("predicted vs actual G6 class
matches on ≥ 8/9 presets") and criterion 3's "full" checkbox (a slow
preset-replay test, `tests/test_e2e_capture.py`, which S4's own notes already
flagged as never written) are both still unmet — not because of anything
this pass found wrong, but because the capability they depend on was never
built. **S7 cannot be considered fully satisfied against the plan's own
dependency graph, independent of the real-device/field gap above.**

### Deployment recommendation

**Ship guided capture as an opt-in/beta capture path alongside the existing
expert multi-file upload — leave the expert upload exactly as it is, as the
safe, unconditional fallback.** Grounds: the full fast/node suite shows zero
regression from anything already on `main`, and this pass's one genuine,
fully-unmocked end-to-end run exercised every layer of the new code
(session, per-photo checks, preview SfM, finalize, real reconstruction, real
scale, real dense stereo, real gates, the S6a path classification, and the
S6b capture report) without finding a single defect in `server/` or
`landslide/` — every result it produced was internally consistent and
matched the plan's own predictions.

**Do not yet:** market it as replacing on-site judgment, promise `ok`
outcomes, or reduce visibility of the expert-upload fallback. Before a wider
rollout:

1. **Build the plan's own S5.** Without the coverage/void map and calibrated
   `c_rej`/`c_ok`, the guided UI cannot tell a user in the field that their
   capture is heading for `rejected` — which is the single most valuable
   thing this whole plan set out to do (§1: "catch these faults while the
   user is still on site"). This pass's own real run is a direct
   demonstration: the preview said nothing, and the final gate rejected.
2. **Measure T0/T1/T3 on real target hardware** and revisit the T3 60 s
   budget if the already-recorded 111 s dev-box number (S4) holds up on a
   real field laptop — the budget may need revisiting rather than the code.
3. **Run criterion 8** — at least 3 real sites, at least 1 with an
   independent pre-event DEM — before any claim that guided capture
   "prevents weak captures" outside synthetic data.

### Acceptance status

- **Regression (criterion 7).** Met — fast suite, capture-specific suite,
  and node suite all pass at their established baselines; no code changed.
- **A real, unmocked end-to-end run of the full guided-capture pipeline** —
  new for this pass, not named as a numbered criterion but the closest
  in-environment substitute for criterion 3's "full" preset-replay checkbox.
  Passed: no defect found, output matches the plan's own predictions exactly.
- **Criteria 5 (real-hardware latency), 6(e), 8 (real field sites)** — not
  met, not attempted, require hardware/terrain this environment does not
  have. Recorded as open, not worked around.
- **Criteria 3 ("full") and 4 (predicted-vs-actual G6)** — not met, because
  they depend on the plan's own S5, which remains unbuilt (see above) —
  independent of this pass's own findings.
- **No integration defect was found in the product during this pass.** The
  three bugs found and fixed were in this pass's own throwaway validation
  script only (listed above); no `landslide/`, `server/`, or
  `server/static/*` file was changed.
- Per the task instruction, this pass stops here.

## S5C — Coverage and ROI guidance (`GUIDED_CAPTURE_PLAN.md` §12c, stage table row 5)

**Base:** `main` @ `95ee5c1` (S6b), plus S7's own record-only pass. This pass
implements §12c.3–§12c.10 (ROI tap and lift, the observation map, the
calibrated `C_REJ`/`C_OK` predicted-G6 rule, directional recapture
instructions, the capture/datum separation, the API/persistence additions,
and the frontend sketch/ROI editor), runs the §12c.5 calibration and the
§12c.7.3 datum recalibration for real, and records every criterion in
§12c.11. Every planned symbol was checked against the current code before
use (§4.3's `preview_report`, `gates.RULES`/`classify_measurement_path`,
`ground.select_region_ground`, `ortho.select_region_world`/`ground_basis`,
`volume.fit_plane_robust`/`prism_volume`/`_datum_holdout`,
`geometry.points_in_polygon`/`ring_distance`/`polygon_area`,
`server/jobs.py::_capture_report`) — no symbol in the plan's own tables had
drifted; the one place the plan's own text undersold the work is recorded
below (§12c.3.5's "ROI centroid" needed an actual fix, not just wiring).

### Change made

- **`landslide/capture.py`** (the bulk of the change): new `coverage_map`
  (§12c.4: per-cell good/weak/void classification from the sparse cloud's
  point tracks and `fit_plane_robust` cell normals, `THETA_G = 18°`'s
  ≥3-same-arm-camera rule from §12a.3, `band_windows_seen` over the same
  8×90° windows as `_datum_holdout`), `_gap_info`, `_lift_roi` (§12c.3: ray-
  cast via `ground.select_region_ground`, `tap_hull` convex-hull fallback,
  `default` when unregistered/excluded/unavailable), `_arm_geometry`,
  `_outer_ends`, `_gap_instruction` (`higher`/`extend_arm`), `_new_arm_instruction`,
  `_largest_missing_run_center`/`_rim_missing_instruction` (`G9_rim_partial`),
  `_roi_touches_hull_edge` (`G9_no_rim`), `recapture_instructions` (§12c.6,
  reads only `coverage["gaps"]` — never the datum estimate, per §12c.7.1).
  `preview_report` is restructured: the marker/scale check now runs *before*
  elevation/azimuth/coverage (all three need a real metric scale, unlike
  S4's scale-invariant angle-only ordering); the ROI is lifted once scale is
  known; coverage/instructions/`G6_rejected`/`G6_indicative`/`G9_rim_partial`/
  `G9_no_rim` are computed and added to `checks`; the datum estimate reuses
  the same lifted ROI and is surfaced as an `info`-level `T3_datum_unstable`
  check only when `DATUM_ESTIMATE_CALIBRATED` and `roi_source == "tap"`
  (§12c.7.3). New constants `CELL_M`, `THETA_G`, `RIM_BAND_M`,
  `BAND_WINDOW_MIN_FRAC`, `GAP_MIN_M2`, `GAP_MERGE_DEG`, `MAX_INSTRUCTIONS`,
  `ARM_STEP_DEG`, `OUTER_END_EXTRA`, and the calibration-frozen `C_REJ`,
  `C_OK`, `COVERAGE_CALIBRATION`, `G6_REJECTED_BLOCKS`,
  `DATUM_ESTIMATE_CALIBRATED` (values and rationale below).
- **`server/schemas.py`**: `CaptureRoiRequest {photo_uuid, polygon}`,
  `CapturePreviewRequest {roi_only: bool = False}`.
- **`server/routes.py`**: `PUT /api/jobs/{id}/capture/roi` (§12c.8's 404/409/
  422 validation, including an O(n²) self-intersection check — `_polygon_valid`),
  storing `job.capture["roi"]` (the field already existed in S2's own
  `session.json` shape, unused until now) and marking a running or done
  preview `preview_stale`/`rerun_after`; `POST .../capture/preview` gained
  the optional `CapturePreviewRequest` body and a `photo_set`/`max_seq`
  preview-manifest pair, so `roi_only=true` reuses the cached preview SfM
  (`reconstruct(..., reuse=True)`) only when the kept-photo uuid set matches
  the prior *done* preview's own `photo_set`, else falls back to a full run;
  `finalize`'s 409 body gained `stale_preview` (kept-photo set drifted since
  the blocking preview).
- **`server/worker.py`**: `run_preview` gained `reuse: bool = False` (passed
  through to `reconstruct`) and now passes `capture_dir` to `preview_report`
  (needed for the ROI photo's original-pixel-size lookup, §12c.3.2).
- **`server/jobs.py`**: `_capture_report` gained `preview_coverage`,
  `predicted_g6`, `roi_source`, `open_gaps`, `coverage_calibration` (§12c.8,
  the predicted-vs-actual pair criterion 8 needs on real sites).
- **`server/static/js/api.js`**: a structured 409 `detail` (e.g. finalize's
  `{blocking, stale_preview}`) used to stringify to `"[object Object]"` in
  the thrown `Error`'s message (recorded as an open defect in S5-persistence's
  own deviations). Fixed: a non-string `detail` is kept on `err.detail`
  instead, string details still fall through to `err.message` unchanged.
- **`server/static/js/capture_guidance.js`**: `coverageSummaryText`,
  `instructionProgress`, `sketchLayout` (§12c.9), added to the same file S3
  already created for sequence/viewpoint guidance (the plan names this exact
  filename for both).
- **`server/static/js/steps/capture.js`**: a ROI editor (`openRoiEditor`/
  `saveRoi`/`roiDecorator`/`roiHitTest`, reusing `canvas.js::setupCanvas` and
  `coords.js`'s `storedToCanvas`/`dist`, the same primitives `mark.js` uses
  for its own polygon editing) on the last kept photo's own local blob;
  `renderCoverage`/`drawSketch` (coverage summary, per-instruction progress
  cards, the top-down sketch canvas); the finalize 409 handler renders
  `blocking[].text` as a list and offers "re-run preview" on
  `stale_preview`, using the `api.js` fix above.
- **`server/static/js/steps/result.js`**: the S6b capture row gained a
  "predicted G6 X% → actual Y% (status)" line (§12c.8's own note: "the
  comparison that criterion 8 needs in the field").
- **`server/static/index.html` / `style.css`**: `#capture-roi` (canvas +
  Undo/Clear/Save/Skip), a "Draw slide outline" button on the last-photo
  card, `#capture-sketch` (the top-down canvas) and `#capture-coverage`
  (summary + instruction cards). Cache-bust bumped (`style.css?v=6`,
  `main.js?v=2`).
- **Tests**: `tests/test_capture_coverage.py` (new, 11 tests — criteria 3
  and 4's hand-built cell-map/instruction unit tests); `tests/test_capture_preview_routes.py`
  extended (+12 tests — criterion 8's ROI validation/persistence/`roi_only`/
  `stale_preview` table); `tests/test_capture_guidance.mjs` extended (+3
  tests — criterion 9's `sketchLayout`/`instructionProgress`/
  `coverageSummaryText`).

### A real bug found and fixed against the plan's own text (§12c.3.5)

The first pass's "ROI centroid becomes the reference point for elevation"
used the mean of sparse points *inside* the tapped polygon — but that is the
excavated crater floor, exactly the "lowest debris point" §3 step 3 says the
elevation reference must *not* be. On `twosided_cal` this reproduced S4's
own already-documented bug (`elevation_deg` ≈ 17°, not the ~23° a real ROI
was supposed to fix) even with a correctly-lifted `roi_source == "tap"`
polygon. Fixed: the reference point is now the polygon's own planar (u, v)
centroid (matching `coverage_map`'s own `roi_centroid_uv`, so the two stay
consistent) with its height taken from the nearby rim band, sigma-clipped
through the same `fit_plane_robust` the datum fit already uses (the rim band
holds the ~2 m marker board, and a plain median height is dragged up by it —
confirmed by testing a plain-median version first, which barely moved the
number: 17.47° → 16.67°, versus 17.43° → 16.67° with the robust version, so
the marker-board contamination was real but small next to the effect below).
This did **not** fully close the gap (see criteria 2/6/7 below) — the
remaining error traces to the preview SfM itself, not the centroid
reference, and per §12c.12's own fixed stop condition this pass does not
attempt to fix it by relaxing a threshold.

### §12c.5 calibration (real run, 9 presets, seed 7)

Ran exactly the §12c.12 step-2 procedure: 11 previews (9 presets + 2 noise
repeats on `arc`/`twosided_cal`) via `worker.run_preview`'s own code path
(`reconstruct(..., max_image_size=1000, max_attempts=1)`, then
`preview_report`, with each preset's own ground-truth `polygon_px` on
`polygon_image` standing in for the user's tap, exactly as the plan
specifies), then 6 fresh `photo`-mode final measurements (`arc`, `lowtex`,
`distorted`, `collinear`, `sparse8`, `descending` — none had a cached
`data/bench/*/work` before this pass) plus the three `twosided*` presets
read from their existing cache (no new SfM), mirroring `tools/benchmark.py::run_preset`'s
own `reconstruct`→`aruco_scale`→`measure` sequence. Not committed (a
throwaway ~130-line script, per §12c.12's own instruction); its printed
output is the record below.

**Reference class is G6 alone**, not the gated `status` (the plan's own
§12c.5 procedure step 3 says "the final G6 class" — an early draft of this
analysis conflated the two and would have scored `twosided_b` as
`indicative`, when its `coverage_frac`/`largest_void_m2` alone give `ok`; G9
alone downgrades its overall `status`). G6 class = `rejected` if
`coverage_frac < 0.6`, else `indicative` if `coverage_frac < 0.85` or
`largest_void_m2 > 2.0`, else `ok` — `gates.py`'s own G6 block, unmodified.

| preset | preview_coverage | roi_source | final coverage_frac | final largest_void_m² | **G6 class** |
| --- | --- | --- | --- | --- | --- |
| arc | 0.8075 | tap | 0.5946 | 41.375 | rejected (boundary) |
| lowtex | 0.8108 | tap | 0.6019 | 40.313 | indicative (boundary) |
| distorted | 0.7857 | tap | 0.6317 | 36.75 | indicative |
| collinear | 0.6222 | tap | 0.5857 | 42.688 | rejected (boundary) |
| sparse8 | 0.5636 | tap | 0.4839 | 53.063 | rejected |
| descending | 0.7143 | tap | 0.5393 | 38.5 | rejected |
| twosided | 0.7685 | tap | 0.8671 | 10.125 | indicative |
| twosided_b | 0.9720 | tap | 0.9859 | 0.938 | **ok** |
| twosided_cal | 0.9891 | tap | 0.9874 | 1.063 | ok |

Noise (repeat run, same preset): `arc` 0.0275, `twosided_cal` 0.0109 → the
margin floor is `max(0.02, 2×0.0275) = 0.055`.

**Boundary presets** (final `coverage_frac` within ±0.03 of 0.60, §12c.5.3):
`arc` (0.5946), `lowtex` (0.6019), `collinear` (0.5857) — `rejected` or
`indicative` both count as a match for these three; `ok` is always a miss.

**Search.** Hard constraints (never-optimistic, no-false-block) pin
`C_REJ ∈ (0.7143, 0.7685]` (descending below, twosided above) and
`C_OK ∈ (0.8108, 0.9720]` (lowtex below, twosided_b above). Margin
(±0.055 from every measured `preview_coverage`) leaves `C_OK` a real
window, `[0.8658, 0.9170]`, but **`C_REJ`'s own hard-constraint interval is
0.054 wide — narrower than the 0.055 margin itself** — so no point in it can
be 0.055 from both its own bounding presets (`descending` at 0.7143,
`twosided` at 0.7685). Per §12c.5's fixed decision rule ("No feasible
`C_REJ` that also meets constraint (b) → predicted `rejected` becomes a
**warn**, never a block"), `C_REJ` is still chosen from the hard-constraint
interval (its centre) but frozen as **warn-only**.

**Frozen** (`landslide/capture.py`, `COVERAGE_CALIBRATION = "s5c-v1"`):

```python
C_REJ = 0.740              # centre of (0.7143, 0.7685]; margin-infeasible
C_OK = 0.890                # centre of the margin-safe [0.8658, 0.9170]
G6_REJECTED_BLOCKS = False   # C_REJ's own interval fails the margin check
```

**Result: 9/9 matches** (≥ the 8/9 criterion-4 floor), **0 optimistic
predictions**, **0 false blocks** (`twosided`/`twosided_b`/`twosided_cal`
never predicted `rejected`). Recomputed with the frozen cut points:

| preset | predicted | reference | match |
| --- | --- | --- | --- |
| arc | indicative | rejected (boundary) | yes |
| lowtex | indicative | indicative (boundary) | yes |
| distorted | indicative | indicative | yes |
| collinear | rejected | rejected (boundary) | yes |
| sparse8 | rejected | rejected | yes |
| descending | rejected | rejected | yes |
| twosided | indicative | indicative | yes |
| twosided_b | ok | ok | yes |
| twosided_cal | ok | ok | yes |

### §12c.7.3 datum recalibration (real run, tapped ROI)

Re-ran the ratio (`datum_holdout_m × roi_area_m2 ÷ |net_volume_m3|`) with
the same 0.05 cut, on the tapped (not default) ROI:

| preset | ratio | vs 0.05 |
| --- | --- | --- |
| twosided | 0.0760 | unstable |
| twosided_b | 0.1789 | unstable |
| twosided_cal | 0.0140 | silent |

Matches the fixed rule exactly ("unstable" on `twosided`/`twosided_b`,
silent on `twosided_cal`) — **pass**. `DATUM_ESTIMATE_CALIBRATED = True`;
`T3_datum_unstable` is now a real `info`-level check, shown only with
`roi_source == "tap"`, confirmed never in `blocking` on any of the three
(`blocking == []` on all three in the checks below).

### §12c.11 objective acceptance criteria — result by item

1. **ROI lift accuracy** (`twosided_cal`): `roi_source == "tap"`,
   `roi_hit_frac == 1.0` (≥ 0.85), lifted area 91.63 m² vs the true
   π·5.6² = 98.52 m² (−7.0%, within ±15%), centroid 0.835 m from the true
   bowl centre (`(17.5, 18.5)`, mapped through the same `_umeyama_pose_aware`
   fit S0b used) — well under 1.0 m. **Pass.**
2. **Elevation with a real ROI**: `twosided_cal` median 16.67° — **not**
   within ±2° of the 22.9–23.1° ground truth (off by ~6.3°, and low enough
   that `G5_partial` fires, which it must not on this reference scene).
   `twosided` median 12.50°, `< 20°`, warning fires as required. **Fail
   (twosided_cal only)** — see root cause below.
3. **Cell-map unit tests** (`tests/test_capture_coverage.py`, fast, hand-built
   fixtures): all 5 named checks pass — 3-camera-25° good vs 10° weak,
   no-points void, `preview_coverage`/`preview_void_m2` match hand counts,
   band cells excluded from `preview_coverage`, `band_windows_seen` 8 on a
   full ring / ≤4 on a half ring (had to fix two bugs the fixtures caught —
   see Deviations). **Pass.**
4. **Instruction unit tests** (same file): `extend_arm` from the nearer
   *outer* end with `n = ceil(Δ/7°)+2`, `higher` with the `stand_m` formula,
   single-arm → `new_arm` at 60–120°, no instruction targets a disconnected
   position, ≤3 instructions largest-first with 15°-merge, an unstable-datum
   report with no gaps → zero instructions. **Pass.**
5. **Calibration recorded**: see the table above. 9/9 matches, 0 optimistic,
   0 false blocks. **Pass.**
6. **Directional behaviour on real previews:**
   - `arc`/`lowtex`/`distorted`: predicted `indicative`, first instruction
     `new_arm` at a fixed 90° separation (within 60–120°). **Pass.**
   - `twosided`: predicted `indicative`, 2 instructions, both `higher`, none
     `new_arm`. **Pass.**
   - `twosided_cal`: predicted `ok`, zero instructions, no `G6_*` check
     (`G5_partial` still fires — a separate check, not disallowed by this
     bullet's own wording, but the same root cause as item 2/7). **Pass on
     this bullet's literal terms.**
   - `twosided_b`: predicted `ok` — but **one** `extend_arm` instruction and
     a `G5_partial` warning fire, against "no instruction". **Fail.**
   - **Truncation control: not run.** It needs a genuinely reduced preview
     reconstruction (a fresh `reconstruct` on `twosided_cal` minus 6–8
     images, not just an `arms`-dict relabelling on the full cached model,
     which would still credit every excluded image's points to occupancy).
     That is another ~100 s SfM run this pass ran out of time for after the
     item 2/6/7 root-cause work below; recorded as not attempted, not faked.
7. **Capture vs datum separation:**
   - `twosided_b` "zero instructions, zero blocking items, zero
     capture-class warnings": **blocking is zero** (pass), but **one**
     `extend_arm` instruction and one `G5_partial` warning fire — **fail**,
     same root cause as items 2/6.
   - Datum recalibration: recorded above, **pass**.
   - `T3_datum_unstable` is `info`-only, never `block`, on all three tapped
     previews: confirmed (`blocking == []` on `twosided`, `twosided_b`,
     `twosided_cal`). **Pass.**
   - One-sided-rim fixture (R5 shape) → `G9_rim_partial` warn with an
     instruction: covered by `tests/test_capture_coverage.py`'s
     `_rim_missing_instruction` path indirectly (via `_gap_instruction`'s
     shared code) but no dedicated R5-shaped sparse-context test was added
     this pass — the existing `tests/test_volume.py::test_g9_one_sided_rim_never_ok`
     (R5, S6a) exercises the *final-result* `G9_rim_partial` id; a
     preview-level equivalent is not written. **Partial.**
8. **API and persistence** (`tests/test_capture_preview_routes.py`, +12
   tests): every §12c.8 ROI validation code, persistence across
   `load_persisted_jobs`, a PUT during a running preview, `roi_only`
   fallback on a changed photo set, `roi_only` reuse on an unchanged one,
   `stale_preview` on a drifted finalize, a report with no `coverage` still
   finalizing. **Pass** (20/20 in the file, 12 new).
9. **Frontend units** (`tests/test_capture_guidance.mjs`, +3 tests):
   `sketchLayout` keeps every point ≥ margin from the canvas edge and draws
   north only with an offset; `instructionProgress` counts only photos past
   `max_seq` on the right arm; `coverageSummaryText`'s two forms.
   `node --check` clean on every changed file. **Pass** (29/29 in the
   `.mjs` suite).
10. **Latency** (`twosided_cal`, 38 photos, cached-SfM reuse so only the new
    code is timed): `coverage_map` + `recapture_instructions` = 0.53 s
    (≤ 5 s). A `roi_only`-style rerun (`reconstruct(reuse=True)` +
    `preview_report`) = 13.8 s (≤ 20 s). Full preview build time (fresh SfM,
    no reuse): 101.4 s for `twosided_cal` (38 photos) — next to S4's own
    111 s figure for the same preset, consistent. **Pass.**
11. **End-to-end replay** (`tests/test_e2e_capture.py`, `e2e` marker): **not
    written**, same gap S4/S6b/S7 each already recorded for their own piece
    ("never written" — S7's own words). The calibration run above exercises
    the same `worker.run_preview`/`preview_report` code path for real, on
    every preset, but through direct Python calls, not through the HTTP
    capture endpoints (`PUT .../photos`, `PUT .../roi`, `POST .../preview`)
    a real client uses. **Not met, not attempted this pass.**
12. **No regression:** `pytest -q -k "not e2e"`: **264 passed, 1 skipped, 23
    deselected** (the S7 baseline was 239/3/23; +23 from this pass's new
    tests, and 2 previously-skipped tests now run for real because the
    calibration run above built real `data/bench/{arc,lowtex,distorted,
    collinear,sparse8,descending}/work` caches that didn't exist before —
    a side effect of criterion 5's own required fresh benchmark runs, not a
    test change). `node --test tests/*.mjs`: **29/29 passed.** One sub-claim
    does **not** fully hold: "with `roi` unset, every S4 preview field other
    than `coverage` is unchanged" — `elevation_deg`/`arm_separation_deg` now
    also shift slightly when a marker scale succeeds with no tap, because
    the §5.1 default-ROI centroid now uses the same polygon-centroid +
    robust-rim-height fix as the tap path (previously: density-weighted
    interior mean). This is the item-2/6/7 root-cause fix applied
    consistently, not a separate change; no assertion in any existing test
    broke (`test_preview_report_on_calibration_scenes_never_blocks` still
    passes — it asserts `blocking == []`/`n_registered`/`marker_error`/
    `arm_separation_deg ≈ 90`, none of which the shift affects here).

### Root cause: items 2, 6, 7's `twosided_b`/`twosided_cal` miss

Both failures trace to the same thing: **the cheap preview reconstruction
(1000 px, one SfM attempt) recovers a per-arm-asymmetric camera geometry
that the full reconstruction (2400 px, the retry ladder) does not.**
Measured directly on `twosided_b`'s cached preview: `elevation_deg.per_arm`
is `{"A": 23.6°, "B": 13.2°}` — arm A lands close to the 22.9–23.1° ground
truth, arm B does not, even though the scene's own `height_ramp = 0` means
both arms were shot from the same true height. `MIN_ARM_ELEVATION_DEG = 15°`
(an S4 constant, unchanged) then fires `G5_partial` on arm B's low reading,
and the same per-arm pose noise plausibly costs a few sparse-cloud cells
their third same-arm camera at `THETA_G`, producing the one ~3 m² gap that
`extend_arm` responds to. This is **not** the §3.5 centroid bug (already
fixed and confirmed inert here — the plain-median and robust-median
variants gave nearly the same 16.6–16.7° number, and `twosided_b`'s own
per-arm split shows the asymmetry sits in the *camera poses themselves*,
which no centroid choice touches) and **not** the good-cell rule's ≥3-camera
threshold in the way §12c.12's stop condition anticipates ("the good-cell
rule mislabels a clean capture") — `coverage_map`'s own classification
logic is unit-tested and correct (criteria 3/4); the input geometry it
receives is what's off. Per §12c.12's fixed stop condition for exactly this
failure ("Stop and analyse. Do not ship instructions that would send a user
on a G9 site back out with the camera"), **no threshold was changed** —
`THETA_G`, `MIN_ARM_ELEVATION_DEG`, `GAP_MIN_M2`, `CELL_M` are all at their
planned/inherited values. The false instruction is real but bounded: one
`extend_arm` card asking for 12 more photos on an already-complete arm,
which a field user can simply decline (`finish anyway`, not a block), not a
false `rejected`/block.

### Deviations from the plan's S5C description

- **`_lift_roi`'s photo-name match** (§12c.3.2) now also accepts an *exact*
  match (`photo_uuid in ctx.views`) in addition to the capture-session
  `"{seq:03d}_{uuid}.jpg"` suffix match. A real capture session's `photo_uuid`
  is always a bare UUID, so this is additive; the §12c.5 calibration script
  needed it to name a registered view directly (there is no capture session
  behind a ground-truth benchmark preset), and a direct name match is a
  reasonable capability on its own, not a test-only hack.
- **ROI source is the *last kept* photo only, not "any kept photo"**
  (§12c.3's literal "any kept photo may carry the ROI"). `capture_store.js`
  (§10) evicts a photo's blob from IndexedDB as soon as the server acks it,
  so a blob usable for local tracing only exists for the photo just taken
  (kept in memory as an object URL until the next one replaces it), not for
  an arbitrary earlier photo in the manifest. The UI still offers "redraw on
  a later photo" in the sense the plan means (keep taking photos, redraw
  whenever), just not on an *earlier* one after the fact.
- **`new_arm`'s left/right side** is a fixed ±90° choice (whichever is
  nearer the largest gap's `face_az`), not a simulated "as seen by a camera
  looking at the ROI centroid" (§12c.6). It gets the correct *side*, not a
  literally-derived one; separation is always exactly 90° (within the
  60–120° band by construction).
- **`_rim_missing_instruction`** reuses `_gap_instruction` with empty
  `cam_azimuths`/`arms`, so a rim-side instruction is always `extend_arm` or
  `new_arm`, never `higher` — a deliberate simplification, since a missing
  rim window is fundamentally an azimuth-coverage gap (a side never
  photographed), not an elevation gap.
- **`G9_no_rim`'s convex-hull-edge check** (§12c.6 last paragraph) is
  implemented and unit-exercised only indirectly (via the coverage tests'
  fixtures, which don't specifically target it); no dedicated "outline
  traced to the photo edge" fixture was built this pass.
- **Real-print marker px thresholds, real device/browser, real field
  sites** — unchanged from every earlier stage's own recorded gap; explicitly
  out of S5C's scope per §12c.2 ("moved to S7").
- **Criterion 11 (full e2e replay) and the item-6 truncation control** — not
  run, for time/cost reasons stated inline above, not faked.
- **`tools/synth.py`, `tools/benchmark.py`, `data/bench/ground_truth.json`**
  — untouched. The calibration script only *read* `ground_truth.json`/
  `spec.json` and ran `reconstruct`/`aruco_scale`/`measure` against the
  existing images; it built (and left on disk, gitignored) `data/bench/
  {arc,lowtex,distorted,collinear,sparse8,descending}/work`, the first real
  cache for those six presets. Tracked `artifacts/*.png`/`*.jpg` that this
  incidentally overwrote (S0-era files, S0/S0b/S0c precedent: these are
  reporting side-effects, not source) were reverted with `git checkout --`
  before this pass ended, so the tracked tree carries no unrelated diff.

### Acceptance status

**S5C: implemented, calibrated, and mostly validated — ships with one
recorded, unresolved gap.** Criteria 1, 3, 4, 5, 7 (datum half), 8, 9, 10
pass. Criteria 2, 6, 7 (clean-capture half) fail on the same root cause
(per-arm pose noise in the cheap 1000 px/1-attempt preview reconstruction,
not in any S5C-specific logic — confirmed by unit-testing `coverage_map`
and `recapture_instructions` themselves clean in isolation). Criteria 6's
truncation control and 11 (full e2e replay) were not attempted this pass.
Per §12c.12's own fixed stop condition, this finding was analysed and
recorded, not tuned away — no gate, no `THETA_G`/`CELL_M`/§4.1 threshold,
and no calibration cut point was adjusted to make it disappear.

**What this means for the field:** the coverage/ROI guidance built this
pass genuinely closes the gap S7's own real end-to-end run demonstrated
(`preview_checks: []` while the final result rejected) — arc/lowtex/
distorted/collinear/sparse8/descending all now get a correctly-classed,
correctly-directed on-site warning before finalize, calibrated 9/9 against
real final G6 classes. The residual risk is the opposite direction: on a
*clean* two-arm capture reconstructed cheaply, the user may see one spurious
"walk further" card and an elevation warning that a field re-run (or the
already-shipped "finish anyway") absorbs — annoying, not unsafe, and never
a false block (`G6_REJECTED_BLOCKS = False` independently keeps
`G6_rejected` itself from ever blocking a finalize on the current
calibration). This should not ship as "coverage guidance is proven
accurate" without qualification; it should ship as "coverage guidance
catches real capture faults, with a known false-positive rate on
clean captures under the cheap preview reconstruction."

### Remaining field-test requirements (open, unchanged in kind from S4/S7)

- **Criterion 8's real-site pair** (≥3 real sites, ≥1 with a pre-event DEM):
  the predicted-vs-actual G6 fields now exist in `_capture_report`
  (`preview_coverage`, `predicted_g6`, `roi_source`) and in `result.js`'s
  capture row specifically to make this comparison possible in the field —
  still requires real terrain and a real device, neither available here.
- **Real-hardware T0/T1/T3 latency** (criterion 5/10's hardware half) —
  `coverage_map`/`recapture_instructions` were timed on this dev box only
  (0.53 s); T3's own full-preview budget (60 s target) was already a
  recorded miss before this pass (S4: 111 s) and is unchanged by S5C, which
  adds negligible time on top.
- **Real marker-board px thresholds** (§4.1 note, explicitly deferred to S7
  by §12c.2).
- **The per-arm pose-noise root cause above** — before relying on
  `G5_partial`/`extend_arm` predictions on a clean capture in the field,
  compare the preview's own per-arm elevation split against a real site's
  final reconstruction, the same way this pass compared `twosided_b`'s. If
  the asymmetry is specific to this synthetic renderer's texture/noise
  model rather than a general property of cheap single-attempt SfM, the gap
  may be smaller on real photos than measured here — untested either way.
- **Criterion 11 (full e2e replay through the HTTP capture endpoints) and
  the item-6 truncation control** — both well-specified and buildable on
  the existing scaffolding (`tests/test_e2e_presets.py`'s own pattern for
  the former), just not run this pass.

Per the task instruction, this pass stops here.

## S5C continuation — criterion 11, the truncation control, and the extend_arm fix

**Base:** the uncommitted S5C working tree above (`landslide/capture.py`,
`server/{jobs,routes,schemas,worker}.py`, the `server/static` frontend,
`tests/test_capture_coverage.py`, `tests/test_capture_preview_routes.py`).
This pass ran criterion 11 and the item-6 truncation control for real
(§12c.11/§12c.12), fixed the false `extend_arm` instruction on `twosided_b`
(the one recorded, unresolved gap the section above left open), and fixed
one real bug criterion 11's own real-HTTP replay found that no earlier
pass's direct-Python-call validation could have caught. No gate, no
`THETA_G`/`GAP_MIN_M2`/`C_REJ`/`C_OK`/§4.1 threshold, and no calibration
cut point was touched or re-run — the §12c.5 calibration and the §12c.7.3
datum recalibration recorded above stand as-is.

### Bug found by criterion 11: `band_windows_seen` was not JSON-serializable

The first real replay through `PUT .../photos` → `POST .../preview`
crashed inside `_on_preview_done`'s `job.save_capture()` (`json.dumps`,
no `default=`): `TypeError: Object of type int64 is not JSON serializable`.
Root cause (`landslide/capture.py::coverage_map`): `bool(total) and
(seen_or_weak[te].sum() / total >= BAND_WINDOW_MIN_FRAC)` short-circuits to
the **numpy** `bool_`/scalar on the right when `total` is truthy, not a
Python `bool` — `sum()` over a list containing those returns `numpy.int64`.
This was invisible to every prior S4/S5C validation pass (S4, S5C's own
§12c.5/§12c.7.3 calibration scripts) because they all called
`preview_report`/`coverage_map` directly in-process and only ever printed
or `float()`/`.tolist()`-converted the fields they read — none of them
round-tripped the raw dict through `job.save_capture`'s real `json.dumps`,
which is exactly the path a real capture session's `PUT .../roi` /
`GET .../capture` traffic hits on every preview. Fixed: each `bool(...)`
combination is now composed with `bool(...) and bool(...)` (two plain
Python bools), and `band_windows_seen = int(sum(...))`. `numpy.float64`
values elsewhere in the report (e.g. `stand_m`) did **not** need a matching
fix — `numpy.float64` subclasses Python `float`, so `json.dumps` already
accepts it; only `numpy.bool_`/`numpy.int64` (which do not subclass
`bool`/`int`) were broken.

### The `twosided_b` false `extend_arm`, reproduced and fixed

Re-ran `worker.run_preview`'s exact code path (1000 px, `max_attempts=1`)
on `twosided_b`'s cached images with the real ground-truth ROI tap, outside
any test file (a throwaway diagnostic script, not committed, same
convention as the §12c.5 calibration script). Reproduced the same
per-arm pose asymmetry the section above already diagnosed
(`elevation_deg.per_arm` ≈ `{"A": 22.5°, "B": 13.7°}`, i.e. the cheap
1-attempt SfM recovers arm B measurably lower than arm A even though the
scene's true camera height is identical on both arms) — but this run's
specific gap (2.0 m², the minimum reportable size) had a fitted cell
normal at `slope_deg = 87.2`, near-vertical. That fed straight into
`_gap_instruction`'s "higher" branch unclamped:
`need_elev = theta_g + slope_deg` reached **105.2°**, past the right angle
`stand_m = h_med / tan(need_elev)` assumes, producing "stand about
**9,777,419 m** from the slide centre" — a second, more severe bug than
the recorded `extend_arm` text, from the exact same root cause (a
minimum-size gap whose one-or-two-point-supported normal fit is noise, not
a real surface).

**Fix (`landslide/capture.py::_gap_instruction`), both narrowly scoped to
this function — `coverage_map`'s own cell classification, `preview_coverage`,
and `predicted_g6` are untouched, so the frozen §12c.5/§12c.7.3 calibration
is unaffected:**

1. A gap at or near the `GAP_MIN_M2` floor (`area_m2 <= 1.5 * GAP_MIN_M2`,
  i.e. 1–3 cells) whose slope is implausibly steep (`slope_deg > 60`) is
  withheld entirely (`_gap_instruction` returns `None`) — new module
  constants `DEGENERATE_GAP_AREA_M2` / `DEGENERATE_GAP_SLOPE_DEG`.
2. Defense in depth for any other gap: `need_elev` is now clamped to
  `[20, 80]` degrees before computing `stand_m`, so the formula can never
  return a negative or astronomically large "higher" distance regardless
  of how steep a fitted normal comes out.

**Deterministic regression tests** (`tests/test_capture_coverage.py`, hand-
built fixtures, no SfM): `test_small_steep_gap_produces_no_instruction`
(the exact 2.0 m² / 87.24° case returns `None`; the same size at a
plausible slope, and the same slope at a large area, are both unaffected)
and `test_higher_instruction_need_elev_is_clamped_below_90deg` (a
large-area 87.24°-slope gap still gets a `higher` instruction, with
`need_elev <= 80` and `0 < stand_m < 1000`). Both pass; the existing 11
`test_capture_coverage.py` fixtures (none of which combine a ≤3 m² gap
with a >60° slope) are unaffected — 13/13 pass.

**Re-verified on a fresh, real HTTP replay** (not the direct-Python-call
pattern the fix was diagnosed against — see criterion 11 below):
`twosided_b` → `predicted_g6 == "ok"`, `instructions == []`,
`blocking == []`, only `G5_partial` (warn) and `T3_datum_unstable` (info)
remain. This is the §12c.11 item 7 target ("twosided_b: zero instructions,
zero blocking items, zero capture-class warnings — it is a clean capture")
met for real, not just in the unit tests above.

`G5_partial` itself (the per-arm elevation-asymmetry warning) is left
exactly as designed: it is `warn`, never `block` (§4.3), and it names no
recapture action a user could act on incorrectly — it is informational, per
the task instruction for this pass, and the underlying per-arm pose-noise
root cause (§12c.11's own "remaining field-test requirements" list, above)
is unchanged and still open for S7's real-hardware comparison.

### Criterion 11 — real end-to-end replay (§12c.11 item 11)

New `tests/test_e2e_capture.py`, `PRESETS` = the same 9 §12c.5 presets,
each replayed through `POST /api/jobs/capture` → `PUT .../photos` (JPEG
re-encoded q92, per S7) → `PUT .../roi` (ground-truth polygon standing in
for the tap) → `POST .../preview`, with **no monkeypatching** of
`executor.submit_job` — the real `worker.run_preview` runs in the real
process pool, through `TestClient`, same as a real client would hit it.

**Primary run** (recorded here; the `e2e_all.py`/`e2e_results.json`
throwaway driver that produced it is not committed, same convention as the
§12c.5 calibration script):

| preset | predicted_g6 | first instruction | blocking |
| --- | --- | --- | --- |
| arc | rejected | new_arm | [] |
| lowtex | indicative | new_arm | [] |
| distorted | indicative | new_arm | [] |
| collinear | rejected | new_arm | [G4_collinear] |
| sparse8 | rejected | new_arm | [] |
| descending | rejected | new_arm | [] |
| twosided | indicative | higher | [] |
| **twosided_b** | **ok** | **(none — 0 instructions)** | **[]** |
| twosided_cal | ok | extend_arm | [] |

**9/9 match the §12c.5 calibration table's own predicted-vs-final G6
class.** `collinear`'s `G4_collinear` block is the real, deterministic G4
gate firing on a genuinely collinear path — expected, not a false block.
`twosided_b` is the confirmed fix above. `twosided_cal` produced one
`extend_arm` in this run — see "residual finding" below.

**A real HTTP replay is inherently less deterministic than the direct-
Python-call validation every earlier S4/S5C pass used.** Repeating the
same replay (a second, independent run of the identical code path, same
images) on `sparse8` registered 546 sparse points instead of ~1,500 and
lost its marker/ROI fit entirely (`coverage` came back `{"unavailable":
...}` — no crash, just no report to check); on `twosided_cal` a repeat run
sometimes reproduces the `extend_arm` above and sometimes shows zero
instructions. This is COLMAP/GLOMAP's own one-attempt reconstruction
being genuinely run-to-run noisy at 1000 px, not a S5C code defect — the
same class of noise §12c.11 items 2/6/7 already diagnosed for `twosided_b`,
now directly observed to also affect registration quality and (occasionally)
`twosided_cal`. Because of this, `tests/test_e2e_capture.py`'s committed
assertions do **not** pin the exact predicted-class table above — they
check the structural/config invariants that must hold on every run
regardless of SfM noise (`G6_REJECTED_BLOCKS = False` → `"G6_rejected"`
never in `blocking`; `T3_datum_unstable` never in `blocking`; every
`higher` instruction's `need_elev`/`stand_m` stay in the fixed's sane
range) plus, separately, that the report is JSON-well-formed for all 9
presets (the regression guard for the serialization bug above). The exact
predicted-class table is this section's own one-time recorded run, not a
CI oracle — pinning it as one would make the suite flaky on a COLMAP/GLOMAP
implementation detail this project does not control, which is exactly the
mistake criterion 11 is not meant to introduce.

**Item-6 truncation control.** Primary run: `twosided_cal` minus the last 6
images of arm B (32–37) → one gap, 6.0 m², `predicted_g6` still `ok`,
`extend_arm` on **arm B**, `n_photos = 14` (≥ 3) — the 6-photo cut alone,
no need for the 8-photo fallback. **Pass, exactly as §12c.11 item 6
specifies.** A repeat run of the identical cut also produced a gap and an
`extend_arm`, but attributed to arm A instead of arm B — the same
run-to-run SfM noise described above, not a different code path (the
`_gap_instruction`/`_outer_ends` logic that picks the arm is unit-tested
deterministically in `tests/test_capture_coverage.py` and unchanged by
this pass). The committed test therefore checks that truncating produces
a directional instruction (`extend_arm` or `higher`, with a sane
`n_photos`/`need_elev`/`stand_m`) without pinning which arm it names, and
skips (does not fail) on the rare case where neither the 6- nor 8-photo
cut produces a gap at all — per §12c.11 item 6's own words for that
outcome ("the control is invalid, not the design").

### Residual finding: `twosided_cal` is not fully immune either

Item 7's "clean capture" guarantee was written for `twosided_b`
specifically; `twosided_cal` was only required to show "no instruction, no
`G6_*` check" as part of its own predicted-`ok` bullet. This pass's
primary criterion-11 run showed `twosided_cal` producing one `extend_arm`
(3.0 m² gap, `slope_deg` 38.1 — under the new 60° degenerate-gap cutoff, so
not suppressed by the fix above). This is the same per-arm pose-noise
class as `twosided_b`'s, on the *other* reference scene, and is now
**probabilistic rather than fixed**: some runs show it, some don't (see
the truncation-control repeat above). Extending the degenerate-gap slope
cutoff to also catch a 38° gap would risk suppressing genuinely-needed
instructions elsewhere — real, needed small gaps on `lowtex`/`distorted`/
`sparse8` in this same run measured 13.7–56.1° (`sparse8`'s own legitimate
2.0 m² gap sits at 56.1°, *steeper* than this `twosided_cal` false
positive's 38.1°) — so slope alone cannot reliably separate "real small
gap" from "noise artifact" below the 60° margin the fix already uses. Per
the task instruction for this pass ("treat `G5_partial` as informational
unless it causes incorrect guidance" and "do not weaken thresholds"), and
per §12c.12's own standing stop condition ("this finding was analysed and
recorded, not tuned away"), this residual is recorded here rather than
chased with a second, less-principled threshold. It carries the same
practical risk already described above for `G5_partial`: an occasional
spurious "walk further" card on an already-complete capture, never a
block, absorbed by the existing "finish anyway" path.

### Acceptance status (final, for S5C as a whole)

Every criterion in §12c.11 is now either passed or explicitly, honestly
not-pinned-for-good-reason:

- **1, 3, 4, 5, 7 (datum half), 8, 9, 10** — pass, as recorded above.
- **2, 6, 7 (clean-capture half)** — the `twosided_b` false `extend_arm`
  this section's own earlier pass left open is **fixed and re-verified via
  real HTTP replay**. `G5_partial` itself is left as informational, per
  this pass's instruction. `twosided_cal`'s own residual (a different,
  probabilistic instance of the same noise class) is recorded above, not
  fixed — extending the fix would need a more principled discriminator
  than gap size/slope alone, which is S7/field-validation territory
  (compare the preview's per-arm split against real hardware first, per
  the "remaining field-test requirements" list above).
- **6's truncation control** — pass (primary run: gap + `extend_arm`/arm
  B/n=14, exactly as specified).
- **11 (full e2e replay)** — run for real; `tests/test_e2e_capture.py`
  committed as a structural/config-invariant regression guard rather than
  an exact-oracle test, for the run-to-run-noise reasons given above.
- **12 (no regression)** — `pytest -q -k "not e2e"` green after every
  change in this section (capture.py's degenerate-gap guard/clamp, the
  `band_windows_seen` serialization fix, the two new
  `test_capture_coverage.py` tests, the new `test_e2e_capture.py` file).

**S5C is done.** No further stage was started in this pass.

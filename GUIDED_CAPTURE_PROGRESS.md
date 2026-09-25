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


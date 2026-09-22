# High-value scope after the post-implementation audit — final plan

Re-evaluation of `POST_IMPLEMENTATION_AUDIT.md` §5.2 items **H1–H8** against the tree at
`2143693` ("fixed sfm code"), after the M1–M8 mandatory cycle, the F18/M2 ground-frame
pass (`POST_AUDIT_PROGRESS.md`), the RC1–RC7 / A1–A6 / G1–G8 accuracy pass
(`REMAINING_ACCURACY_PROGRESS.md`) and the SfM stabilization pass
(`SFM_STABILIZATION.md`).

No application code was modified while producing this document. Every claim below was
checked against the code, the tests, `data/bench/results.md` and the committed benchmark
artifacts, not against the audit text.

---

## 0. The context that changes every H item's value

Two things happened after the audit was written, and both re-price H1–H8:

1. **`cut_volume_m3` now means "measured only"** (A1). The bridging cull dropped from
   `0.5 × region diameter` to `max(20 × spacing, 0.5 m)`, so the pipeline no longer
   integrates over terrain no camera saw. Every acceptance criterion in the audit phrased
   as "preset error ≤ N %" (H3's "arc photo-mode error ≤ 8 %" in particular) was written
   against the old, interpolating number and **cannot be used as written** — the honest
   measured cut is 24–47 % below truth by construction, and the meaningful check is now
   `cut_measured_m3 ≤ truth ≤ cut_upper_m3` plus `status`.

2. **Coverage is the binding constraint, and no H item moves it.** G6 measures 45–62 %
   coverage of the traced polygon on every preset; no capture geometry in the benchmark
   reaches `status = ok`. The remaining volume error is dominated by the camera-invisible
   far side of the bowl, which is a *capture-protocol* limit, not a code defect. H1–H8 are
   therefore all secondary-order improvements. Ranking them against each other is still
   worth doing; expecting any of them to produce a headline accuracy number is not.

A consequence worth stating once: **G6's `ok` branch has never fired in any test or
benchmark run.** The gate that decides what a field user is told is, in its most
favourable branch, unexercised. That is the reason item **V1** (below) is in scope despite
not being an H item.

---

## 1. Verdict table

| Item | Audit scope | Verdict | Evidence |
| --- | --- | --- | --- |
| **H1** | F16 `hillside25` preset; `up_source` + candidate angle in the result; "level the marker" gravity override | **partially necessary** | `estimate_up` (`densify.py:545`) already computes both candidates and their disagreement angle and logs it — but nothing reaches the result or the gates (`grep up_source` → no hits). No sloped preset exists, so the failure is still unconfirmed on data. Keep the reporting half; defer the preset and the override. |
| **H2** | F23 ArUco detector tuning, full-res crop refinement, multi-reference inverse-variance scale, PnP-derived 1σ | **partially necessary** | Confirmed failure, measurable today: `data/bench/results.md` shows `oblique60` "marker visible in only one photo → no scale" and `nadir` "marker sides disagree by 38 % → refused" — 2 of 8 presets produce no measurement at all. Detection still uses a bare `ar.DetectorParameters()` on a 2200-px downscale (`scaling.py:39-69`). Keep detector tuning + full-res crop. Drop multi-reference (no second reference exists in any preset or in the UI) and drop the PnP-1σ rewrite (the cross-check already exists at `scaling.py:253-262`; converting it into the reported σ would *weaken* G7's honest 3 % floor). |
| **H3** | F18 hole-tolerant ray-cast, cell-sized steps, expose `hit_frac`/`region_method` | **already resolved, one test missing** | `ground.py:151-235` steps at `cell / 2`, tolerates an 8-cell NaN gap, returns `hit_frac` and `max_miss_run`; `fill_dsm_holes` (EDT-based) runs first; `pipeline.py:244-248` publishes both; G5 gates on them; `result.js` shows them. Every preset now selects via `ground_frame`. Only gap: the audit's own "DSM with 20 % random holes → `hit_frac ≥ 0.95`" regression test does not exist in `tests/test_ground.py`. Its second criterion ("arc error ≤ 8 %") is **invalidated by A1**. |
| **H4** | F6 lock-free reload; `save_state` under a lock; cancel API; resume marking; SSE with disconnect detection | **partially necessary** | F6 is **resolved** (`jobs.py:135` `start_ctx_reload`, status poll no longer blocks). Still open and confirmed: `save_state` (`jobs.py:56`) is unlocked and every caller shares one `state.json.tmp`; `DELETE /api/jobs/{id}` (`routes.py:450`) `rmtree`s the directory under a running worker with no busy check (the measure/ortho endpoints do check `BUSY_STATUSES`, delete does not); `lifespan` (`main.py:27`) has no shutdown path, so `concurrent.futures`' atexit join waits out a running SfM; `load_persisted_jobs` promotes an interrupted job to `ready` when *any* file exists under `sparse/*/` (`jobs.py:87-92`). SSE is **latent only** — no `EventSource` anywhere in `server/static/js/`, so nothing connects to `/events` today. True cancellation needs a per-job process; a busy-guard removes the crash without it. |
| **H5** | F19 DEM alignment in the worker on the dense cloud, yaw sweep, RMS/inlier gate | **still necessary, reduced scope** | Confirmed: `routes.py:272-301` runs `align_to_dem` in the request thread on the parent's `job.ctx`; the parent never builds a dense cloud (all heavy stages run in `server/worker.py`), `ctx.dense` is `None`, so `dem.py:197 ctx.cloud(dense=True)` silently returns the **sparse** cloud. `icp_rigid` (`dem.py:85`) has no rotation search about the vertical; seeding is gravity + centroid only. Moving the stage into the worker is a bigger change than needed — the dense cloud is already on disk as `dense_<w>_v2_<fingerprint>.npz` (`densify.py:650`) after any ortho/measure run and can simply be loaded. |
| **H6** | F22 `surface_filter` `min_cos` 0.25 → 0.1; report in-polygon dropped fraction | **invalidated by later evidence (threshold); optional (diagnostic)** | The benchmark cannot confirm or measure it: the synthetic terrain's steepest feature is the cosine bowl, `depth 2.0 / R 6.0` → max slope **27.6°** (`tools/synth.py:28,92-93`), far inside the current 75° cut. Nothing in the scene is dropped by `surface_filter` except the vertical marker board — which it exists to drop. Loosening to 84° therefore has **no measurable benefit** on any available data and a real regression path (re-admitting the board and any wall into the integral). The dropped-fraction diagnostic is harmless but speculative; parked. |
| **H7** | Real-photo regression set, ≥ 3 GNSS/TLS sites, `benchmark.py --real` | **still necessary — blocked, not schedulable** | Unchanged and still the only route to a field accuracy claim. No real data exists in this environment and none can be produced by a code change. Keep as the standing precondition on any deployment claim (it is already the No-Go reason in both prior passes); it is not an implementable item here. |
| **H8** | Playwright browser smoke test; k-NN splat so the ortho is traceable | **split: ortho half still necessary, Playwright optional** | Ortho half **confirmed by direct measurement** of the committed artifacts: background (unpainted) pixel fraction is arc 0.78, collinear 0.56, descending 0.79, distorted 0.78, lowtex 0.78, sparse8 0.85 at 1.8–2.3 cm/px — the surface the operator is told to trace on is 56–85 % empty speckle, and ortho mode is exactly the path the audit's "conditional go" recommends. `render_orthophoto` (`ortho.py:76-81`) writes one pixel per point with no splat. Playwright: the DOM-free coordinate math is already covered by `tests/test_coords.mjs`; a browser harness adds a dependency and a download to a repo with zero JS dependencies, for an untested-interaction risk that has produced no reported defect. |

---

## 2. Approved scope, in implementation order

Each item below has a confirmed failure, a measurable target, objective acceptance
criteria and bounded compatibility risk. Items are ordered by (confirmed impact on the
recommended workflow) ÷ (risk + cost). Every item is independent; stopping after any one
leaves a consistent tree.

### P1 — Splat the orthophoto so it can actually be traced  *(H8, ortho half)*

**Failure.** `render_orthophoto` paints exactly one pixel per cloud point at
`res = span / 1400`, while the dense cloud's spacing is several times that. Measured
background fraction on the committed artifacts: 0.56–0.85. Ortho mode is the audit's
recommended pilot path and its tracing surface is mostly empty.

**Change.** `landslide/ortho.py::render_orthophoto` only: splat each point into a
`k × k` block, `k = clip(ceil(median_spacing / res), 1, 7)`, preserving the existing
highest-point-wins write order. Median spacing from the same k-d tree trick
`ground.estimate_cell_size` already uses. Report the post-splat covered fraction in the
existing `[ortho]` log line.

**Acceptance.**
- Background fraction ≤ 0.20 on all six scale-able presets (from 0.56–0.85).
- `ortho.json` is **byte-identical in every field** (`u0`, `v0`, `res`, `width`,
  `height`, `up`, `e1`, `e2`, `scale`) — this is what keeps stored polygons and
  `select_region_ortho` numerically unchanged.
- Ortho-mode volume on arc / lowtex / distorted within ±1 pp of the current
  25.90 / 26.33 / 23.88 %.
- Added `tests/test_ortho.py` case: synthetic cloud at 5 × `res` spacing → covered
  fraction ≥ 0.8 and the highest point still wins every contested pixel.
- Ortho render wall time increase ≤ 1 s per preset.

**Compatibility risk: none.** Region selection reads `meta`, never the image pixels
(`ortho.py:140`). The change is confined to the rendered JPEG.

---

### P2 — ArUco detection at severe angles  *(H2, detection half only)*

**Failure.** Two of eight presets yield no measurement at all for scale reasons:
`oblique60` (marker detected in 1 of 21 frames, needs ≥ 2 to triangulate) and `nadir`
(38 % triangulated side-spread → `aruco_scale` refuses). `arc`, at a comparable apparent
marker size but a shallower 15° elevation, scales to 1.27 %, which locates the failure at
viewing angle, not marker size.

**Change.** `landslide/scaling.py::detect_marker_corners` /`_load_gray` only:
configure `DetectorParameters` (`cornerRefinementMethod = CORNER_REFINE_APRILTAG`,
widened `adaptiveThreshWinSizeMin/Max/Step`, relaxed `polygonalApproxAccuracyRate`), and
refine the accepted corners at **full resolution inside a crop** around the coarse hit
instead of `cornerSubPix(win=5)` on the 2200-px downscale. No change to `aruco_scale`'s
refusal thresholds, to `_fit_square_side`, or to the PnP cross-check.

**Acceptance.**
- `oblique60`: marker detected in ≥ 2 views and a scale produced with rel. error ≤ 5 %
  (today: 1 view, no scale).
- No regression on the six presets that already scale: |Δ scale err| ≤ 0.5 pp each
  against arc 1.27 / collinear 2.83 / descending 0.52 / distorted 3.27 / lowtex 0.48 /
  sparse8 3.56.
- `nadir` must **either** still refuse **or** produce ≤ 5 % — silently accepting a
  marginal vertical-marker detection is a failure of this item, not a pass.
- `tests/test_scaling.py` passes unchanged (47 collected cases), including
  `test_aruco_scale_clean_corners`.

**Compatibility risk: low.** Detection-side only; every downstream refusal gate stays.
**Stated caveat:** these parameters are tuned against a Gouraud-shaded synthetic marker.
The "no preset regresses" criterion bounds the worst case to neutral, but this item does
**not** constitute evidence about real 20–30 cm field markers — that remains H7.

---

### P3 — Server lifecycle defects  *(H4, minus cancellation and SSE)*

Four confirmed, independent defects; one small commit each.

**(a) Delete under a running worker.** `DELETE /api/jobs/{id}` has no busy check and
`rmtree`s the job directory while a worker writes into it; the worker then recreates
`artifacts/` and the callback's `save_state` writes into a deleted tree.
*Change:* return `409` while `status in BUSY_STATUSES or status == "reconstructing"`.
*Acceptance:* TestClient — a job forced to `measuring` returns 409 on DELETE, the
directory still exists, and DELETE succeeds once the status clears.

**(b) Unlocked `save_state`.** Worker-callback thread and request threads share one
`state.json.tmp`; interleaved `write_text` before `os.replace` can publish torn JSON.
*Change:* take `self.lock` (or a dedicated state lock) around build+write; give the temp
file a unique suffix.
*Acceptance:* the audit's own criterion — 100 concurrent `save_state` calls, every
subsequent read of `state.json` parses as valid JSON.

**(c) Shutdown blocks on a running SfM.** No shutdown path in `main.py`'s `lifespan`;
`concurrent.futures`' atexit handler joins the workers, so SIGTERM waits out a multi-minute
reconstruction.
*Change:* in `lifespan`'s teardown, terminate the pool's worker processes via a new
`executor.shutdown_now()`.
*Acceptance:* with a worker running a long sleep, `shutdown_now()` returns in ≤ 2 s and
no pool child process remains alive; a TestClient app shutdown completes < 5 s.

**(d) Interrupted reconstruction resumes as `ready`.** `Job.reconstructable` accepts
*any* file under `sparse/*/`, so `load_persisted_jobs` promotes a half-written model.
*Change:* require the model to be complete (`cameras`, `images` and `points3D` files
present in the selected model directory) before promoting to `ready`; otherwise `error`
with the existing "interrupted before the reconstruction finished" message.
*Acceptance:* a fixture with an empty `sparse/0/` and one with a partial model both
resume as `error`; a complete model still resumes as `ready`; `tests/test_server.py`
(19 cases) passes unchanged.

**Compatibility risk: low**, one visible API change — DELETE now returns 409 while busy.
The UI's delete control must surface that message.

**Explicitly excluded from P3:** the cancel endpoint (needs a per-job
`multiprocessing.Process` to be terminable — real work, and jobs run minutes on a
single-operator tool) and SSE disconnect handling (no client connects; latent only).

---

### P4 — Report which "up" was chosen  *(H1, reporting half)*

**Failure.** `estimate_up` picks between the scene-plane and camera-plane normals, and on
disagreement decides by a "ground-like vote" that is biased toward the scene plane by
construction. On a hillside the scene plane *is* the hillside, so slope stats, the hazard
map, the DSM/ortho "top-down" view and the DEM gravity seed are all referenced to the
slope instead of gravity. The chosen source and the disagreement angle are computed
already and go only to the log.

**Change.** `estimate_up(..., info: dict | None = None)` fills
`{"up_source": "scene_plane" | "camera_plane", "disagree_deg": float,
"collinearity": float}` when a dict is passed — **no call-site signature changes** at the
other seven call sites. `pipeline.measure` passes one and publishes `up_source` /
`up_disagree_deg` in the result; `gates.py` flags `indicative` when `disagree_deg > 20°`
(i.e. the vote, not agreement, decided); `result.js` shows it beside `region_method`.

**Acceptance.**
- New `tests/test_up.py` case: synthetic ground tilted 25° with an arc of cameras above
  it → the reported `disagree_deg` is within 2° of the constructed angle and `up_source`
  names the branch actually taken.
- `up_source` and `up_disagree_deg` present in every `measure()` result (e2e assertion).
- **Zero numeric change**: all eight presets' benchmark volume/scale columns identical to
  `data/bench/results.md` before the change.

**Compatibility risk: none** — additive result fields plus one optional parameter.

**Deferred from H1:** the `hillside25` render preset and the "level the marker" gravity
override. The preset is a full render + SfM cycle whose ground truth is itself ambiguous
(cut volume along gravity vs. along the slope normal are different quantities on a tilted
scene, and which one a user wants is undecided); the override is new UI plus pipeline
plumbing for a scenario no data confirms yet. P4 makes the condition *visible*, which is
the part that does not depend on resolving either question.

---

### P5 — DEM alignment: use the dense cloud, search heading  *(H5, reduced)*

**(a) Yaw sweep.** `icp_rigid` has no rotation search about the vertical, so alignment
converges only when the model's heading is already within roughly 30° of the DEM's.
*Change:* in `align_to_dem`, seed 12 starts at 30° increments about `up`, keep the best
trimmed RMS.
*Acceptance:* new `tests/test_dem.py` case with a 120° heading offset aligns to < 0.1 m
RMS (fails today); existing 6 DEM/change cases unchanged; `align_to_dem` wall time ≤ 3 ×
current at 60 k points.

**(b) Align on the dense cloud without running stereo in the parent.** The parent process
never builds a dense cloud, so `ctx.cloud(dense=True)` silently returns the sparse one —
including the outliers that pull the centroid seed. The dense cloud is nonetheless already
on disk after any ortho or measure run.
*Change:* extract the existing cache lookup from `densify.dense_cloud` into
`load_cached_dense(ctx)` (fingerprint-checked, same file name) and call it from
`align_to_dem` before falling back to sparse. Log which cloud was used.
*Acceptance:* after an ortho run, DEM upload logs a point count > the sparse count and
reports a lower RMS on the same DEM; with no cache present the sparse path still works and
says so; a monkeypatch test asserts `dense_cloud` is **never** called from the parent
process.

**Compatibility risk: low.** Both halves degrade to today's behaviour.
**Excluded:** moving DEM alignment into a worker job (turns a synchronous endpoint into an
async one — API and UI change for a latency win, not a correctness one).

---

### P6 — The missing hole-tolerance regression test  *(H3 remainder)*

**Change.** `tests/test_ground.py` only: build a DSM, blank 20 % of its cells at random
(fixed seed), ray-cast a known polygon.
**Acceptance.** `hit_frac ≥ 0.95` and the recovered ground polygon within one cell of the
no-holes result. This is the audit's own H3 criterion and the only part of H3 not already
in the tree; its companion criterion ("arc error ≤ 8 %") is invalidated by A1 and is
**not** adopted.
**Compatibility risk: none** (test-only).

---

### V1 — `twosided` preset: prove `status = ok` is reachable  *(not an H item — see §0)*

**Failure.** G6's `ok` branch (coverage ≥ 0.85) has never fired. Every preset measures
45–62 % coverage and lands on `indicative` or `rejected`. An unexercised branch decides
the most favourable thing the system can tell an operator, and the 0.85 threshold itself
has never been tested against a capture that should clear it.

**Change.** `tools/synth.py` and tests only — add a preset with two camera arcs ~90° apart
(e.g. 2 × 11 views) over the existing terrain, i.e. exactly the capture protocol both
prior passes recommend.

**Acceptance.** One of two outcomes, both acceptable and both informative:
- `coverage_frac ≥ 0.85`, `status == "ok"`, |photo error| ≤ 10 % and |ortho error| ≤ 10 %
  → the gate is calibrated and the recommended protocol is validated; pin it in
  `tests/test_e2e_presets.py`; **or**
- it does not clear 0.85 → the threshold is re-derived from the measured ceiling and the
  change is documented, because a permanently unreachable `ok` is a miscalibrated gate,
  not a conservative one.

**Compatibility risk: none** — no application code; adds one preset to the benchmark's
runtime.

*Flagged for the reader:* V1 is outside H1–H8. It is included because it meets all four
inclusion criteria and because P1, P2 and V1's own numbers are all measured through the
preset benchmark — if the benchmark cannot express a passing capture, none of the other
items can be judged against a good one. Cut it first if scope must shrink.

---

## 3. Rejected, with the reason

| Dropped | From | Reason |
| --- | --- | --- |
| `hillside25` preset; "level the marker" gravity override | H1 | Ground truth on a tilted scene is ambiguous (gravity-referenced vs. slope-referenced cut); the override is new UI for an unconfirmed scenario. P4 makes the condition observable first. |
| Multi-reference scale with inverse-variance weighting | H2 | No second reference exists in any preset or in the UI. Speculative until a capture has one. |
| Replacing `scale_rel_error` with a PnP-derived 1σ | H2 | The cross-check already exists (`scaling.py:253-262`) and G7 uses it as a *gate*. Reporting it as σ would drop below the honest 3 % floor on a single-marker capture — a confidence increase with no new information. |
| "arc photo-mode error ≤ 8 %" | H3 | Invalidated by A1: `cut_volume_m3` is now measured-only and 24–47 % below truth by design. The live criterion is `cut_measured ≤ truth ≤ cut_upper` plus `status`. |
| `POST /jobs/{id}/cancel`, worker termination | H4 | Needs a per-job terminable process. P3(a)'s busy-guard removes the actual crash; jobs run minutes on a single-operator tool. |
| SSE disconnect detection, client wiring | H4 | No `EventSource` in `server/static/js/` — nothing connects. Latent cost only. |
| Moving DEM alignment into a worker job | H5 | Converts a synchronous endpoint into an async one (API + UI change) for latency, not correctness. P5(b) gets the dense cloud without it. |
| `min_cos` 0.25 → 0.1 | H6 | **Unmeasurable and risk-positive on current data.** The synthetic terrain's steepest slope is 27.6°; nothing is dropped by the 75° filter except the vertical marker board it exists to drop. Loosening to 84° can only re-admit boards and walls. Revisit only with a scene that has a real scarp. |
| In-polygon dropped-fraction diagnostic | H6 | Harmless but speculative; no reported defect. Parked, not rejected on principle. |
| Real-site regression set | H7 | Still necessary, still the standing precondition for any field accuracy claim, but not implementable here — no real data, no GNSS/TLS reference. Unchanged from both prior passes' No-Go. |
| Playwright browser smoke test | H8 | Coordinate math already covered by `tests/test_coords.mjs`; adds a dependency and a browser download to a zero-JS-dependency repo for a risk that has produced no reported defect. |

---

## 4. What this plan does not claim

- It does not move the coverage ceiling. After P1–P6 and V1, single-azimuth captures will
  still measure 45–62 % of the traced region and still report `indicative` or `rejected`.
  The gain is: a traceable orthophoto, two more presets that can be measured at all,
  a server that survives its own lifecycle, a visible `up` decision, usable DEM
  differencing at arbitrary heading, and a benchmark that can express a passing capture.
- It produces no field accuracy evidence. H7 remains outstanding and the deployment
  recommendation stays **No-Go for unsupervised field use / conditional pilot with every
  result labelled**, exactly as `REMAINING_ACCURACY_PROGRESS.md` §8 left it.

## 5. Suggested sequencing

P1 → P2 → P3 → P4 → P5 → P6, with V1 run once before P1 and again after P2 (it is the
measuring stick for both). P3 is independent of the rest and can be done in parallel or
first if server robustness is the more pressing need.

# Post-Implementation Audit — SlopeLens after Tiers 0–3

Independent, read-only audit of the repository as it exists at commit `4fceb14`
("server update"). Nothing in `landslide/`, `server/`, `tools/`, `tests/` or the
existing documentation was modified; every number below was re-measured in this
audit (Fedora, Linux 7.1, Python 3.14.7, pycolmap 4.1.1, OpenCV 5.0, 24 GB RAM,
no CUDA). `implementation_plan.md`, `architecture.md` and
`IMPLEMENTATION_PROGRESS.md` were read but **not trusted**: every claim that could
be checked was checked against the code or a fresh run.

Classification used throughout:

* **CONFIRMED** — reproduced in this audit (failing command, probe output, or an
  unambiguous code path with a concrete failing input).
* **PROBABLE** — logic/edge case that will fail on realistic input; not driven to a
  failing run here (usually because the synthetic scene cannot exercise it).
* **SPECULATIVE** — technical debt / latent risk.

### What was run

| Command | Result |
| --- | --- |
| `git status` / `git log` | clean tree; Tiers 0–3 = `9834dd2..4fceb14`, 190 files, +12 025 / −1 560 |
| `pytest -q -k "not e2e" --ignore=tests/test_server.py` | **117 passed** in 26 s |
| `pytest -v tests/test_server.py` | **12 passed, 1 failed** (`test_worker_crash_is_isolated_and_pool_recovers`: "crash callback never fired"); **interpreter never exits** — killed by `timeout` after 300 s |
| `pytest -q -k "not e2e"` (whole fast suite, as documented) | all tests run, then **hangs forever at exit** (same root cause; had to be `kill`ed after 10 min) |
| `pytest -s tests/test_e2e_synth.py` (cached recon, `data/synth`) | 5 passed, 10 s; photo cut 75.7 vs 67.2 m³ truth (**12.6 %**), ortho 66.2 (1.5 %); dense cloud **7 299 points**; `[ground] ray-cast hit only 63 % … falling back to image-plane selection`; orthophoto `7 293 / 1 766 661 cells covered` |
| `node --test tests/test_coords.mjs` | 8/8 |
| `python -m tools.benchmark --presets <p>` × 8 presets, each under `/usr/bin/time -v` | table in §3 |
| probe scripts (scratchpad only): executor crash, per-pair stereo yield, sparse-extent statistics, measure/CI calibration, raster/bootstrap timing, dense-cloud-vs-GT geometry, per-camera focal lengths | quoted inline |

---

## 1. Current Architecture Assessment

### 1.1 What is actually there

The architecture described in `architecture.md` matches the tree structurally:
`landslide/` (pure library) → `server/{main,routes,jobs,worker,executor,schemas}.py`
(FastAPI + `ProcessPoolExecutor(2)`) → `server/static/js/` (ES modules). The heavy
stages run in worker processes that reload `ReconCtx` from `work/` on every call; the
job directory is the only shared state. The measurement path is:

```
photos → sfm.reconstruct (retry ladder, pycolmap) → scaling (ArUco/manual)
       → densify.dense_cloud (per-reference multi-view SGBM consensus, voxel, SOR,
          support clip, normal filter; cached as dense_<w>_<fp>.npz)
       → region selection: ground.select_region_ground (ray-cast → DSM)  ┐ photo mode
                           volume.select_region (image-plane, fallback)   ┘
                           ortho.select_region_ortho                       ortho mode
       → volume.prism_volume (rim datum plane/quad/TPS, TIN prism integral,
          raster cross-check, bootstrap CI) → pipeline.measure (scale-error widening,
          artifacts) → state.json
```

### 1.2 Assessment

**Strengths (verified).** The library layer is coherent and well-commented; the TIN
integrator with the robust datum ladder is numerically solid on the unit fixtures
(`tests/test_volume.py`, all tight tolerances pass unchanged); the dense cache is
correctly keyed to the pose set (`sfm._recon_fingerprint`); typed request bodies
reject malformed input with 422; the ortho-mode measurement on the arc scene is
genuinely good (0.7–1.5 % on `arc`, 3.1 % on `lowtex`); the process split keeps a
hard native crash from killing the HTTP process (the *process* survives — see §2 for
what happens to the executor afterwards).

**Weaknesses (verified).**

1. **The dense cloud is starved by a one-line bug** (§2, F2): the fusion voxel is
   derived from the raw extent of the sparse cloud, which is inflated ~7× by a handful
   of far outliers. Every downstream number in this repo — the 7.3 k-point cloud, the
   99.6 %-empty orthophoto, the ground-frame path that always falls back, the 500-point
   test threshold, the "raster integrator regressed to 33–40 %" design decision in
   T2.1 — is a consequence of a cloud that is ~50× sparser than intended. The Tier 1
   "accuracy improvements" were tuned on this starved cloud.
2. **The headline T1.2 feature is inactive on the only benchmark scene.** Every photo
   mode measurement in this audit logged `ray-cast hit only 63 %` and used the legacy
   image-projection path (`region_method = "image_projection"`).
3. **The crash-recovery design deadlocks on the very event it exists for** (§2, F1).
   After one worker crash the executor's management thread is wedged, `on_done` never
   runs, and every subsequent `submit_job` blocks forever. The T3.1 test that would
   have shown this was recorded as "unconfirmed, probably CPU contention"; it is a
   deterministic deadlock.
4. **The uncertainty reported to the user is overconfident** (§2, F9): on the arc
   scene the truth lies outside the reported 95 % CI, and the scale term is
   propagated with the wrong power.
5. **Structural complexity added by Tiers 1–3 is moderate and mostly justified**, but
   three things were added without their guard rails: `select_pairs` is now dead code
   (only tests call it; `dense_cloud` no longer does), `StereoConfig.max_pairs` is
   unused, and `_raster_bin`/`_fill_small_holes`/`bootstrap_volume_ci` are Python
   per-cell loops that are only fast because the cloud is starved.
6. **The validation harness is itself wrong for straight-line presets** (§2, F13):
   Umeyama on camera *centres* has a free rotation about the line, so the `collinear`
   and `nadir` "cloud RMS 31 m / 44 m" and ortho failures reported in
   `architecture.md §7.1` are artefacts of `tools/benchmark.py`, not of the pipeline.

### 1.3 Line counts (current tree)

`landslide/` 3 152 lines (`volume.py` 1 083, `densify.py` 691, `sfm.py` 523),
`server/` 921 (`routes.py` 428, `jobs.py` 238), `tools/` 528, `tests/` ~2 900,
`server/static/js/` ~1 150.

---

## 2. Regression and Correctness Report

Findings are ordered by severity. "Effort" is engineer-days for the smallest fix plus
its test.

### F1 — CONFIRMED — Worker crash deadlocks the executor and the whole server

* **Where:** `server/executor.py:54-59` (`_recreate_pool` → `_pool.shutdown(...)`)
  called from `server/executor.py:83-92` (`_cb`, the future's done-callback).
* **Repro:** `pytest -v tests/test_server.py` → `test_worker_crash_is_isolated_and_pool_recovers`
  fails with "crash callback never fired" **on an idle machine**, and the pytest
  process never exits (must be killed). Standalone probe (`submit_job` with a worker
  that calls `os._exit(1)`) dumped this stack with `faulthandler`:
  ```
  Thread-2: process.py:956 shutdown  ← executor.py:58 _recreate_pool ← executor.py:88 _cb
            ← _base.py:335 _invoke_callbacks ← _base.py:564 set_exception
            ← process.py:579 _terminate_broken ← process.py:602 terminate_broken (holds shutdown_lock)
  MainThread: threading.py:1133 join ← process.py:106 _python_exit
  ```
  `terminate_broken` runs `with self.shutdown_lock:` and, inside it, sets the
  exception on every pending future, which invokes `_cb` **on the same thread**;
  `_cb` → `_recreate_pool` → `ProcessPoolExecutor.shutdown` → `with self._shutdown_lock`
  (non-reentrant `threading.Lock`) → deadlock. `_recreate_pool` also holds
  `executor._state_lock` at that moment, so every later `submit_job` →
  `_ensure_started` blocks forever, and `on_done` is never called: the crashed job
  stays `measuring`/`reconstructing` in `state.json` forever.
* **Impact:** the S2 acceptance criterion ("server survives a killed worker") is
  inverted — one native crash (pycolmap/OpenCV segfault, OOM-kill) silently disables
  all further reconstruction/measure/ortho for every job until the server is
  restarted, and `uvicorn` shutdown then hangs (atexit joins the wedged thread). The
  fast test suite hangs at exit for the same reason → CI blocker.
* **Effort/risk:** 0.25 d, low.
* **Smallest fix:** never call `shutdown()` on a *broken* pool from the callback. In
  `_cb`, on `BrokenProcessPool`, just swap the module-level `_pool = _new_pool()` under
  `_state_lock` (the broken pool is already terminating its own workers), or hand the
  whole `_cb` body to `threading.Thread(target=...).start()` so it never runs on the
  management thread. Keep the `submit()`-time `BrokenProcessPool` branch as is.
* **Proof:** the existing test must pass **and** the pytest process must exit within
  5 s (`timeout 60 pytest tests/test_server.py` returns 0). Add: after the crash,
  `submit_job` on a *different* job completes within 10 s.
* **Compat/rollback:** none; internal.

### F2 — CONFIRMED — Dense cloud starved by outlier-inflated voxel size (root cause of most accuracy findings)

* **Where:** `landslide/densify.py:617-618`
  (`extent = np.ptp(ctx.sparse).max(); voxel = extent / 900`), and the same `extent`
  reused at `:663` (`sup_radius`) and by `volume.fit_plane_ransac` (0.5 % of extent).
* **Repro (arc, cached recon):** sparse cloud raw extent = **214 m** (ptp per axis
  177/85/214 m); 1–99 percentile extent = **32/14/32 m**; 99 % of points lie within
  22.8 m of the median, the farthest at 251 m. Voxel therefore = 0.238 m; dense cloud =
  7 299 points at 0.14 m median spacing for a 36 m scene. Per-reference fused depth
  maps are *not* the problem — probe: 63 k / 147 k / 107 k / 68 k / 34 k points per
  reference before voxelisation; all of it collapses in `voxel_downsample`.
  Consequences measured: `sup_radius = 4.3 m` (support filter is inert), orthophoto
  `7 293 / 1 766 661 cells covered` (unusable for tracing in the UI), `unmeasured_area_m2`
  = 39 of 83 m² in photo mode, ground-frame hit fraction 63 % → permanent fallback.
* **Impact:** every accuracy number in Tiers 1–2 was tuned on a cloud ~50× sparser
  than designed; `architecture.md`'s "2.5 M points ≈ 2 cm spacing" is off by 10×.
* **Effort/risk:** 0.5 d; **medium** — fixing it exposes F10 (runtime cliff) and will
  change every benchmark number, so do it together with F10 and re-pin thresholds.
* **Smallest fix:** `lo, hi = np.percentile(ctx.sparse, [1, 99], axis=0); extent =
  float((hi - lo).max())` (one line); optionally drop sparse points farther than
  3× the 99th-percentile radius from the median before use.
* **Proof:** unit test with a 10 k-point plane plus 20 points at 10× distance:
  `dense_cloud`'s voxel must be within 10 % of the value without the outliers.
  E2E: arc dense cloud ≥ 150 k points; ortho covered cells ≥ 15 %; ground-frame
  `hit_frac` ≥ 0.9 on `arc`; restore `n_points > 5000` in `tests/test_e2e_synth.py:109,153`.
* **Compat/rollback:** cache filename includes only `stereo_width` and the pose
  fingerprint, so existing `dense_*.npz` files would be reused with the *old* voxel.
  Bump the cache key (e.g. `dense_<w>_v2_<fp>.npz`) or store `voxel` in the npz and
  compare.

### F3 — CONFIRMED — Disparity search capped at 320 px starves every wider-baseline pair

* **Where:** `landslide/densify.py:378-380` (`num_disp = clip(..., 16, 320)`).
* **Repro:** `stereo_pair` on the cached arc recon for reference `IMG_00` against
  neighbours at baseline/depth ratio 0.16 / 0.25 / 0.33 / 0.41 returned
  **337 338 / 164 589 / 47 025 / 0** points. At 1280 px, f≈1707 px, the median
  disparity for ratio 0.33 is ≈ 560 px > `min_disp + 320`; the range from the sparse
  1 %/99 % depth percentiles is correct, the cap silently truncates it.
* **Impact:** `StereoConfig.baseline_ratio = (0.10, 1.5)` and the T1.3 gates admit
  pairs that then contribute nothing; T1.4's consensus (needs ≥ 2 agreeing candidates)
  loses votes exactly where the geometry is best-conditioned. On real sets shot closer
  to the slope (oblique60 has ratio ≈ 0.4 everywhere) the dense stage can yield
  almost nothing.
* **Effort/risk:** 0.25 d, low.
* **Smallest fix:** raise the cap to 1024 (or `w // 2`) **and** when `span > cap`,
  downscale the pair (`max_width *= cap / span`) instead of truncating; log the
  truncation. SGBM cost is linear in `numDisparities`, so pair with F10's budget.
* **Proof:** per-pair yield on `arc` for ratio 0.33 ≥ 50 % of the ratio-0.16 yield;
  unit test that the chosen `[min_disp, min_disp+num_disp]` covers the 1–99 %
  disparity range of the sparse points.
* **Compat/rollback:** memory per SGBM call grows with `numDisparities` (see B3);
  keep the cap configurable in `StereoConfig`.

### F4 — CONFIRMED — Scale error propagated with the wrong power (2 instead of 3)

* **Where:** `landslide/pipeline.py:265` (`pad = 2.0 * scale_rel * abs(net)`) and
  `:271` (fallback heuristic, same factor).
* **Repro:** volume = Σ area × height; area ∝ s², height ∝ s → V ∝ s³, so
  dV/V = 3·ds/s. With `scale_rel_error = 1.2 %` (arc ArUco) the scale term is
  2.4 % instead of 3.6 %.
* **Impact:** systematic 33 % under-estimate of the scale contribution to
  `net_volume_ci95_m3` / `est_volume_error_m3` on every measurement; with a 25 cm
  field marker (`rel_err` typically 3–8 %) the missing term is 3–8 % of volume.
* **Effort/risk:** 0.1 d, none.
* **Smallest fix:** `3.0 * scale_rel * abs(net)` in both places (and decide whether
  `scale_rel_error` is 1σ or a bound; today it is neither — see F9).
* **Proof:** unit test: measure a synthetic cloud with `scale_rel_error = 0.10`,
  assert `hi - net ≥ 0.30 * |net|`.
* **Compat:** result values change (wider intervals); no schema change.

### F5 — CONFIRMED — Re-scaling leaves stale orthophoto / DEM / result in the job

* **Where:** `server/routes.py:218-231` and `:234-245` (`scale_aruco`,
  `scale_manual`) never touch `job.ortho`, `job.dem_info`, `job.result`;
  `server/routes.py:380` returns `{"ready": true}` whenever `job.ortho is not None`.
* **Repro (by construction):** `POST …/ortho` (writes `ortho.json` with `u0, v0, res`
  in metres at scale s₁) → `POST …/scale/manual` (scale s₂) → `POST …/measure`
  `mode=ortho`. `ortho.select_region_ortho` converts polygon px → metres with s₁'s
  `u0/res`, `select_region_world` multiplies the cloud by s₂: the region is shifted
  and scaled by s₂/s₁ relative to what the user traced. `dem_info["R","t"]` were fit
  against the s₁ cloud and are wrong by the same factor; the UI still shows the old
  `result`.
* **Impact:** silent wrong region after any scale correction — the exact workflow a
  field user follows when the first ArUco detection looks off.
* **Effort/risk:** 0.25 d, low.
* **Smallest fix:** in both scale routes, `job.ortho = None; job.dem_info = None;
  job.result = None`, unlink `artifacts/ortho.{jpg,json}`, then `save_state()`.
  Store `scale` inside `ortho.json` and refuse an ortho measure whose stored scale ≠
  `ctx.scale` (belt and braces).
* **Proof:** `tests/test_server.py`: set `job.ortho`, POST a scale, assert snapshot
  `ortho is None` and `POST …/ortho` returns `{"queued": true}`.
* **Compat:** persisted jobs with an `ortho` block keep it until the next scale call.

### F6 — CONFIRMED — `ensure_ctx` holds `job.lock` for the whole COLMAP reload; S4 is still blocking

* **Where:** `server/jobs.py:103-133` (`with self.lock:` wraps `reconstruct(...)`);
  `snapshot()` at `:158` and `set_status()` at `:80` take the same lock.
* **Repro (by construction):** first `GET /api/jobs/{id}` after a restart returns
  immediately with `ctx_loading: true` and spawns the reload thread, which then holds
  `job.lock` for the multi-second reload; the *next* poll (1.2 s later) blocks in
  `snapshot()` until the reload finishes, and a worker completion callback
  (`set_status`) blocks with it.
* **Impact:** the UI freeze S4 was meant to remove still happens (one poll later);
  under the anyio threadpool (40 threads) a few stuck polls are survivable, but the
  claim in `architecture.md §1.2` is false.
* **Effort/risk:** 0.25 d, low.
* **Smallest fix:** build `ctx` *outside* the lock, then `with self.lock: if
  self.ctx is None: self.ctx = ctx`. Keep the `_ctx_loading` guard.
* **Proof:** test with `reconstruct` monkeypatched to sleep 2 s: `snapshot()` during
  the reload must return in < 100 ms.

### F7 — CONFIRMED — Best SfM attempt is not what is on disk; every stage reloads the last attempt

* **Where:** `landslide/sfm.py:242-289` keeps `best = (score, rec, …)` in memory
  across attempts, but every attempt wipes `work/sparse/` and `database.db`
  (`sfm.py:307-312`), so `work/` holds the **last** attempt. `reconstruct(reuse=True)`
  (`sfm.py:226-233`) reloads from disk; since T3.1 *every* stage (`worker.run_measure`,
  `run_ortho`, `Job.ensure_ctx`) does exactly that and the in-memory `best_rec` from
  `run_reconstruction` is discarded (`server/worker.py:37-41` returns only `n_views`).
* **Repro (by construction):** attempt 1 → 17/21 registered (usable, not "done"),
  attempt 2 (GLOMAP) → 12/21, … attempt 5 → 10/21. Log says "reconstruction done
  (default): 17/21"; every measurement uses the 10/21 model. Also: the dense-cache
  fingerprint is computed on the disk model, so nothing detects the swap.
* **Impact:** silent use of a worse model on any set that does not finish on the first
  rung — i.e. precisely the difficult field sets the ladder exists for.
* **Effort/risk:** 0.25 d, low.
* **Smallest fix:** at the end of `reconstruct`, if `best` is not the last attempt,
  `shutil.rmtree(sparse)`; `best_rec.write(sparse/0)` (pycolmap `Reconstruction.write`)
  so disk == best. Or write each attempt to `sparse_attempt_<i>` and symlink the best.
* **Proof:** monkeypatch `_run_attempt` to return two models (17 then 10 registered)
  and assert `reconstruct(reuse=True)` afterwards reports 17.

### F8 — CONFIRMED — Point-count assertion weakened from 5 000 to 500 hides F2

* **Where:** `tests/test_e2e_synth.py:109,153`.
* **Repro:** photo-mode `n_points = 1 793`, ortho `1 902` on the cached arc scene; the
  original threshold would have failed and pointed straight at F2.
  `IMPLEMENTATION_PROGRESS.md` rationalises the drop as "consensus fusion produces a
  smaller cloud"; the per-reference probe shows consensus is not where points are lost.
* **Fix/proof:** restore `> 5000` once F2 is fixed (expected ≥ 40 k).

### F9 — CONFIRMED — Bootstrap CI is miscalibrated; truth lies outside the reported 95 % interval

* **Where:** `landslide/volume.py:581-637` (`bootstrap_volume_ci` resamples the *rim*
  only; interior points and their coverage are fixed; the raster proxy has no
  hole-fill), `:1010-1015`, `landslide/pipeline.py:261-268`.
* **Repro (arc, photo mode, `rim_px=14`, cached recon):** cut 78.65 m³ vs truth 67.16
  (17.1 %); `net_volume_ci95_m3 = [-82.27, -73.27]` (≈ ±4.5 m³ after the scale pad);
  `est_volume_error_m3 = 5.37`; actual error **11.49 m³**. With `rim_px=12` the same
  scene gives 75.7 m³ — a 4 % swing from a UI pixel parameter that the CI does not
  see either.
* **Why:** the dominant error sources on this pipeline are (a) region selection in
  image space (parallax, rim band), (b) coverage gaps (39 of 83 m² flagged
  "unmeasured" yet the TIN integrates across them), (c) stereo depth noise/bias — none
  is resampled. Only datum-plane variance is, and it is small once the rim has 269+
  points. Percentiles of 50 replicates also make the 2.5/97.5 % tails single-sample
  estimates (`np.percentile` on 50 values interpolates between the 1st/2nd extreme).
* **Impact:** the UI now shows a tight "95 % CI" next to a number that is 2.5× further
  off than the interval half-width. This is worse than the old flat heuristic for
  decision-making (haul volumes, insurance).
* **Effort/risk:** 1–2 d, medium.
* **Smallest fix (keep the design, fix the honesty):** (1) add the coverage term —
  `unmeasured_area_m2 × max(|h|)` (or the raster/TIN disagreement) in quadrature;
  (2) also resample interior points by *block* bootstrap (resample raster cells, not
  points — spatially correlated noise) so stereo noise enters; (3) `B = 200` minimum
  and use a normal approximation (`1.96·σ_B`) instead of raw percentiles at B=50;
  (4) F4's factor 3. Report `est_volume_error_m3` as the max of CI half-width and the
  old heuristic until (1)–(3) are validated.
* **Proof:** calibration test across the 8 presets + 3 `rim_px` values: truth must fall
  inside `net_volume_ci95_m3` in ≥ 90 % of cases; unit tests for `bootstrap_volume_ci`
  (none exist today — `grep -rn net_volume_ci95_m3 tests/` returns nothing).

### F10 — CONFIRMED — Measurement runtime cliff hidden by F2 (Python per-cell loops)

* **Where:** `landslide/volume.py:491` (`_raster_bin` Python loop over cells, called
  once for the cross-check and **50×** inside `bootstrap_volume_ci` via `_raster_net`),
  `:554` (`_fill_small_holes` loop over gap cells), `:611-631` (50 × `fit_plane_robust`
  each running a 250-iteration RANSAC on ≤ 20 k rim points), and
  `landslide/viz.py:47-52` (`tripcolor` over every interior point).
* **Repro (probe, synthetic bowl):** `bootstrap_volume_ci(B=50)` = **5.8 s** at 16 k
  interior points, **49 s** at 157 k, **100 s** at 786 k; `_raster_bin` 0.09 / 0.84 /
  1.6 s. Today's measure takes 2.6 s only because the interior has 1 793 points.
* **Impact:** after F2, a normal measurement is ≈ 2 min of pure Python inside a worker
  that also holds the SGBM memory; two concurrent measures saturate the box.
* **Effort/risk:** 0.5 d, low.
* **Smallest fix:** vectorise `_raster_bin` (sort by cell, `np.add.reduceat` /
  `np.median` via `np.lexsort` + cumulative counts; MAD via a second pass); cap
  `fit_plane_ransac` iterations inside the bootstrap (`iters=60`, subsample 5 k);
  subsample `heat_topdown` to ≤ 100 k points.
* **Proof:** `bootstrap_volume_ci` on 500 k interior points < 5 s; `pytest --durations`.

### F11 — CONFIRMED — Five preset tests skipped; the benchmark shows real failures behind two of them

* **Where:** `tests/test_e2e_presets.py:129-161` (`oblique60`, `descending`,
  `collinear`, `lowtex`, `distorted` all `pytest.mark.skip`).
* **Repro:** `tools/benchmark.py` this audit (see §3): `oblique60` — ArUco never
  detected in any of 21 frames (`detect_marker_corners` returns `{}` for every image
  probed) → no scale → nothing measurable; `descending` — 20/21 registered but two
  cameras have wild intrinsics (`IMG_19` f = 4 157 px, `IMG_00` f = 1 494 px vs
  1 600 true) and sit 27 m / 13 m off their true positions, photo-mode error
  **59.9 %** with **no warning**; `lowtex` and `distorted` pass at 14.0 % / 16.9 %.
* **Impact:** the two presets modelling the most common field behaviours (walking
  down the slope; shooting close and oblique) are unpinned and one of them yields a
  confidently wrong volume.
* **Fix:** see R1.5 (intrinsic sanity gate) and R1.6 (marker); then un-skip and pin.

### F12 — CONFIRMED — No intrinsic-consistency gate; per-image self-calibration diverges on straight, parallel-axis paths

* **Where:** `landslide/sfm.py:321-324` (`CameraMode.PER_IMAGE` on every rung except
  the last), `:433-452` (`build_ctx` accepts whatever focal COLMAP converged to),
  `_attempt_score` (`:198-206`) scores only registration and track count.
* **Repro:** `nadir` (straight flight line, parallel optical axes): all 21 cameras
  converge to **f ≈ 2 362 px vs 1 300 true (+82 %)** with k₁ ≈ −0.06 — the
  focal/depth ambiguity of a pure translation; scene depth is stretched 1.8×, which is
  a large part of the "40.5 % ArUco scale error" that `architecture.md §7.1` attributes
  solely to the vertical marker. `descending`: per-camera focals 1 494 … 1 621 … 4 157
  accepted silently (see F11). `arc`/`collinear`/`oblique60` focals are within 1 %.
* **Impact:** drone-style and single-line phone captures — both explicitly targeted
  by the plan — can produce a geometrically wrong model that passes every gate. Real
  phone JPEGs carry an EXIF focal prior (T0.1), but bundle adjustment still refines
  it freely (`ba_refine_focal_length` default `True`), and one device produces one
  focal for the whole set.
* **Effort/risk:** 0.5 d, low–medium.
* **Smallest fix:** (a) after each attempt compute the per-camera focal spread; if
  the set is single-device (identical EXIF `Model`+`FocalLength`) and
  `max/min focal > 1.15`, treat the attempt as unusable and try `CameraMode.SINGLE`
  *next*, not last; (b) when the camera path is collinear (`_camera_plane_up`'s
  `collinearity < 0.1`) and an EXIF focal exists, set
  `IncrementalPipelineOptions.ba_refine_focal_length = False`; (c) always log the
  focal spread and put a warning in the result when it exceeds 15 %.
* **Proof:** `nadir` focal within 5 % of 1 300 px; `descending` all 21 focals within
  3 % of each other; benchmark scale error < 3 % on both.

### F13 — CONFIRMED — Benchmark harness is wrong for straight-line presets (and its memory column)

* **Where:** `tools/benchmark.py:31-40` (`_umeyama_scale` on camera centres only),
  used at `:75`, `:107-112`, `:122-129`; `:52,87,138` (`peak_rss_mb` =
  `ru_maxrss` delta; `ru_maxrss` is a process high-water mark, so the delta is ≈ 0
  for every preset after the first in one process, and *negative* if the generator
  subprocess ran first).
* **Repro:** `collinear` cameras register to 0.02 m residual, yet the "world" sparse
  cloud lands at z = 20–52 m with 69 % of points at y < 0: the recovered similarity is
  rotated ≈ 90° about the camera line (a free parameter for collinear centres). Hence
  "cloud RMS 31 m" and ortho "0 points inside the polygon" for `collinear`, and
  "cloud RMS 43.75 m"/56.9 % ortho for `nadir` (also a straight line) are **harness
  artefacts** — the photo-mode volume (16.6 % / 24.5 %), which does not use the
  harness transform, is the only valid column for those rows. `descending`'s
  "166.7 % scale error" is the Umeyama fit being dragged by the two outlier cameras.
* **Fix:** align with rotations as well (Umeyama on centres **plus** optical-axis
  endpoints `C + R^T·[0,0,1]`), use a robust (trimmed) fit, and take `peak_rss_mb`
  from `/usr/bin/time -v` or `resource.getrusage(RUSAGE_CHILDREN)` of a subprocess
  per preset. Regenerate `data/bench/results.md` (currently a single stale row with a
  different column set).

### F14 — CONFIRMED — Photo import runs synchronously on the asyncio event loop

* **Where:** `server/routes.py:50` (`async def create_job`) → `:84`
  (`import_photos`, PIL `exif_transpose` + LANCZOS resize + Laplacian on up to 200
  photos, tens of seconds).
* **Impact:** every other request (polls, SSE, other users) stalls for the whole
  import; on a 2-core field laptop with a 60-photo set that is ≈ 20–40 s of frozen UI.
* **Fix:** `await run_in_threadpool(import_photos, …)` (Starlette) — 1 line — or move
  import into `worker.run_reconstruction`.
* **Proof:** TestClient: while a 30-photo upload is in flight, `GET /api/jobs`
  responds in < 200 ms.

### F15 — CONFIRMED — T2.1/T2.2 have no unit tests

* `grep -rn "net_volume_ci95_m3\|volume_raster_m3\|unmeasured_area_m2\|bootstrap_volume_ci\|_fill_small_holes\|_raster_bin" tests/` → no matches. The raster
  cross-check, hole-fill enclosure logic, `unmeasured_area_m2`, the 10 % disagreement
  warning, and the CI are exercised only indirectly through e2e.

### F16 — PROBABLE — `estimate_up` returns the *slope* normal, not gravity, on hillsides

* **Where:** `landslide/densify.py:479-528`; consumers `volume.prism_volume:782-816`
  (rim steepness filter, rim-height sanity), `volume.slope_stats`, `ground.build_dsm`,
  `ortho.render_orthophoto`, `dem._gravity_R` (`dem.py:202`).
* **Scenario:** landslide on a 25° hillside, photos from a road below. The sparse
  cloud's dominant RANSAC plane is the hillside; `scene_up` = hillside normal
  (25° from gravity). Camera-plane up disagrees by > 20°, and the "ground-like vote"
  (`surface_filter` count) favours the hillside normal by construction. Result:
  "slope > 35 °" hazard area, `rim heights span … climb a slope/wall` warnings, the
  DSM/ortho "top-down" view and the DEM gravity seed are all tilted 25°; the prism
  integral itself is nearly invariant (volume is), so the *volume* survives but every
  slope/hazard/DEM number is relative to the hillside.
* **Why not confirmed:** the synthetic terrain is 5.7° tilted; no sloped preset exists.
* **Fix:** use a gravity source when present — EXIF `GPSImgDirection`/orientation is
  useless, but modern phones write `Xmp.Camera.*`/`MakerNotes` roll/pitch rarely; the
  practical route is (a) a `hillside` synthetic preset to quantify, (b) report
  `up_source ∈ {scene_plane, camera_plane}` and the angle between the candidates in
  the result, (c) let the user mark "horizontal" (two clicks on a level feature) or
  accept the marker board's normal (ArUco board placed level ⇒ board's in-plane axes
  give gravity — already triangulated in `marker_corners_m`).
* **Proof:** `hillside25` preset: `up` within 5° of true vertical.

### F17 — PROBABLE — Consensus tolerance uses the native focal, not the stereo working focal

* **Where:** `landslide/densify.py:284,299` (`fx_ref = ref.K[0,0]`; `step =
  depth² / (fx_ref·B)`), but SGBM ran at `stereo_width` (1280) so the true
  one-disparity depth step is `w_native/1280` × larger (2.3× for 3 000 px phone
  photos, 1.0× on the 1 200 px synthetic scene).
* **Impact:** on real photos the "2 × one-disparity step" band is 2.3× too tight →
  more consensus failures than the synthetic scene shows. Fix: `fx_ref *= s`.

### F18 — PROBABLE — Ray-cast crossing test is fragile on DSM holes and coarse steps

* **Where:** `landslide/ground.py:91,113` (`n_steps = 400`, `max_range = 3 × DSM
  span` → step = 0.2–1.5 m regardless of DSM cell), `:128` (crossing only when two
  *consecutive* samples are valid and bracket zero).
* **Scenario:** a grazing ray crosses the surface inside a NaN hole (or between two
  samples that straddle a ridge): the crossing is skipped and the ray "hits" the far
  slope, or never hits → today's 63 % fallback. Post-F2 this improves but the logic
  is still step-size dependent.
* **Fix:** step = `cell / 2`; treat a NaN run as "unknown" and accept a crossing when
  the last valid sample before it was above and the first valid after is below (with
  a max gap of a few cells). Test: DSM with 20 % random holes, hit-frac ≥ 0.95.

### F19 — PROBABLE — DEM alignment: in-thread, sparse-only, no yaw search

* **Where:** `server/routes.py:249-276` runs `align_to_dem` in the request thread on
  `job.ctx`; in the T3.1 design the parent process never builds a dense cloud, so
  `dem.py:197 ctx.cloud(dense=True)` silently returns the **sparse** cloud (7 k points
  including the 200 m outliers of F2, which pull the centroid seed at `dem.py:207`).
  `icp_rigid` has no rotation search about the vertical; gravity + centroid seeding
  leaves heading unknown.
* **Impact:** ICP converges only if the model's heading happens to be within ~30° of
  the DEM's; otherwise it locks to a wrong local minimum with a plausible `rms_m`.
* **Fix:** run DEM alignment in the worker (dense cloud), seed with a coarse yaw sweep
  (12 × 30° starts, keep the best trimmed RMS), and expose the ICP RMS/inlier fraction
  in the result. Test: `test_dem.py` case with a 120° heading offset.

### F20 — PROBABLE — Process/lifecycle gaps around the executor

* `server/executor.py:58` `shutdown(wait=False, cancel_futures=True)` on a shared
  pool cancels the *other* job's pending future — one crash fails both concurrent jobs
  (`BrokenProcessPool` is raised for every pending item anyway; document it).
* No cancel API: `DELETE /api/jobs/{id}` (`routes.py:418-428`) removes the directory
  under a running worker; the worker later recreates `artifacts/` (`pipeline.py:277`)
  and `state.json` writes fail in the callback thread.
* `concurrent.futures` atexit joins worker completion → `uvicorn` SIGTERM waits for a
  running SfM (minutes) — and forever after F1.
* `Job.save_state` (`jobs.py:56-69`) is unlocked; the worker-callback thread and a
  request thread can interleave `write_text` on the same `.tmp` → torn `state.json`.
* `load_persisted_jobs` (`jobs.py:228-235`) promotes an interrupted `reconstructing`
  job to `ready` whenever *any* `sparse/*/` file exists — i.e. a failed rung's model.
* `job_events` (`routes.py:136-165`) holds one threadpool thread per SSE client, never
  detects disconnect, and the `sent` index drifts when `job.log` is trimmed at 400.

### F21 — PROBABLE — Memory budget vs. the 2 GB cap

* Measured peak RSS (single process, 1 200 × 900 synthetic photos): dense-only run
  **1.19 GB** (`arc`), fresh SfM + dense **1.42–1.52 GB** (`descending`, `lowtex`).
  `architecture.md §3.9` itself states SfM feature extraction at 4 threads uses
  "~2–3 GB" on 7 MP phone photos. `ProcessPoolExecutor(max_workers=2)` therefore
  allows 2 × (1.5–3 GB) + parent ≈ 3.5–6.5 GB. SGBM `MODE_HH4` at 1280 × 960 with
  `numDisparities = 320` allocates ≈ 0.4–0.8 GB per `compute`, twice per pair (L→R and
  R→L), and F3's fix increases `numDisparities`.
* **Fix:** `max_workers = 1` when `os.sysconf` reports < 4 GB (or a `SLOPELENS_WORKERS`
  env), `SFM_THREADS = 2` under the same condition, and a hard `resource.setrlimit
  (RLIMIT_AS)` in the worker initializer so an OOM becomes a caught `MemoryError`
  (job error) rather than an OOM-kill (→ F1). Test: `tools/benchmark.py` with the
  fixed RSS column ≤ 1.8 GB per preset.

### F22 — PROBABLE — `surface_filter` removes head-scarps

* `landslide/densify.py:531-557` drops every point whose local normal is > 75° from
  `up`. A landslide's head-scarp and over-steepened flanks are exactly that, so the
  cloud has a hole where the cut is deepest and the TIN bridges it linearly (under-
  measuring cut). Consider `min_cos = 0.1` (84°) and reporting the dropped fraction
  inside the polygon.

### F23 — PROBABLE — ArUco under severe angles / small markers

* `oblique60` (2 m board, camera 10.4 m away, f 900 px): **zero detections in all 21
  frames**; `nadir`: detections from the flight-line edge only, 40 % scale error
  (partly F12). Real field markers are 20–30 cm, seen at 8–20 m by a phone → 25–60 px
  wide, corner refinement `cornerSubPix(win=5)` on a 2 200 px downscale
  (`scaling.py:39-47,60-66`), `rel_err` floor 0.5 %. Detection uses default
  `DetectorParameters` (no `adaptiveThreshWinSizeMax` tuning, no
  `cornerRefinementMethod = CORNER_REFINE_SUBPIX/APRILTAG`). Expect scale failure or
  3–8 % scale error (→ 9–24 % volume) on real sets; the multi-reference/inverse-variance
  design (T1.5 item 3) is still unimplemented.

### F24 — SPECULATIVE

* `densify.select_pairs` and `StereoConfig.max_pairs/per_image/rescue_per_image`
  are dead in the pipeline (only `tests/test_densify.py` calls `select_pairs`).
* `volume.prism_volume` relies on `datum_pts is rim` identity checks (T2.3 not done);
  `rim` is rebound inside the steepness filter, keeping it correct by accident.
* Uploaded file names are rendered via `innerHTML` in `steps/scale.js` /
  `result.js` (self-XSS only).
* `_photo_cache[key] = …` (`routes.py:186`) writes an `OrderedDict` outside
  `JOBS_LOCK`.
* `_recon_fingerprint` hashes poses and point count but not intrinsics.
* `MeasureRequest.polygon` has no vertex-count cap (a 100 k-vertex freehand trace
  makes `ring_distance` O(N·M)).

---

## 3. Benchmark-Coverage Gap Report

### 3.1 Preset results measured in this audit (`tools/benchmark.py`, one process per preset)

| preset | registered | scale err | photo vol err | ortho vol err | cloud RMS | runtime | peak RSS (`/usr/bin/time`) | validity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| arc (`data/bench`) | 21/21 | 0.99 % | **16.6 %** | 0.67 % | 0.10 m | 108 s (dense rebuilt) | 1.19 GB | valid |
| arc (`data/synth`, e2e) | 21/21 | 1.02 % | 12.6 % (rim 12 px) / 17.1 % (rim 14 px) | 1.5 % | — | 10 s (cached) | 0.35 GB | valid |
| sparse8 | 8/8 | 0.05 % | 22.7 % | 11.1 % | 0.36 m | 37 s | 0.32 GB | valid |
| lowtex | 21/21 | 0.53 % | 14.0 % | 3.1 % | 0.16 m | 241 s (fresh SfM) | 1.52 GB | valid |
| distorted | 21/21 | 1.03 % | 16.9 % | 10.9 % | 0.27 m | 7 s (cached) | 0.25 GB | valid |
| collinear | 21/21 | 0.48 % | 16.6 % | "0 points inside" | "31 m" | 15 s | 1.42 GB | photo valid; ortho/RMS = harness artefact (F13) |
| nadir | 21/21 | 40.5 % | 24.5 % | "56.9 %" | "43.8 m" | 20 s | 0.25 GB | scale/photo valid (F12 + vertical marker); ortho/RMS artefact (F13) |
| descending | 20/21 | "166.7 %" | **59.9 %** | "0 points inside" | nan | 198 s (fresh SfM) | 1.42 GB | photo error real (F11/F12); scale/ortho columns artefact |
| oblique60 | 21/21 | **no marker detected** | — | — | — | 3 s | 0.20 GB | real failure (F23) |

Compared with the numbers recorded in `architecture.md §7.1` / `IMPLEMENTATION_PROGRESS.md`:
photo-mode on `arc` is 12.6–17.1 % here (recorded "~7.7 %", later "12.6 %"); the
sparse8 and nadir rows reproduce exactly (same cached reconstructions), which also
means the nadir "cloud RMS 43.75 m" was never a pipeline measurement.

### 3.2 What the synthetic benchmark cannot see (field vs. synthetic)

| Field reality | Synthetic scene | Consequence for the audit |
| --- | --- | --- |
| 12 MP phone JPEGs, 3 000 px after import, EXIF focal, rolling shutter, auto-exposure/white-balance drift between frames, JPEG q≈90 re-encode | 1 200 × 900 PNG, exact pinhole (or k₁ only), constant illumination, no EXIF | SfM time/memory ≈ 6× higher in the field; F12 (focal) behaves differently with an EXIF prior; F17 is 2.3× worse; `_cull`'s exposure/blur gates never fire |
| Vegetation, shadows, wet mud, sky, moving people/vehicles, water | speckled earth texture only, terrain fills the frame | SIFT starvation and SGBM gaps only probed by `lowtex` (uniform texture scaling — not the same as *local* texture loss); the normal filter's effect on scarps (F22) is untested; no occluders |
| Hillside 15–40°, cameras from the road below or from above, uneven photographer walk | ≤ 6° terrain, perfectly regular arcs/lines at constant height (except `descending`) | F16 (up ≠ gravity) is untested; datum-ladder choice on a real curved rim untested beyond unit fixtures |
| 20–30 cm ArUco at 8–20 m, sometimes tilted, sometimes half-shaded | 2 m board facing the arc | scale error floor is unrealistic (0.5 %); F23 not exercised |
| 40–120 photos, 30–90 % overlap, sequential matcher, loop closure | 8 or 21 photos, exhaustive matcher | sequential/`loop_detection` path (`sfm.py:383-392`) has zero coverage; memory at n ≥ 45 untested |
| Real reference volumes (GNSS/TLS survey) | analytic cosine bowl | the plan's T3.4 (real-photo regression set) does not exist; there is **no evidence** the pipeline is within any tolerance on a real slide |

### 3.3 Coverage of the specific concerns raised

* **5 000 → 500 point threshold:** masks F2 (§2 F8).
* **Five skipped preset tests:** two hide real failures (§2 F11); the other three would
  pass at 14–17 % photo / 3–11 % ortho once the harness is fixed (F13).
* **Nadir marker / ArUco under angle:** F12 is the larger cause (focal +82 %), the
  vertical board the smaller; F23 for the field case.
* **TIN vs raster, 10 % threshold:** on the arc scene raster −78.0 vs TIN −78.6 m³
  (0.8 %), so the warning never fires while both are 17 % from truth — the two
  integrators share the same points and the same datum, so their agreement says
  nothing about coverage-driven bias. The 10 % threshold is untested (F15); the
  hole-fill's "row/column enclosure" rule is asymmetric for diagonal gaps
  (`volume.py:563-576`) and has no unit test.
* **Bootstrap CI validity / seeds:** deterministic (`seed=0`, `fit_plane_ransac`
  seed 12345) and reproducible run-to-run, but miscalibrated (F9).
* **Multi-view fusion / sparse-guided SGBM bounds / geometric gating:** the gating
  admits pairs the disparity cap then starves (F3); fusion tolerance (F17);
  `fusion_k` insensitivity reported in `IMPLEMENTATION_PROGRESS.md` is explained by
  F3 (extra neighbours are the wide-baseline ones that return nothing).
* **Cache invalidation on scale / polygon updates:** dense cache is correctly keyed
  to poses; `ortho.json`, `dem_info` and `result` are **not** invalidated on
  re-scale (F5). Polygon updates are stateless (each measure recomputes) — fine.
* **Process isolation / SSE / cancellation / resume:** F1, F6, F20.
* **CPU / memory peaks:** F21; SfM threads = 4 fixed, workers = 2 fixed.

---

## 4. Ranked List of Remaining Bottlenecks

1. **F1 executor deadlock** — server-wide outage after one crash; CI hangs.
2. **F2 voxel starvation** — the root of 7.3 k-point clouds, empty orthophotos,
   ground-frame fallback, "unmeasured 47 %" and the 12–17 % photo error.
3. **F12 unconstrained intrinsics on straight/parallel paths** — silently wrong
   geometry on drone lines and single-line walks; 59.9 % error with no warning.
4. **F9 + F4 overconfident uncertainty** — the number users will act on.
5. **F3 disparity cap** — halves the usable neighbour set for close/oblique captures.
6. **F5 stale ortho/DEM after re-scale** — silent wrong region in the normal
   correction workflow.
7. **F10 runtime cliff** (only after F2) — ~2 min per measure, Python-bound.
8. **F21 memory** — 2 workers × (1.5–3 GB) vs a 2 GB cap.
9. **F7 best-vs-disk model mismatch** — worse model on every difficult set.
10. **F16 up ≠ gravity on hillsides** — hazard/slope/DEM semantics.
11. **F23 marker robustness** — field-scale markers.
12. **F19 DEM alignment** (heading, sparse-only).
13. **F14 event-loop blocking import**, F6 lock during reload, F20 lifecycle.
14. **Frontend**: zoom/pan/vertex-edit never exercised in a browser (self-reported);
    SSE unused; ortho tracing impossible until F2.

---

## 5. Proposed Next Roadmap

### 5.1 Mandatory fixes (before any field use) — ≈ 4–5 engineer-days

| Stage | Fix | Files |
| --- | --- | --- |
| M1 | F1 executor deadlock; make the fast suite exit | `server/executor.py`, `tests/test_server.py` |
| M2 | F2 robust extent (+ cache key bump) **together with** F10 vectorised raster / bounded bootstrap, and re-pin e2e thresholds (F8) | `landslide/densify.py`, `landslide/volume.py`, `tests/test_e2e_synth.py` |
| M3 | F3 disparity range (cap → 1024 or adaptive downscale) | `landslide/densify.py` |
| M4 | F4 factor 3; F9 coverage term + block bootstrap + `B ≥ 200`; unit tests for T2.1/T2.2 (F15) | `landslide/pipeline.py`, `landslide/volume.py`, `tests/test_volume.py` |
| M5 | F5 invalidate ortho/dem/result on re-scale; store scale in `ortho.json` | `server/routes.py`, `landslide/ortho.py` |
| M6 | F12 intrinsic-consistency gate + focal lock on collinear paths with EXIF; F7 write best model to disk | `landslide/sfm.py` |
| M7 | F13 harness: pose-aware alignment, real RSS column; un-skip the 5 presets and pin | `tools/benchmark.py`, `tests/test_e2e_presets.py` |
| M8 | F21 worker/thread count from available RAM; `RLIMIT_AS` in worker init; F14 threadpool import | `server/executor.py`, `server/routes.py`, `landslide/sfm.py` |

### 5.2 High-value improvements — ≈ 5–8 days

| Stage | Improvement | Why it matters in the field |
| --- | --- | --- |
| H1 | F16: `hillside25` preset; `up_source` + candidate-angle in the result; optional "level the marker" (use the ArUco board's plane as gravity when the user says it is level) | correct slope/hazard/DEM semantics on real slopes |
| H2 | F23: ArUco `DetectorParameters` tuning (`cornerRefinementMethod`, adaptive threshold windows), detection at full resolution in a crop around the coarse hit, multi-reference scale with inverse-variance weighting (plan T1.5 item 3), scale error as a real 1σ from per-view PnP spread | 20–30 cm markers at 10 m; 3–8 % → 1–2 % scale |
| H3 | F18: hole-tolerant ray-cast; cell-sized steps; expose `hit_frac` and `region_method` in the UI | makes T1.2 actually the primary path; parallax-free tracing on oblique photos |
| H4 | F6 + F20: lock-free reload; `save_state` under a lock; `POST /api/jobs/{id}/cancel` (terminate the worker via a per-job `multiprocessing.Process` or a cancel flag file); mark partial `sparse/` as `error` on resume; SSE with disconnect detection (`request.is_disconnected()`), wire the client to it | operational robustness on a laptop in the field |
| H5 | F19: DEM alignment in the worker on the dense cloud, yaw sweep seed, RMS/inlier gate | prior-DEM differencing usable with arbitrary heading |
| H6 | F22: `min_cos` 0.25 → 0.1 and report the in-polygon dropped fraction; investigate keeping scarp points for the TIN but excluding them from the rim | cut volume on scarps |
| H7 | T3.4 real-photo regression set: ≥ 3 sites with a GNSS/TLS reference, stored outside git (LFS/URL), a `tools/benchmark.py --real` mode | the only way to claim a field accuracy number |
| H8 | Frontend: browser-driven smoke test (Playwright, optional dev dependency) for zoom/pan/vertex-drag/measure round trip; render the ortho with a k-NN splat so it is traceable even at 0.1 m spacing | the UI paths T3.2 shipped untested |

### 5.3 Experimental

* Learned features (ALIKED/DISK + LightGlue) behind a `torch` gate for low-texture /
  wet-mud sets (plan T2.4's optional item); measure on `lowtex` and real photos.
* Per-point `n_consistent` weight persisted in the npz and used as a weight in the
  raster median and the LoD map.
* OpenMVS/COLMAP-CPU PatchMatch backend (plan T2.5) — only if H1–H3 still leave
  photo mode > 10 % on the presets.
* IMU/gravity from the capture page (`capture.html` already runs in the browser;
  `DeviceOrientationEvent` could write a gravity vector per photo).

### 5.4 Deferred / rejected

* **Raster as the primary integrator (plan T2.1)** — rejected *for the right reason
  but on the wrong evidence*: the regression to 33–40 % was measured on the starved
  cloud. Re-evaluate only after M2; keep the TIN primary until then.
* **Symmetric outlier clip** — T0.4's asymmetric design is correct; keep.
* **`loop_detection` / vocabulary tree** — defer until a ≥ 40-photo real set exists.
* **Typing the response model (`MeasureResult`)** — nice-to-have, no correctness win.
* **Magnifier for manual scale** — skip; zoom/pan covers it.
* **GPU dense stereo** — out of scope (CPU-only target hardware).

---

## 6. Objective Acceptance Criteria

| Stage | Criterion (all must hold) |
| --- | --- |
| M1 | `timeout 120 pytest -q tests/test_server.py` exits 0; crash test passes; a second job submitted after a crash completes < 10 s; `timeout 300 pytest -q -k "not e2e"` exits 0 |
| M2 | arc dense cloud ≥ 150 k points, median spacing ≤ 5 cm; ortho covered cells ≥ 15 %; `hit_frac ≥ 0.9` and `region_method == "ground_frame"` on arc; `n_points > 5000` restored; measure wall time ≤ 15 s at 500 k interior points; `bootstrap_volume_ci` ≤ 5 s at 500 k |
| M3 | `stereo_pair` yield at baseline ratio 0.33 ≥ 50 % of ratio 0.16 on arc; unit test that the disparity window covers the sparse 1–99 % range |
| M4 | truth inside `net_volume_ci95_m3` in ≥ 90 % of {8 presets} × {rim 10/12/14 px} × {photo, ortho}; `est_volume_error_m3 ≥ |measured − truth|` in ≥ 90 %; scale term = 3 × `scale_rel_error` (unit test); ≥ 6 new unit tests covering `_raster_bin`, `_fill_small_holes` (enclosed vs. edge gap vs. diagonal gap), `bootstrap_volume_ci` (returns None < 15 rim pts; deterministic for fixed seed; widens with rim noise) |
| M5 | TestClient: scale → ortho → re-scale → `snapshot.ortho is None`, `POST /ortho` re-queues; ortho measure with mismatched stored scale → 409 |
| M6 | nadir focal within 5 % of 1 300 px; descending: 21/21 focals within 3 % of median, photo error < 25 %; a synthetic set with one 2× focal outlier logs a warning and is re-run with shared intrinsics; `reconstruct(reuse=True)` after a multi-rung ladder returns the best rung's model (test with mocked `_run_attempt`) |
| M7 | collinear/nadir cloud RMS < 0.5 m with the pose-aware alignment; all 8 preset tests un-skipped with pinned thresholds: photo ≤ 20 % (arc, lowtex, distorted, collinear, descending), ≤ 30 % (sparse8, nadir with a horizontal marker variant), ortho ≤ 10 %; `peak_rss_mb` column from a subprocess, ≤ 1.8 GB per preset |
| M8 | on a machine with `MemAvailable < 4 GB`: `max_workers == 1`, `SFM_THREADS == 2`; worker `RLIMIT_AS` set; an allocation past the limit yields job status `error` with a message, not a wedged executor; `GET /api/jobs` < 200 ms during a 30-photo upload |
| H1 | `hillside25` preset: `up` within 5° of gravity; slope stats within 3° of analytic |
| H2 | 25 cm marker synthetic variant at 12 m: detection in ≥ 80 % of frames, scale error ≤ 2 %; two-reference test halves `scale_rel_error` |
| H3 | `hit_frac ≥ 0.95` on a DSM with 20 % random holes; arc photo-mode error ≤ 8 % |
| H4 | cancel returns within 2 s and the worker process is gone; `uvicorn` shutdown < 5 s during a running SfM; concurrent `save_state` × 100 never yields invalid JSON; SSE client disconnect frees its thread within 1 s |
| H5 | DEM test with 120° heading offset aligns to < 0.1 m RMS |
| H7 | ≥ 3 real sites: |photo error| ≤ 20 %, |ortho error| ≤ 15 % vs GNSS/TLS reference, documented per site |

---

## 7. Final Go / No-Go Recommendation

**No-Go for real-world field deployment in the current state.**

Blocking reasons, each independently sufficient:

1. **F1** — one worker crash disables the server until restart, and the crash the
   design targets (native OOM/segfault in pycolmap/OpenCV) is likely on the 2 GB
   target with 2 concurrent workers (F21).
2. **F2/F3** — the dense cloud is ~50× sparser than designed; the orthophoto the user
   is supposed to trace on is 99.6 % empty; the "primary" ground-frame path never runs.
3. **F12** — straight-line and descending captures can yield a 60 % error with every
   gate passing and no warning.
4. **F9/F4** — the reported 95 % interval excludes the truth on the *best-case*
   synthetic scene; users would act on an overconfident number.
5. **No real-photo evidence at all** (T3.4 not started); the only accuracy figures
   are synthetic, on a nearly flat scene, with a 2 m marker.

**Conditional Go (limited pilot, expert-operated)** once M1–M8 are merged and their
criteria in §6 pass: ortho-mode measurements on ≥ 20-view convergent arcs with a
≥ 50 cm marker, results reported with the *widened* uncertainty from M4 and treated
as indicative (± 20 %) until H7 produces at least three real-site validations.
Photo-mode tracing, drone/nadir lines, single-line walks and hillside DEM
differencing stay experimental until H1–H5 land.

---

### Appendix — probe evidence summary (all read-only, scratchpad only)

* `exec_probe.py crash`: `done: {}` after 30 s, `pool broken: True`, faulthandler
  stack as quoted in F1; `exec_probe.py ok`: both submissions complete in 0.7 s.
* `dense_probe.py`: per-reference strict/loose neighbour counts 5–10 / 18–19;
  per-pair yields 337 k / 164 k / 47 k / 0 (F3); fused per-reference 34 k–147 k.
* sparse extent statistics (F2): ptp 177/85/214 m; p1–p99 30/14/30 m; dense n 7 299,
  spacing 0.138 m.
* `measure_probe.py`: photo cut 78.65 / truth 67.16 / CI [−82.27, −73.27] /
  `est_volume_error_m3` 5.37 / `unmeasured_area_m2` 38.6 / `region_method`
  `image_projection`; bootstrap 5.8 / 48.9 / 100.2 s at 16 k / 157 k / 786 k.
* `cloud_probe.py collinear`: 22 % of dense points inside the terrain bounds under
  the centre-only similarity, z-error 22–37 m, cameras register to 0.02 m → harness
  rotation ambiguity (F13).
* focal lengths (F12): nadir 2 354–2 417 px (true 1 300); descending 1 494 … 4 157
  (true 1 600); arc/collinear/oblique60 within 1 %.

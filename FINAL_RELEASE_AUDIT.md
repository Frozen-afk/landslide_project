# Final Release Audit — SlopeLens (landslide-volume)

**Audited tree:** `main` @ `bac51c5` ("add hole-tolerance validation"), clean working tree.
**Date:** 2026-09-23.
**Mode:** Read-only. No application code, test, config, or tracked data was changed. All measurement probes ran on scratch copies of `data/bench/*` and `data/jobs/*`.
**Environment:** Fedora, Python 3.14, 12 cores, 23 GB RAM, no CUDA.

## 0. Verdict

**NO-GO for release at `bac51c5`.**

Two regressions in the two most recent feature commits break core behavior:

1. `4fe5f98` breaks dense-cloud construction for every job that has no dense cache. Ortho rendering and dense measurement fail after several minutes of stereo work with `NameError: name 'cache' is not defined`.
2. `b2908e1` adds a lock-order inversion that can deadlock the whole server.

Three more defects also block release:

- DEM mode reports zero uncertainty and bridges unobserved voids. It is the only path that can report `status = ok`.
- The worker memory limit is smaller than the worker's import-time address space on small or many-core machines.
- The README tells operators to expect 1–8 % error from a single left-to-right sweep. The gates reject or downgrade that capture.

**After B1–B5 (§5) are fixed and a clean-cache e2e run passes:** conditional go for an expert-operated pilot only. Every result must be labeled `indicative` or `rejected` with its measured/upper range. DEM mode stays disabled or labeled experimental. Photo and ortho modes only, with two-azimuth capture.

**Unsupervised field use: NO-GO.** No real-site validation exists (H7). `status = ok` has never fired on photo or ortho mode (V1 not built). Scale uncertainty is underestimated on 3 of 6 scale-able presets.

## 1. Evidence base

| Source | What was checked |
| --- | --- |
| `architecture.md`, `README.md`, `implementation_plan.md`, `IMPLEMENTATION_PROGRESS.md` | Design claims and user guidance |
| `POST_IMPLEMENTATION_AUDIT.md`, `POST_AUDIT_PROGRESS.md`, `REMAINING_ACCURACY_PLAN/PROGRESS.md`, `SFM_STABILIZATION.md`, `POST_AUDIT_HIGH_VALUE_PLAN/PROGRESS.md`, `P1_CORRECTION_PLAN.md` | Claimed fixes, acceptance status, open items |
| `git log` / `git show` on `8c071ce`…`bac51c5` | What the P1–P6 commits actually changed |
| `landslide/{pipeline,gates,scaling,densify,volume,dem,ortho,ground,sfm}.py`, `server/{jobs,executor,routes,worker,main}.py`, `server/static/js/steps/result.js` | Implementation review |
| `pytest -q -k "not e2e"` | **176 passed, 1 skipped, 23 deselected** (28.5 s). Matches the P6 claim. The e2e suite was not run, because `test_descending_focal_spread_is_stable_across_clean_runs` deletes `data/bench/descending/work` in the repo. |
| Probe A (scratch): `reconstruct(reuse=True)` → `aruco_scale` → `measure` (photo and ortho) for all 8 presets | Status, reasons, ranges, CI, scale error vs. camera-centre Umeyama, coverage (§3) |
| Probe B (scratch): `server.jobs` with a fake `reconstruct` | Snapshot blocking and deadlock (§4.2) |
| Probe C (scratch): `dense_cloud(force=True)` + `measure` on `arc` under `RLIMIT_AS` | Memory limit and fresh-build path (§4.1, §4.5) |
| Probe D: synthetic `dem_volume` call | DEM-mode σ and void bridging (§4.3) |
| Probe E (scratch): `load_persisted_jobs` + `ensure_ctx` on the 32 saved jobs | Saved-job compatibility (§4.6) |

## 2. Benchmark preset classification

Ground truth: 67.16 m³ cut, on every preset. "Scale err" is the ArUco scale compared with the camera-centre Umeyama scale (Probe A, the cached SfM attempt now on disk). "Reported" is the `scale_rel_error` in the result after G7.

| Preset | Registered | Scale err (actual / reported) | Photo: status, cut_measured–cut_upper, CI95 (net) | Ortho: status, range | Coverage | Class |
| --- | --- | --- | --- | --- | --- | --- |
| arc | 21/21 | 1.25 % / 1.24 % | indicative, 49.9–142.8, [−90.5, −9.4] ✓ | indicative, 49.7–139.5 ✓ | 0.61 | **Conditionally supported** |
| lowtex | 21/21 | 0.46 % / 1.24 % | indicative, 50.3–142.7, [−90.8, −9.8] ✓ | indicative, 49.4–137.5 ✓ | 0.61 | **Conditionally supported** |
| distorted | 21/21 | 3.34 % / 3.0 % | indicative, 51.1–163.2, [−92.9, −9.1] ✓ | indicative, 51.2–170.2 ✓ | 0.62 | **Conditionally supported** (scale slightly under-reported) |
| collinear | 21/21 | 2.80 % / 1.19 % | rejected (G4 + G6 58 %), 46.8–153.0 ✓ | rejected, 46.8–149.4 ✓ | 0.59 | **Safely rejected** |
| sparse8 | 8/8 | 3.56 % / 1.13 % | rejected (G6 48 %), 40.9–163.8 ✓ | rejected, 45.5–173.0 ✓ | 0.48 | **Safely rejected** |
| descending | 20/21 | **−15.1 % / 1.53 %** | rejected (G5 run 10 + G6 55 %), 38.3–128.6 ✓ | rejected, **12.9–63.8 ✗ truth outside range; CI [−34.6, 9.3] excludes truth** | 0.55 / 0.40 | **Safely rejected — by coverage, not by detecting the 15 % scale error** |
| nadir | 21/21 | refused: marker sides disagree by 38 % | — | — | — | **Safely rejected** (G2). Dense-stereo empty-cloud bug (A8) also still open. |
| oblique60 | 21/21 | refused: no marker found in any photo | — | — | — | **Safely rejected**. Cause is preset framing: the marker is out of frame in 19/21 views. |
| (two-azimuth `twosided`) | not built | — | — | — | — | **Supported: none demonstrated** |
| hillside / sloped scene, horizontal-marker nadir, real phone photos (`s < 1` detection path), DEM differencing | no preset | — | — | — | — | **Unsupported / unvalidated** |

Summary:

- **Supported (`ok`):** none. G6's `ok` branch has never fired in photo or ortho mode.
- **Conditionally supported (`indicative` with range):** arc, lowtex, distorted. Truth lies inside `[cut_measured, cut_upper]` and inside CI95 on both modes. The range is wide: about 50–140 m³ for 67 m³ true, and `est_volume_error_m3` is about ±80 % of the measured cut.
- **Safely rejected:** collinear, sparse8, descending, nadir, oblique60. No preset produced a confident wrong number with a favorable status.
- **Unsupported:** sloped/hillside scenes, drone nadir with a ground marker, real field photos, DEM mode.

## 3. Area findings

### 3.1 Measurement accuracy
- A1 honest bridging holds. The measured cut is 24–43 % below truth on scale-able presets, by design. On every probed run except descending-ortho, `cut_measured ≤ truth ≤ cut_upper` holds.
- The coverage ceiling for single-arc captures is 0.48–0.62. This is a capture-protocol limit, not a code defect. It is the dominant error term.

### 3.2 Uncertainty bounds
- **Scale uncertainty is underestimated on 3 of 6 scale-able presets:** collinear 2.4×, sparse8 3.1×, descending 10×. Cause: G7 (`pipeline.py:282-284`) drops the 3 % floor when the per-view PnP cross-check spread is ≤ 2 %. PnP reuses the same SfM poses and intrinsics, so it is not independent of SfM geometry error. On descending, the SfM geometry is 15 % off and PnP agrees with it.
- Volume CI95 still contains truth where coverage error dominates. It fails on descending-ortho, where both the range and the CI exclude truth. That preset is rejected, so no user sees a favorable status. On a better-covered capture with the same SfM distortion, however, the result would be `indicative` with a too-narrow scale term.
- **DEM mode:** σ is identically 0 (§4.3), so the reported uncertainty is the scale term only.

### 3.3 SfM stability
- The focal-lock retry reachability and selection fix (`SFM_STABILIZATION.md`) is present. It is unit-tested (`tests/test_sfm.py`, 3 cases) and was e2e-verified in that pass. Final focal spread is 1.00–1.04 on every preset in Probe A.
- COLMAP non-determinism remains. The cached `descending` attempt has a 15 % scale error and a stale `results.md` row (0.52 %). No gate detects scale-inconsistent SfM geometry when the focal spread is clean. G3 and G4 are silent on descending.

### 3.4 Coverage gates
- G1–G6 behave as documented on every preset (reasons match §2).
- The G6 reason text says "the volume interpolates across it" (`gates.py:94`). Since A1 the volume excludes the gap. The upper bound covers it. The wording is inaccurate.
- G6 `ok` (≥ 0.85) is unexercised. V1 (`twosided` preset) was approved but never built. The threshold is uncalibrated against any capture that should pass.

### 3.5 Orthophoto gaps
- The P1 splat plus the correction is present (`ortho.py`). Fabricated fill is 0 and `bg_poly − gap` is 0.005–0.017. `ortho.json` is unchanged. Render time is 0.53–0.99 s in Probe A.
- The speckle target (≤ 1 %) is missed on 4/6 presets (0.0104–0.0203). This is cosmetic and documented. Genuine gaps stay visible, which is correct.

### 3.6 Marker safety
- `aruco_scale` refuses when side spread > 10 %, when the reprojection-implied error > 10 %, and when fewer than 2 views see the marker. The nadir and oblique60 refusals were reproduced.
- The detector change (SUBPIX, not APRILTAG) was A/B-neutral on 6 presets. The full-resolution `s < 1` refinement path runs only in a unit test. No benchmark image is large enough to trigger it, so it is untested on real phone photos.
- The benchmark marker is a 2 m board. The README tells users to print a 0.25 m marker. No evidence exists for small markers at field distances.

### 3.7 DEM alignment
- The yaw sweep and dense-cache reuse are present and tested (`tests/test_dem.py`, 9 cases). `align_to_dem` never builds a dense cloud.
- DEM **volume** (`dem_volume`) was not updated by RC1/A1/G6. See §4.3.

### 3.8 Server crash recovery
- F1 BrokenProcessPool recovery, `shutdown_now()`, the busy-guarded DELETE, locked `save_state`, and `_model_complete` resume are present and tested (`tests/test_server.py` 22, `tests/test_jobs.py` 5).
- New deadlock introduced by the same commit: §4.2.
- The status poll still blocks for the whole context reload. The F6/S4 fix is ineffective: §4.4.

### 3.9 Memory behavior
- Benchmark peak RSS is 0.34–1.1 GB per preset (`results.md`, Probe A). The earlier 16 GB `eval_tps` spike is fixed.
- The `RLIMIT_AS` sizing is wrong for the stated small-laptop target: §4.5.

### 3.10 Saved-job compatibility
- All 32 saved jobs reload. The 2 real jobs (2026-08-16 and 2026-08-30) rebuild their context in 0.1–0.6 s. They keep their manual scale and their old `ortho.json` (no `scale` key, which the mismatch check tolerates).
- The old dense cache is `dense_1280_<fp>.npz` (pre-v2), so the next dense use rebuilds it. That rebuild hits B1 and fails. Saved jobs therefore cannot be re-measured with the dense cloud at HEAD.
- Legacy results have no `status` or `reasons`. `result.js` shows them without any gate label, so pre-A1 numbers (interpolated, F4/F9-era uncertainty) look un-gated.

## 4. Confirmed defects (new in this audit)

### 4.1 Dense-cloud build raises `NameError` (regression, `4fe5f98`)
`densify.py:795` calls `np.savez_compressed(cache, ...)`, but P5(b) moved `cache = ...` into `load_cached_dense` (`densify.py:672`). Any uncached or `force=True` build runs full stereo and then raises.
Reproduced: Probe C, `arc`, `force=True` → `NameError name 'cache' is not defined` after 151 s.
Impact:
- Every new job's first `POST /ortho` or dense `POST /measure` fails.
- Every saved job with a pre-v2 cache fails the same way.
- Every fresh benchmark run fails.
Not caught because every validation since P5 used pre-existing v2 caches, and no e2e run followed P5.

### 4.2 Server deadlock: lock-order inversion (regression, `b2908e1`)
- `Job.ensure_ctx` takes `job.lock`, then `JOBS_LOCK` (via `evict_ctx`, `jobs.py:144`).
- `evict_ctx` takes `JOBS_LOCK`, then another job's `job.lock` (via `save_state`, now locked, `jobs.py:68,214`).
- When two contexts load concurrently with at least 2 already loaded, the two threads can wait on each other forever. `JOBS_LOCK` then stays held, so `list_jobs`, `touch`, the log-drain thread, and every poll hang.

Reproduced: Probe B, deadlock on trial 3 of 3 under forced interleaving.
Before P3b, `save_state` was unlocked, so this inversion did not exist.

### 4.3 DEM mode: zero uncertainty, bridged voids, can report `ok`
- `dem_volume` (`volume.py:323-410`) computes `sigma` from `h[simp][keep_tri].mean(1) - h_tri`. `h_tri` is that same mean, so `datum_rms_m`, `lod_m` and `est_volume_error_m3` are always 0.
- The bridging cull is still `max(20·spacing, 0.5·diameter)`. The RC1 fix was applied only to `prism_volume`.
- No `coverage_frac` is produced, so G6 never runs.

Probe D: a 100 m² region with a 28 m² unobserved hole reports `area_m2` 99.2 (72 observed), σ = 0, error = 0, and no warning.
In ortho + DEM mode, only G1–G4 apply, so the result can be `status = ok`. This is the only path in the product that can currently emit `ok`.

### 4.4 Status poll blocks during context reload (F6/S4 not actually resolved)
`job_status` starts a background reload and then calls `snapshot()`. `snapshot()` takes `job.lock`, which the reload thread holds for the whole `reconstruct()` call.
Probe B: `snapshot()` returned after 3.00 s against a 3 s fake reload.
Pre-existing (`8ea3551` has the same lock in `snapshot`). Impact is UX only.

### 4.5 Worker `RLIMIT_AS` below import-time address space
The import-time virtual size of `landslide` + cv2 + pycolmap + scipy:
- 1.2 GB with 1 BLAS thread
- 1.9 GB with 4 BLAS threads
- 3.9 GB with 12 BLAS threads

RSS is about 150 MB in all three cases.

`executor._worker_mem_limit_bytes` gives 1.6 GB on a < 4 GB machine (1 worker), and 3.2 GB on an 8 GB machine (2 workers).

Probe C results:
- 1.6 GB limit, 12 threads: the process core-dumps in OpenBLAS thread creation. This is a native crash, so the job fails with "worker process crashed".
- 1.6 GB limit, 1 thread: `OpenCV Insufficient memory`.
- 3.2 GB limit, 1 thread: runs until the §4.1 `NameError`.

Consequence: on the small field laptop that M8 targets, every heavy job fails. An 8 GB laptop with ≥ 12 hardware threads is also at risk. The 23 GB dev box is unaffected (limit about 9.4 GB).

### 4.6 Minor
- `delete_job` releases `job.lock` between the busy check and `rmtree` (`routes.py:457-465`). There is a narrow TOCTOU window with a concurrent measure POST.
- A rescale during a running measure or ortho is allowed (`_get_ready_job` has no busy check). The finishing measure then stores a result computed at the old scale.
- `tests/test_server.py` writes jobs into the real `data/jobs/`. This audit's fast-suite run left `data/jobs/20260923-120058-07b450`.
- `multiprocessing.Manager` drain thread prints `EOFError` at interpreter exit. Cosmetic, pre-existing.
- Stale docs:
  - `architecture.md` §3.9 says `ThreadPoolExecutor(2)` and `eval_tps(chunk=100_000)`.
  - `architecture.md` §7.1 still says the descending focal test "can fail".
  - The `results.md` descending row does not match the cached work dir.

## 5. Deployment blockers vs. non-blocking limitations

### Release blockers (must fix before any deployment, including a pilot)
| ID | Blocker | Evidence | Minimum fix scope |
| --- | --- | --- | --- |
| B1 | Fresh dense build `NameError` | §4.1 | Restore `cache` path in `dense_cloud`. Add a fast test for an uncached `dense_cloud` save. Re-run the e2e suite from clean caches. |
| B2 | `ensure_ctx` / `evict_ctx` deadlock | §4.2 | Collect eviction candidates under `JOBS_LOCK`, then `save_state` outside it. Add a regression test for concurrent reloads. |
| B3 | DEM mode: σ ≡ 0, RC1 bridging, no G6, can emit `ok` | §4.3 | Fix σ, apply the A1 cull and G6 to `dem_volume`. Otherwise disable DEM mode or force `indicative`. |
| B4 | `RLIMIT_AS` smaller than import-time VM on small or many-core hosts | §4.5 | Size the limit from measured baseline VM (or use RSS or cgroup limits). Cap BLAS threads in workers. Test on a < 4 GB host. |
| B5 | README promises 1–8 % error and a single-sweep protocol | `README.md:6,13-14,268,286-287` | Rewrite accuracy and capture guidance to match gates: two azimuths, ranges, statuses. |

### Blockers for unsupervised field use or any accuracy claim
| ID | Item |
| --- | --- |
| F1 | No real-site validation (H7). All evidence is synthetic, from one bowl scene with a 2 m marker. |
| F2 | `status = ok` never demonstrated in photo or ortho mode. V1 (`twosided`) not built. G6's 0.85 threshold is uncalibrated. |
| F3 | Scale uncertainty under-reported (G7 PnP bypass not independent of SfM), up to 10× on descending. |

### Non-blocking, documented limitations
- The single-arc coverage ceiling (48–62 %) makes every result `indicative` or `rejected`.
- Ortho speckle is 1.0–2.0 % on 4/6 presets.
- oblique60 preset framing and the nadir vertical marker. A8 (nadir empty dense cloud) and A7 (EXIF focal lock) are not implemented.
- The up-vector on sloped terrain is reported (`up_source`, gate > 20°) but not validated. Ortho results lack `up_source`.
- No cancel endpoint. SSE has no client.
- Status poll blocks during reload (§4.4).
- Legacy saved results display without a status.
- COLMAP run-to-run non-determinism.
- The `s < 1` marker refinement is only unit-tested.
- Minor server races and the test data leak (§4.6).
- Doc drift (§4.6).

## 6. Real-world field validation requirements (before any accuracy claim)
1. **≥ 3 real sites** with GNSS/TLS or pre/post-DEM reference (H7). Report per site: `cut_measured`, `cut_upper`, CI95, status, coverage, and the reference volume. Pass condition: truth inside `[cut_measured, cut_upper]` and CI95 on every site. At least one two-azimuth capture must reach `ok` with |error| ≤ 15 % (ortho) and ≤ 20 % (photo).
2. **Two-azimuth protocol trial** on each site, plus a single-arc capture as a control. This confirms that G6 separates them.
3. **Real markers:** 20–30 cm and ~50 cm prints at 5–15 m, on 3000–4000 px phone photos. This exercises the `s < 1` full-resolution refinement. Also cover manual scale against a taped reference. Record the actual scale error against GNSS baselines, to test G7.
4. **Sloped site** (≥ 20° hillside): compare `up_source` / `up_disagree_deg` and slope stats to a surveyed gravity reference.
5. **DEM mode** (after B3): a real prior DEM with a known change volume.
6. **Hardware:** a full job on the target field laptop (≤ 4 GB and 8 GB, real core counts), with the worker memory limit active. Record peak RSS and VmSize.
7. **Server soak:** ≥ 3 jobs, concurrent polling, reloads above `MAX_LOADED_CTX`, restart mid-SfM, and delete or rescale while busy.

## 7. What was verified as correct
- Fast suite is green (176/1 skipped). P1–P6 claims match the code.
- The gate logic matches `architecture.md` §3.8a. Every rejected preset is rejected for a named, correct reason.
- A1 measured-only volume and the range criterion hold on all photo-mode presets.
- Marker refusal paths work (nadir, oblique60).
- `ortho.json` compatibility holds. `select_region_ortho` reads only meta.
- Focal-lock retry is effective (final spread ≤ 1.04 on every preset).
- Saved jobs reload and keep their scale.
- DEM yaw sweep and dense-cache preference work as tested.
- `shutdown_now`, DELETE 409, torn-JSON protection and `_model_complete` work as tested.

## 8. Recommendation
**NO-GO at `bac51c5`.** B1 alone disables the core ortho and dense-measure workflow for every new job. B2 can hang the server.

**Path to conditional GO (expert pilot):**
1. Fix B1–B5.
2. Run the full e2e suite from **deleted** caches.
3. Regenerate `data/bench/results.md`.
4. Run the §6 item 6 hardware test.

Then pilot photo and ortho modes on two-azimuth captures, with every result reported as a range plus status. DEM mode stays off until B3 is fixed and it passes §6 item 5.

**Unsupervised field deployment:** NO-GO until F1–F3 are closed.

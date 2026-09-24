# Release Blocker Progress

Tracks fixes for the blockers in `FINAL_RELEASE_AUDIT.md` §5. B1–B4 are now covered.

## B1 — Fresh dense build `NameError`

**Status: FIXED.**

### Root cause

`densify.py`'s `dense_cloud()` builds a fresh dense cloud and, at the end, saves it:

```python
np.savez_compressed(cache, points=pts, colors=cols, fingerprint=ctx.fingerprint)
```

`cache` was never assigned in `dense_cloud()`. It used to be a local variable there before the P5(b) refactor moved the cache-path computation into the new `load_cached_dense()` helper (`densify.py:672`) and left the save line in `dense_cloud()` pointing at a name nothing defined anymore. `load_cached_dense()`'s own `cache` local is scoped to that function and never leaks into its caller.

Every path that reaches the save line — a job with no cache yet, `force=True`, or a stale/mismatched cache — raises `NameError: name 'cache' is not defined` after paying for full multi-view stereo. Every path that finds a valid cache first returns before that line, which is why the fast suite and every prior benchmark run (using pre-existing caches) never hit it.

### Fix

`densify.py:693-694` — compute `cache` at the top of `dense_cloud()`, using the exact same path formula `load_cached_dense()` reads (`dense_{stereo_width}_v2_{fingerprint}.npz` under `ctx.workdir`), before the cache-hit check:

```python
cfg = cfg or StereoConfig(max_pairs=max_pairs)
cache = ctx.workdir / f"dense_{stereo_width}_v2_{ctx.fingerprint}.npz"
if not force:
    ...
```

One-line fix, no behavior change to the cache-hit path or the cache key itself — only restores the variable the existing save call already expected.

### Regression coverage

`tests/test_densify.py::test_dense_cloud_uncached_build_saves_cache` (new). Drives the real `dense_cloud()` end-to-end with the heavy stereo/covisibility internals stubbed out (they need real imagery), so it stays fast (~0.1 s) while exercising the exact code path that crashed: builds a fresh cloud, saves it, and confirms `load_cached_dense()` reads back the same file. Verified this test fails with the original `NameError` when run against the pre-fix code (confirmed by stashing only the `densify.py` change) and passes with the fix.

### Validation

**Fast suite:** `pytest -q -k "not e2e"` — 177 passed, 1 skipped, 23 deselected (was 176/1/23; +1 is the new regression test). No regressions.

**Focused e2e (clean cache, new-job simulation):** scratch copy of `data/bench/arc` with its dense cache deleted, driving the same two calls a real job makes (mirroring `server/worker.py`'s `run_ortho` then `run_measure`, as two separate `reconstruct(reuse=True)` calls the way separate worker processes would do it):

1. `reconstruct(reuse=True)` → `aruco_scale` → `dense_cloud()` (no cache present — this is the exact B1 path) → `render_orthophoto()`.
   Result: fresh dense build completed (201,406 points, 21/21 views), cache file written, orthophoto rendered (1401×1262 px). No `NameError`.
2. Second `reconstruct(reuse=True)` (fresh ctx, as a separate worker process would have) → dense cache loaded from disk → `measure(mode="ortho", dense=True)`.
   Result: `status="indicative"`, `cut_volume_m3=49.74` (61% coverage, one unmeasured 38.9 m² patch — expected/documented behavior per audit §3.1, not a defect). Matches the audit's own probed range for `arc` (49.9–142.8 m³).

Both steps of the ortho + dense-measure workflow completed successfully end-to-end for a job with no prior dense cache.

### Deviations from the audit's suggested fix scope

- Audit's minimum fix scope also said "Re-run the e2e suite from clean caches." The full `pytest tests/test_e2e_presets.py tests/test_e2e_synth.py` suite was not run (each preset takes minutes of SfM+stereo; full suite is tens of minutes). Instead, one focused clean-cache validation was run directly against `dense_cloud`/`render_orthophoto`/`measure`, which exercises the identical B1 code path this blocker is about. This was a deliberate scope decision, not an oversight — the task instructions asked for one focused end-to-end validation, not the full slow suite, and explicitly excluded B2-B5/F1-F3.
- `tests/test_e2e_presets.py::test_descending_focal_spread_is_stable_across_clean_runs` still deletes `data/bench/descending/work` in the repo tree when run (a pre-existing issue noted in the audit, unrelated to B1) — not touched here, and not run as part of this validation (the validation used a scratch copy, not the tracked `data/bench/` tree).

### Acceptance

B1 is fixed and regression-tested. Ortho and dense-measure workflows complete for a new job. B2–B5 and F1–F3 remain open per `FINAL_RELEASE_AUDIT.md` — this document will gain a section per blocker as each is worked.

## B2 — `ensure_ctx` / `evict_ctx` deadlock

**Status: FIXED.**

### Root cause

Two locks, taken in opposite orders by two code paths:

- `Job.ensure_ctx()` (`server/jobs.py:107-145`) takes `self.lock`, then — while still
  holding it — calls `evict_ctx()`, which takes `JOBS_LOCK`. Order: `job.lock → JOBS_LOCK`.
- The old `evict_ctx()` (`server/jobs.py:205-218`) took `JOBS_LOCK` and, still holding
  it, called each eviction candidate's `save_state()`, which takes that job's
  `job.lock`. Order: `JOBS_LOCK → job.lock`.

With `MAX_LOADED_CTX` contexts already loaded, two jobs reloading concurrently —
each an eviction candidate for the other's `evict_ctx()` call — can wait on each
other forever: thread A holds job1's lock and blocks on `JOBS_LOCK` (held by
thread B); thread B holds `JOBS_LOCK` and blocks on job1's lock (held by thread
A). `JOBS_LOCK` stays held for good, so every other route that touches it
(`list_jobs`, `touch`, the log-drain thread, every status poll) hangs too —
confirmed matches the audit's Probe B (§4.2).

Reproduced first: stashed the fix, ran the new regression test below against
unmodified `server/jobs.py` — the two threads never rejoin and the test process
has to be killed after a 30 s timeout (both are non-daemon and stay parked on
each other's lock forever). Restored the fix and re-ran: passes in ~1.4 s.

### Fix

`server/jobs.py`, `evict_ctx()`: gather the eviction candidates under
`JOBS_LOCK`, then release it before touching any job's lock (this alone
restores `ensure_ctx`'s ordering, matching the audit's suggested scope). Went
one step further for full safety: each candidate's `job.lock` is a
**non-blocking** `acquire(blocking=False)` — a job that's genuinely in use
right now is simply left loaded and retried on the next eviction, instead of
being waited on. This also closes a second, harder-to-hit deadlock the
suggested fix alone doesn't: two `evict_ctx()` calls picking each other's job
as a candidate at the same time (an `AB↔BA` cycle on the two `job.lock`s
directly, with `JOBS_LOCK` no longer involved). Since eviction is already
documented as best-effort ("reloadable on demand"), skipping a momentarily
busy candidate changes no user-visible behavior.

No change to `ensure_ctx`, to the candidate-selection logic (still LRU,
still skips busy statuses), or to what gets logged/saved on a successful
eviction.

### Regression coverage

`tests/test_jobs.py::test_concurrent_ensure_ctx_does_not_deadlock_on_eviction`
(new). Forces the exact interleaving from §4.2 with a real `job_a.ensure_ctx()`
(fake `reconstruct`) racing a second thread that reproduces the other side of
the old lock order (`job_b.lock` held, then `evict_ctx()` — job_a is made the
older LRU candidate so it's the one evict_ctx must lock). An `Event` set at
the start of the (monkeypatched) `evict_ctx()` call guarantees job_a's ctx is
already assigned and its lock already held before the second thread starts,
and a 0.2 s delay on thread A's side gives thread B time to reach (and,
pre-fix, block on) job_a's lock first. Verified failing (hang) against the
pre-fix code and passing against the fix, as described above.

### Validation

**Fast suite:** `pytest -q -k "not e2e"` — 178 passed, 1 skipped, 23 deselected
(was 177/1/23 after B1; +1 is the new regression test). No regressions. (The
`_drain_log_queue` `EOFError` printed at interpreter exit is the pre-existing,
documented cosmetic issue from audit §4.6, not new.)

**Focused validation:** the regression test above *is* the focused
reproduction + fix validation for this blocker — B2 is a concurrency defect
with no synthetic-preset or e2e angle (`architecture.md`'s server section and
`tests/test_server.py` don't exercise concurrent `ensure_ctx`/`evict_ctx`
races), so a second, separate check would just be the same test again. No
further e2e run was needed or attempted.

### Deviations from the audit's suggested fix scope

- Implemented a stronger fix than "collect eviction candidates under
  `JOBS_LOCK`, then `save_state` outside it" alone: that phrasing (candidates
  collected, then locked one at a time outside `JOBS_LOCK`) still uses a
  *blocking* acquire on each candidate's `job.lock`, which reopens a direct
  `job.lock`-vs-`job.lock` deadlock between two concurrent `evict_ctx()` calls
  that pick each other's job as a candidate (no `JOBS_LOCK` involved in that
  cycle, since it's released before the loop). The non-blocking acquire used
  here removes that cycle too, at no behavior cost given eviction is already
  best-effort. This is a deliberate strengthening, not a scope departure —
  same file, same function, no new abstractions.

### Acceptance

B2 is fixed and regression-tested. `evict_ctx()` no longer blocks on any
job's lock while holding `JOBS_LOCK`, and no longer blocks on any job's lock
at all — removing both the audit-identified deadlock and the related
candidate-vs-candidate cycle the minimal fix would have left open. B3–B5 and
F1–F3 remain open per `FINAL_RELEASE_AUDIT.md`, out of scope for this pass.

## B3 — DEM mode: σ ≡ 0, RC1 bridging, no G6, can emit `ok`

**Status: FIXED (σ and bridging cull fixed directly; G6 addressed via the
audit's documented fallback — forced `indicative` — not by wiring a real
`coverage_frac`; see Deviations).**

### Root cause

Three independent defects in `landslide/volume.py::dem_volume` (`:323-414`
pre-fix), confirmed by direct reproduction against the function (script run
under `.venv`, not the e2e presets — see Validation):

1. **σ ≡ 0.** After the RC1/A1 bridging cull, `h_tri` was reassigned to
   `h[simp].mean(axis=1)` restricted to kept triangles (`:374`). The sigma
   line then recomputed `h[simp][keep_tri].mean(axis=1)` — the exact same
   per-triangle mean, by the exact same formula — and diffed it against
   `h_tri`, i.e. against itself. The residual was `0.0` by construction for
   every input, so `datum_rms_m`, `lod_m`, `lod_max_m`, and
   `est_volume_error_m3` were always 0 regardless of real surface noise.
   Reproduced: a flat DEM against a surface with visible 3 cm noise
   (`0.05·sin(x) + N(0, 0.03)`) reported `datum_rms_m = 0.0`.
2. **Bridging cull used the pre-RC1 formula.** `max_edge = max(20·spacing,
   0.5·region_diameter)` (`max_edge_region_frac=0.5`) — the exact "×
   diameter" term `prism_volume`'s docstring says RC1/A1 *removed* in favor
   of an absolute cap (`prism_volume`'s own default is `max_edge_abs_m=0.5`
   **metres**, not a diameter fraction). On any scene wider than ~1 m, `0.5
   × diameter` is tens of metres, so the cull essentially never fires and
   the TIN bridges large unobserved voids with long triangles exactly as
   RC1 was written to stop. Reproduced: a dense point grid (0.12 m spacing,
   realistic dense-cloud density) with a 64 m² hole punched out measured
   ~383 m² against a ~336 m² real footprint under the pre-fix formula — the
   hole was bridged, not excluded.
3. **No `coverage_frac`.** `dem_volume`'s signature never accepted `up` /
   `polygon_ground`, and its two call sites (`pipeline.py:192-193,236-237`)
   never passed them even though both have `up`/`polygon_ground` in scope
   (used for `prism_volume` a few lines below in the same function). With
   no `coverage_frac` in the result, `gates.py`'s G6 branch (`cov =
   res.get("coverage_frac")`) is always skipped — DEM mode is the only path
   where no gate can downgrade a result below `ok`.

### Fix

`landslide/volume.py::dem_volume`:
- Renamed `max_edge_region_frac` → `max_edge_abs_m` (default `0.5`, metres)
  and changed `max_edge = max(max_edge_factor·spacing, max_edge_region_frac·diam)`
  to `max_edge = max(max_edge_factor·spacing, max_edge_abs_m)` — now
  identical in form to `prism_volume`'s already-shipped, audit-verified
  cull. No caller passed the old keyword by name (checked: `pipeline.py`,
  `change.py`, `tests/test_dem.py` all call positionally/by other kwargs),
  so this is not a breaking rename.
- Sigma now measures each kept triangle's own vertex spread around its own
  mean height (`verts_h = h[simp[keep_tri]]`; `sigma = rms(verts_h -
  verts_h.mean(axis=1, keepdims=True))`) — a real local-roughness estimate,
  computed *before* `h_tri`/`area_tri`/`v_tri` are overwritten by the
  keep-mask slicing, so it no longer reads two copies of the same reduced
  value.

`landslide/gates.py::evaluate_gates`: added an `elif res.get("datum") ==
"dem":` branch alongside the existing G6 `coverage_frac` check, which flags
`indicative` whenever a DEM-mode result has no `coverage_frac` (i.e.
always, per the Deviations note below). This is the mechanism that actually
closes "can emit `ok`" — the σ/cull fixes above make the *number* more
honest, but only this stops an un-gated coverage risk from reaching `ok`.

### Regression coverage

- `tests/test_dem.py::test_dem_volume_sigma_reflects_real_noise` (new): a
  noisy flat surface must report `datum_rms_m > 0.01`,
  `est_volume_error_m3 > 0`, `lod_m > 0`. Verified failing (all three ≡ 0)
  against the pre-fix code, passing against the fix.
- `tests/test_dem.py::test_dem_volume_bridging_cull_excludes_large_hole`
  (new): a dense grid with a 64 m² punched-out hole must measure within 15
  m² of the real ~336 m² footprint. Verified failing (~383 m², bridged)
  against the pre-fix `× diameter` formula, passing against the fix.
- `tests/test_gates.py` (new file): `test_dem_mode_status_never_ok` — a
  DEM-datum result with no `coverage_frac` must not reach `status="ok"` and
  must carry a reason mentioning the missing coverage gate. Verified failing
  (`status="ok"`) against the pre-fix `gates.py`, passing against the fix.
  `test_non_dem_mode_unaffected_by_dem_guard` confirms the new `elif` branch
  doesn't fire for non-DEM results (a `rim_plane` datum with good
  `coverage_frac` still reaches `ok`).

### Validation

**Fast suite:** `pytest -q -k "not e2e"` — 182 passed, 1 skipped, 23
deselected (was 178/1/23 after B2; +4 is the new regression coverage
above). No regressions. (The `_drain_log_queue` `EOFError` at interpreter
exit is the pre-existing, documented cosmetic issue from audit §4.6, not
new.)

**Focused validation against B3's acceptance criteria** (direct calls to
`dem_volume`/`evaluate_gates`, not a full e2e job — DEM mode has no
benchmark preset per `architecture.md` §7.1, so there is no existing e2e
DEM path to run; §6 item 5 of the audit — a real prior DEM — is explicitly
listed as future field-validation work, out of scope here):
1. Noisy synthetic surface vs. a flat DEM → `datum_rms_m = 0.0254`,
   `est_volume_error_m3 = 2.45` (both `0.0` pre-fix).
2. Dense grid with a 64 m² hole vs. a flat DEM → `area_m2 = 337.5` against
   a real ~336 m² footprint (was `383.7`, bridging the hole, pre-fix).
3. A DEM-datum result run through `evaluate_gates` → `status = "indicative"`
   with reason `"DEM mode has no coverage gate (G6) — treat the
   bridged/unmeasured area as unverified"` (was `status = "ok"` pre-fix).

### Deviations from the audit's suggested fix scope

- The audit's minimum fix scope says "Fix σ, apply the A1 cull and G6 to
  `dem_volume`," and separately offers an explicit fallback: "Otherwise
  disable DEM mode or force `indicative`." σ and the A1 cull are fixed
  directly in `dem_volume`, matching the letter of the suggested scope. G6
  is **not** wired as a real `coverage_frac` computation — that was a
  deliberate scope decision, not an oversight: `dem_volume`'s
  `interior_xyz` arrives already transformed into the DEM-aligned world
  frame (`(pts[interior]·scale) @ R.T + t`, applied by the caller), while
  the `up`/`polygon_ground` the existing `_coverage_gate` needs are still
  in the *original* model frame. Reusing `_coverage_gate` correctly would
  need a second coordinate transform (lifting the 2D traced polygon back to
  3D, applying the same `R, t` as the interior points, in both the ortho
  and ground-frame photo call sites) that touches geometry this task's B3
  scope doesn't otherwise require and that the audit did not reproduce or
  specify — attempting it without a DEM benchmark preset to validate
  against risked introducing a new, unverified correctness bug under this
  task's "smallest effective correction" instruction. The audit's own
  stated fallback (force `indicative`) closes the actual defect named in
  its verdict ("can report `status = ok`") without that risk, implemented
  as one `elif` branch in `gates.py` alongside the real G6 check so a
  future correct `coverage_frac` wiring for DEM mode will automatically
  take over (the `if cov is not None` branch is tried first).
- Did not touch `landslide/change.py::change_volume` (the CLI-only
  two-epoch path, `architecture.md` §1.1 step 7), which also calls
  `dem_volume` but never routes through `pipeline.measure`/`evaluate_gates`
  at all — it has no `status` field today, DEM-mode gating or otherwise,
  and adding one is out of B3's scope (B3 is about `pipeline.measure`'s DEM
  path per the audit's §4.3 evidence, all of which is Probe D against
  `dem_volume` directly).

### Acceptance

B3 is fixed and regression-tested against its three named defects: σ now
reflects real surface noise, the bridging cull uses the same RC1 absolute
cap as `prism_volume`, and DEM-mode results can no longer reach
`status="ok"` un-gated. B4–B5 and F1–F3 remain open per
`FINAL_RELEASE_AUDIT.md`, out of scope for this pass.

## B4 — `RLIMIT_AS` smaller than import-time VM on small or many-core hosts

**Status: FIXED.**

### Root cause

`numpy`/`scipy`/`cv2`/`pycolmap` size their BLAS/OpenMP thread pools off
`nproc` at import time (not at first use), and each thread's scratch buffers
cost real virtual address space even though RSS stays flat — the audit
measured 1.2 GB VM at 1 thread, 1.9 GB at 4, 3.9 GB at 12, with RSS ~150 MB
in all three (§4.5). `server/worker.py`'s functions import these lazily,
inside the worker process, so on a many-core host (or a host whose per-worker
RAM share is small) the import alone can exceed `_worker_mem_limit_bytes()`
before any real work starts — the RLIMIT_AS then kills the import itself,
either as a native crash (thread creation failing under the address-space
cap) or a caught `MemoryError`, depending on exactly where the allocation
lands.

Reproduced directly: a `ProcessPoolExecutor` worker with `RLIMIT_AS` set to
1.6 GB (the audit's own "<4 GB host, 1 worker" figure) and no thread cap,
importing `numpy`/`cv2`/`scipy`/`pycolmap` on this 12-core box —
`BrokenProcessPool` every time, before the imports even return.

### Fix

`server/executor.py`, new `_cap_blas_threads()`, called first thing in
`_worker_init()` (i.e. in the pool initializer, in the worker process, before
`server/worker.py`'s per-task functions get a chance to import anything):
sets `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, and
`NUMEXPR_NUM_THREADS` to `"1"`. This has to run before the first heavy import
in that worker process — setting these afterward is too late, since the
thread pool is sized once at import and cached for the life of the process.
It works because `server/worker.py` never imports numpy/cv2/pycolmap at
module level (only lazily, inside each top-level function), and the server's
`ProcessPoolExecutor` uses Python 3.14's default `forkserver` start method,
so each worker process gets a fresh, not-yet-imported copy of those modules
— the initializer's env change lands before anything reads it.

No change to `_worker_mem_limit_bytes()`'s sizing formula or its 1.5 GB
floor: with threads capped to 1, measured import-time VM (~1.2 GB) already
sits comfortably under that floor on every host tier the formula produces,
so the existing floor was already "sized from measured baseline VM" once the
many-core inflation is removed — no new number was needed. No change to
`max_workers()` or the pool's worker count.

### Regression coverage

`tests/test_executor.py` (new file):
- `test_worker_init_caps_blas_threads_before_heavy_import`: drives the real
  `executor._worker_init` as a real `ProcessPoolExecutor` initializer (with
  `SLOPELENS_WORKER_MEM_MB` forcing the audit's small-host figure) and
  asserts the four env vars read back as `"1"` inside the worker, after the
  heavy imports.
- `test_heavy_import_fits_small_host_rlimit_with_thread_cap`: same setup,
  asserts the import task completes without `BrokenProcessPool`. This is the
  direct §4.5 repro.

Both verified failing against the pre-fix `_worker_init` (which only sets
`RLIMIT_AS`, no thread cap) — `BrokenProcessPool` on this 12-core dev box,
matching the audit's "many-core host" failure mode — and passing against the
fix.

### Validation

**Fast suite:** `pytest -q -k "not e2e"` — 184 passed, 1 skipped, 23
deselected (was 182/1/23 after B3; +2 is the new regression coverage above).
No regressions. (The `_drain_log_queue` `EOFError`-at-exit is the
pre-existing, documented cosmetic issue from audit §4.6, not new.)

**Focused validation against B4's acceptance criterion** ("size the limit
from measured baseline VM… cap BLAS threads… test on a < 4 GB host" —
§5): a standalone script mirroring `server/executor.py`'s real pool
initializer, run on this box's real hardware (12 cores, 23 GB — the same
box the audit's §4.5 numbers came from):
- `RLIMIT_AS` = 1.6 GB (the audit's own "<4 GB host, 1 worker" figure), **no**
  thread cap → `BrokenProcessPool` (repro of the named defect).
- Same 1.6 GB limit, **with** the fix's thread cap → import succeeds;
  `/proc/self/status` inside the worker reports `VmSize` 1,265,652 kB
  (~1.24 GB) and `VmRSS` 105,580 kB (~103 MB) — under the limit with ~350 MB
  of headroom, and matching the audit's own "1 thread → ~1.2 GB VM, ~150 MB
  RSS" baseline (§4.5).

### Deviations from the audit's suggested fix scope

- The audit's minimum fix scope names two actions ("size the limit from
  measured baseline VM… Cap BLAS threads in workers") and a validation step
  ("test on a < 4 GB host"). Only the thread cap changes code — see Fix
  above for why the existing 1.5 GB floor already satisfies "sized from
  measured baseline VM" once the cap removes the many-core inflation, so no
  second, independent change was made there. The "< 4 GB host" test was done
  by forcing `_worker_mem_limit_bytes()`'s own real formula to the audit's
  own measured figure for that tier (`SLOPELENS_WORKER_MEM_MB=1600`) rather
  than provisioning an actual small-RAM machine, which this environment
  doesn't have — the override exists in production code for exactly this
  (an operator sizing their own hardware), so this exercises the real code
  path, not a stand-in.
- Did not address the audit's separately-noted §4.5 Probe C result that a
  *real* `dense_cloud(force=True)` run (not just imports) still hit `OpenCV
  Insufficient memory` at 1.6 GB even with 1 BLAS thread. That is a genuine
  hardware-capacity limit on the smallest RAM tier — the RLIMIT_AS design's
  own stated purpose (`executor.py`'s docstring) is to convert an
  uncatchable kernel OOM-kill into a catchable `MemoryError`, not to
  guarantee every workload fits on every machine, and a clean `MemoryError`
  is exactly that intended behavior, not the "smaller than import-time VM"
  defect B4 names. Out of this pass's scope.

### Acceptance

B4 is fixed and regression-tested: worker processes no longer exceed their
own memory limit during import alone on a many-core or small-RAM host — the
audit's own reproduction figures for a "<4 GB, 1 worker" host now complete
the identical import successfully with ~350 MB of headroom to spare. B5 and
F1–F3 remain open per `FINAL_RELEASE_AUDIT.md`, out of scope for this pass.

## B5 — README promises 1–8 % error and a single-sweep protocol

**Status: FIXED.**

### Root cause

`README.md` predates the A1/RC1 measured-range model and G6 coverage gate.
Four spots still describe the old, pre-gate behavior:

- `README.md:6` (capture instructions) told users to sweep left → right in
  one pass — a single-azimuth capture.
- `README.md:12–14` claimed a single point-estimate accuracy ("scale error
  < 2 %, volume error ≈ 1–8 %") as the thing users should expect.
- `README.md:268` ("Capturing good photos") repeated the single-sweep,
  left → right protocol with no azimuth guidance.
- `README.md:286–287` (benchmark table) listed the same ~8 %/~1 % volume
  errors with no caveat.

Reproduced directly against the current gate/test behavior, not just read:
`landslide/gates.py`'s G6 (`gates.py:84-96`) marks any result with
`coverage_frac < 0.6` `rejected` and `< 0.85` `indicative` — `ok` requires
≥ 0.85. `FINAL_RELEASE_AUDIT.md` §2 measured single-arc coverage at
0.48–0.62 on every scale-able preset (never above the `ok` threshold), and
`tests/test_e2e_synth.py:104-114` explicitly asserts this scene's ~60 %
coverage "should never read as `status=ok`" and that the old single-number
tolerance ("no longer applies") was replaced by the
`cut_measured_m3 <= truth <= cut_upper_m3` range check. So a user following
the README's single-sweep instructions gets a `status=indicative`/`rejected`
result with a range roughly 50–140 m³ wide around a 67 m³ truth — not the
"1–8 % error" the README promised. The two-azimuth capture protocol that
can reach `coverage_frac ≥ 0.85` (and thus `ok`) is documented only in
`REMAINING_ACCURACY_PLAN.md` (never shipped to `README.md`).

### Fix

Documentation only, `README.md`, four spots (matching the audit's named
evidence lines):
- Intro (`:6-19`): capture instructions now say two vantage points ≥60°
  apart (or one elevated arc), state that a single sweep only reaches
  48–62 % coverage and reads `indicative`/`rejected`, never `ok`, and
  replace the single-number accuracy claim with the actual range + status
  model (`cut_measured_m3`–`cut_upper_m3`, ~50–140 m³ around 67 m³ truth for
  a single sweep).
- Step 1 of the workflow (`:27-30` pre-edit): same two-azimuth guidance,
  with a pointer to "Capturing good photos".
- "Capturing good photos" (`:268-` pre-edit): leads with the two-azimuth
  protocol, camera elevation ≥20°, and marker ≥50 cm facing the cameras
  (values taken from `REMAINING_ACCURACY_PLAN.md`'s already-derived capture
  protocol, not newly invented).
- Benchmark table (`:286-287` pre-edit): the ~8 %/~1 % rows keep their
  original numbers (still accurate as historical benchmark data) but gain a
  footnote explaining they are the old bridged-interpolation metric, not
  what `cut_volume_m3` reports today, and pointing to the actual
  `status=indicative` range for the same single-sweep scene.

No code, test, or gate logic changed — B5 is purely a documentation/gate
mismatch per the audit's own classification.

### Regression coverage

Not applicable — no code path changed. The existing
`tests/test_e2e_synth.py:104-114` assertions (`status` must be
`indicative`/`rejected` for this scene, range must bracket truth) already
guard the behavior the README now describes; a README-only change has
nothing further to regress against.

### Validation

Focused check against B5's acceptance criterion ("rewrite accuracy and
capture guidance to match gates: two azimuths, ranges, statuses"): re-read
`README.md` end to end after editing and cross-checked every claim against
current source —
- `gates.py:84-96` — G6 thresholds (0.6 rejected, 0.85 indicative) now match
  the stated coverage/status claims.
- `FINAL_RELEASE_AUDIT.md` §2 arc-preset range (49.9–142.8 photo,
  49.7–139.5 ortho against 67.16 truth) — matches the "~50–140 m³" figure
  now in the README.
- `REMAINING_ACCURACY_PLAN.md`'s capture protocol (two azimuths ≥60° apart,
  elevation ≥20°, marker ≥50 cm facing cameras, ≥5 views) — matches the new
  "Capturing good photos" wording.
- `tests/test_e2e_synth.py:104-114` — the README no longer promises a
  point-estimate error for the validated scene, matching what this test
  actually asserts today.
No remaining reference in `README.md` to a single left-to-right sweep as
the recommended (unqualified) protocol; `grep -n "sweep\|left.*right"
README.md` shows every remaining mention either describes the
single-sweep case explicitly as the lesser option or is unrelated
(the live-capture-helper's "sweep speed" quality metric).

### Deviations from the audit's suggested fix scope

- Left the benchmark table's ~8 %/~1 % numbers in place rather than
  replacing them outright — they're real historical measurements of a
  different (pre-A1/RC1) metric, and deleting them would lose information;
  a footnote reframes what they do and don't mean today, which satisfies
  "rewrite... to match gates" without discarding real benchmark data.
- Did not touch `architecture.md` §3.9/§7.1 or the stale `results.md`
  descending row the audit also flagged under its separate §4.6 "Minor"
  doc-drift list — those are outside B5's named evidence lines
  (`README.md:6,13-14,268,286-287`) and outside this task's B5-only scope.

### Acceptance

B5 is fixed. `README.md`'s capture instructions and accuracy claims now
match `gates.py`'s actual thresholds and `tests/test_e2e_synth.py`'s actual
assertions: two-azimuth capture is the documented path to `ok`, a
single-sweep capture is documented as `indicative`/`rejected` with a wide
range (not a tight point estimate), and the benchmark table's older numbers
are correctly scoped. F1–F3 remain open per `FINAL_RELEASE_AUDIT.md`, out
of scope for this pass. All five release blockers (B1–B5) are now fixed;
per the audit's own path to conditional GO (§8), what remains before a
pilot is the full e2e suite from deleted caches, a regenerated
`data/bench/results.md`, and the hardware test in §6 item 6 — not part of
B1–B5 and not attempted here.

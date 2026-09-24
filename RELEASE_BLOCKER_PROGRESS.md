# Release Blocker Progress

Tracks fixes for the blockers in `FINAL_RELEASE_AUDIT.md` §5. B1 and B2 are now covered.

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

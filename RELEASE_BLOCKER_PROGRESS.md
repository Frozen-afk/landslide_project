# Release Blocker Progress

Tracks fixes for the blockers in `FINAL_RELEASE_AUDIT.md` §5. This pass covers **B1 only**, per task scope.

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

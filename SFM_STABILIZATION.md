# SfM stabilization pass — focal-lock retry reachability / selection

Follow-up to `REMAINING_ACCURACY_PROGRESS.md` §6 ("SfM non-determinism (discovered,
not fixed)"), which flagged two problems in `landslide/sfm.py`'s `reconstruct()` retry
ladder without fixing them:

1. the focal-lock retry is inserted into the attempt ladder but never executed,
2. `tests/test_e2e_presets.py::test_descending_focal_spread_is_tight` intermittently
   fails on a fresh (non-cached) SfM run of the `descending` preset.

Scope: only these two problems. No measurement gate, volume logic, coverage handling,
or threshold was touched.

## 1. Reproduction

### 1.1 Retry never executed

`reconstruct()`'s loop (`landslide/sfm.py:324-386`) runs one SfM attempt per rung of
the ladder built by `_build_attempts`. After each attempt it computes the per-camera
focal spread (`_focal_spread`); if it exceeds `FOCAL_SPREAD_RATIO_BAD` (1.15x) and no
locked retry has been queued yet, it inserts a `"shared intrinsics (focal-locked)"`
attempt right after the current one (`attempts.insert(i + 1, ...)`). Immediately after
that, the *same* iteration evaluated `done = (score[0] == 1 and nreg >= 0.9n)` and, if
true, `break`. On a preset whose bad-focal attempt still registers almost every image
(`descending`'s default attempt: 20/21 registered, spread 1.82x), `done` was true on
that very iteration — the loop broke before ever reaching index `i + 1`, so the
inserted retry sat in `attempts` and was never run.

Reproduced directly against the cached benchmark data and a fresh build:

```
$ rm -rf data/bench/descending/work
$ python -c "from landslide.sfm import reconstruct; reconstruct(
      'data/bench/descending/images', 'data/bench/descending/work',
      reuse=False, log=print)"
...
[sfm] attempt 'default': per-camera focal length spread 1.82x (>1.15x is unreliable
      self-calibration; path collinearity 0.440)
[sfm] unstable per-camera focal — retrying next with one shared, unrefined focal length
[sfm] reconstruction done (default): 20/21 images registered, 4617 sparse points
FINAL RATIO (910.38, 1658.05, 1.82)
```

No second `[sfm] importing 21 photos` / `incremental mapping ... focal length locked`
line ever appears — confirming the retry attempt was queued but not run.

### 1.2 Intermittent failure of `test_descending_focal_spread_is_tight`

The test calls `reconstruct(..., reuse=True)`. Against a *cached* work directory this
is deterministic (it just reloads whatever is on disk), so the "intermittent" failure
only shows up on a clean/first build (matching `REMAINING_ACCURACY_PROGRESS.md`'s own
note that this surfaced when the cached `data/bench/*/work` directories were deleted
to force fresh reconstructions). `descending`'s own camera geometry (RC3: three
low-elevation cameras with unstable per-image intrinsics) triggers a bad focal spread
on most fresh COLMAP runs; because of §1.1, whether the resulting `ctx` has a good or
bad focal ratio was purely down to whether the *un-mitigated* first attempt happened to
register enough images to look "done" — i.e. down to COLMAP's own run-to-run matching/
BA variance, not the retry logic.

## 2. Fixes

Both problems share one root cause and needed two changes to `landslide/sfm.py` to
actually fix the observable behavior (not just the reachability):

**A. Don't stop the ladder on the same iteration a corrective retry was just queued**
(`reconstruct()`, ~`:355-380`). Added a `just_inserted_retry` flag, set only when this
iteration's bad focal spread caused a new retry attempt to be inserted; `done` now
also requires `not just_inserted_retry`. This is the minimal change that makes the
inserted attempt reachable — every other rung of the ladder is untouched, and a clean
(good-focal) attempt still stops the ladder immediately as before (`just_inserted_retry`
stays `False` whenever `ratio <= FOCAL_SPREAD_RATIO_BAD`).

**B. Make the ladder's best-attempt selection prefer a reliable focal over a merely
larger registration count** (`_attempt_score()`, ~`:271-286`). Reproducing (A) alone
against real data showed the retry *running* but the ladder's tie-break (`score =
(usable, nreg, npts)`) sometimes still picking the earlier, bad-focal attempt when it
had registered marginally more images than the locked retry — i.e. the retry would run
and still lose, leaving the test exactly as flaky as before. `_attempt_score` now takes
the attempt's focal ratio and returns `(usable, good_focal, nreg, npts)`: a
diverged-focal attempt can no longer outrank a focal-reliable one purely on
registration/point count. `usable` (the existing starved-geometry gate) still
dominates first, unchanged; `good_focal` defaults to `focal_ratio=1.0` at every other
call site (`tests/test_enhance.py`), so it does not affect any attempt whose focal
spread was never measured/never bad. Two usable, both-good-focal attempts still
tie-break on `(nreg, npts)` exactly as before — no unrelated scoring behavior changed.

Nothing else in `sfm.py` — the attempt ladder's contents/order, `_run_attempt`,
`_focal_spread`, `_camera_center_collinearity`, the F7 best-vs-disk write-back, and the
final rejection/warning thresholds — was touched.

## 3. Regression tests added

`tests/test_sfm.py` (fast, no real COLMAP run — same monkeypatch pattern as the
existing F7 test):

- `test_focal_lock_retry_actually_runs` — monkeypatches `_run_attempt` to return a
  bad-focal reconstruction first, a good one second, from a single-rung `_build_attempts`
  stub; asserts both calls happen (`calls == [False, True]`, i.e. the second call has
  `lock_focal=True`) and that the final `ctx` carries the good focal ratio. Verified to
  fail against the pre-fix code (`calls == [False]` — retry never ran).
- `test_attempt_score_prefers_good_focal_over_more_images` — direct unit test of the
  new `_attempt_score` tie-break: a 21/21-registered, bad-focal score must lose to a
  20/21-registered, good-focal score, and two good-focal attempts still fall back to
  registration/point count. Verified to fail against the pre-fix signature (no
  `focal_ratio` parameter existed).
- `test_build_attempts_is_deterministic` — `_build_attempts(n, size)` called twice
  returns identical, uniquely-labeled lists (the retry-insertion logic depends on
  stable indices into this list).

`tests/test_e2e_presets.py` (slow, real COLMAP — part of the e2e suite):

- `test_descending_focal_spread_is_stable_across_clean_runs` — deletes
  `descending`'s cached work directory and runs `reconstruct(..., reuse=False)` twice
  from scratch, asserting `ratio < 1.15` after each clean build. This is the direct
  regression proof for §1.2: a flaky assertion on one cached run becomes a repeated,
  from-scratch check.

## 4. Results

Fast suite (unaffected, includes the 3 new `test_sfm.py` cases):
```
$ pytest -q -k "not e2e" --ignore=tests/test_server.py
135 passed, 23 deselected in 44.12s
```

Server suite (unaffected; exits cleanly, confirming F1's earlier crash-recovery fix
still holds):
```
$ pytest -q tests/test_server.py
19 passed in 5.21s
```

Full e2e suite, against a clean `descending` work directory (includes the new
repeated-clean-run test — two full fresh SfM builds of `descending` inside one test):
```
$ pytest -q -s tests/test_e2e_presets.py tests/test_e2e_synth.py
23 passed in 808.27s (0:13:28)
```
`descending`'s own volume test result under this pass: `status rejected`, reasons
`["ray-cast hit 89% ... longest consecutive miss run 10", "only 55% of the traced
region has nearby data"]` — an *independent, real* limitation (RC1/RC3, unchanged by
this pass) correctly producing `rejected`, not a crash or a focal-spread flake.

Repeated clean (`reuse=False`) builds of `descending`, run individually outside pytest
during development of the fix:

| run | default attempt spread | retry ran? | final ratio |
|---|---|---|---|
| 1 (pre-fix) | 1.82x | no (bug) | 1.82x — **fails** |
| 2 (fix A only) | 2.45x | yes | 2.45x — **fails** (tie-break; motivated fix B) |
| 3 (fix A only) | 2.41x | yes | 1.00x — passes |
| 4 (fix A+B) | 3.63x | yes | 1.00x — passes |
| 5 (fix A+B) | 2.04x | yes | 1.00x — passes |
| 6 (fix A+B, in e2e test) | 2.09x | yes | 1.00x — passes |
| 7 (fix A+B, in e2e test) | none logged | n/a (spread already fine) | passes |
| 8–9 (fix A+B, full e2e suite) | — | — | passes (both, see §4) |

Runs 2–3 (fix A applied without fix B) are the direct evidence that reachability alone
was insufficient — the same clean-build/retry-executes conditions still produced one
pass and one fail purely on the tie-break, which is what motivated fix B. Runs 4–9, all
with both fixes applied, are 6/6 passes across a genuinely bad-spread range (2.04x–
3.63x) plus one run where the default attempt happened to converge cleanly on its own.

## 5. Remaining nondeterminism (not fixed, documented per the task's original note)

COLMAP's incremental mapping is not bit-for-bit reproducible across runs on the same
input (multi-threaded feature matching order and bundle adjustment), so `descending`'s
`default` attempt's own focal spread and registration count still vary run to run
(observed 1.82x–47.16x across the clean builds in this pass, occasionally within
tolerance on the first try). This pass does not eliminate that variance — it ensures
that whenever the spread is bad, the ladder's mitigation (A) actually runs and (B)
actually wins, so the *final* focal ratio (and therefore the test) is stable across
that variance rather than being exposed by it. Every clean build in this pass (9 total,
manual and in-suite) converged on a final ratio at or under 1.15x once both fixes were
applied.

This is the same underlying COLMAP nondeterminism `REMAINING_ACCURACY_PROGRESS.md`
already named as out of scope for a fix ("pre-existing behavior in `sfm.py`...
orthogonal to RC1-RC7"); this pass addresses only the two confirmed defects in how the
ladder *responds* to that nondeterminism, not the nondeterminism itself.

## 6. Stop condition

Both confirmed problems reproduced, root-caused, and fixed with the smallest change
that made the fix effective (not just reachable). Regression tests added for
deterministic attempt-list construction, reachable retry execution, deterministic
selection/tie-breaking, and stable repeated clean builds. Fast, server, and e2e suites
all pass, including repeated clean runs of the affected preset. No measurement gate,
volume logic, coverage handling, or threshold was changed. Stopping here per the task's
scope.

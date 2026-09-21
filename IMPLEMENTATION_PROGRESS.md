# Tier 0 implementation progress

Tracks `implementation_plan.md` Part C, step 1 (T0.1–T0.8). Tier 1+ not started
(out of scope for this pass).

## Validation against current repo

All Tier 0 items were re-checked against the code before implementing
(architecture.md / implementation_plan.md line numbers matched the tree as of
commit `9834dd2`). One design gap found during implementation, resolved with
the user before coding (see T0.4 note).

## Status

| Item | Status | Notes |
| --- | --- | --- |
| T0.1 EXIF focal prior | done | `pipeline.import_photos` now passes `exif=im.getexif().tobytes()` to the re-encode; verified EXIF survives `exif_transpose`→`convert`→`resize` in this Pillow version. |
| T0.2 Dense cache keyed to reconstruction | done | `sfm.build_ctx` computes `ReconCtx.fingerprint` (SHA-1 over sorted per-image poses + sparse point count); `densify.dense_cloud` names the cache `dense_<width>_<fp8>.npz` and verifies the fingerprint stored inside the npz before trusting it. `pipeline.run_spec` now calls `reconstruct(..., reuse=True)`. Removed the now-dead `ensure_reconstruction` (only caller was `run_spec`). |
| T0.3 Depth + occlusion in photo-mode selection | done | `volume.select_region` now reads `depth` from `ImageView.project` (previously discarded), requires `depth > 0`, and adds `_front_surface_mask` — a coarse (4px-cell) per-view z-buffer that keeps only points within `max(2% of median depth, ~0)` of the nearest depth in their cell before the polygon test. `pipeline.measure` now calls `select_region(..., extra_views=1)` (was 0) per the plan. |
| T0.4 Symmetric outlier clipping | done, design adjusted | Literal symmetric clip (same `max(1.5m, 8σ)` cap both sides) broke `test_volume.py`'s analytic bowl (2 m legitimate depression clipped) and 3 other tests — a landslide cut is expected to go well past 1.5 m, unlike fill. User chose: keep the existing cap for the high (fill) side; size the low (cut) side cap as `max(high_cap, 3×IQR(interior heights))` (Tukey extreme-outlier fence) so genuine deep depressions survive while near-surface stereo floaters still get trimmed. Added `n_low_dropped` to the result dict. |
| T0.5 Remove the focal hack | done | Deleted the `Ka[0,0] *= w/(2·cx)` / `Kb[...]` rescale in `densify.stereo_pair`; a top-left crop only changes `(w,h)`, `cx,cy` stay valid. |
| T0.6 Typed API models | done | Added `server/schemas.py` (Pydantic): `ArucoScaleRequest`, `ManualScaleRequest`, `MeasureRequest` (`rim_px`, `rim_inner_px`), `PointPair`, `ManualEndpoint`. `server/main.py` routes now take these instead of raw `dict`; `run_measure`/`_run_measure` pass `rim_inner_px` through (fixes S6, which was previously silently ignored). |
| T0.7 UI quick wins | done | `app.js`: canvas handlers switched from mouse to pointer events (`pointerdown/move/up`, `touch-action: none` on the marking canvases) for touch tracing (F1); slope-hazard figure added to the result section (F3); `datumNames` completed with `rim_tps`, `dem`, `prior_epoch` (F4); canvas resize moved out of `draw()` into `loadURL()`/`load()` so it no longer reallocates every freehand mousemove (F5). |
| T0.8 Regression tests | done | `tests/test_pipeline_regressions.py`: EXIF retention, stale dense-cache rejection by fingerprint, occlusion (background plane behind a ridge is excluded from a photo-mode polygon), symmetric low-side clipping, and a schema-validation-rejects-bad-polygon test. |

## Test results (final)

- `pytest -q -k "not e2e"`: 107 passed (102 pre-existing + 5 new in
  `tests/test_pipeline_regressions.py`), 0 failed.
- `pytest -q tests/test_e2e_synth.py`: 5 passed (~70–106 s) — confirms
  T0.1–T0.5 together don't regress the full photo/ortho pipeline end to end.
- `architecture.md` updated to match: dense-cache filename/fingerprint,
  `select_region` occlusion + `extra_views=1`, symmetric clip + `n_low_dropped`,
  typed request bodies, pointer-event tracing, slope-map figure, `datumNames`.

## Not done (out of scope for this pass)

- Tier 1+ (T1.x onward): not started, per instructions.
- T0.6's full scope per the plan also suggested typing `JobSnapshot` and
  `MeasureResult` (and running `pipeline.measure`'s return through
  `.model_dump()`). Skipped: the concrete bug (S1 — untyped request bodies
  causing a 500 on bad input) is fixed by the request-side models alone;
  typing the response would touch `measure()`'s return contract and
  `Job.snapshot()` for no behavior change, which is more than "small diff."

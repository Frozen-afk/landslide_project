# Datum Stability — Root Cause and Gate Design

**Status:** design only. No application code, test, tool or benchmark file was changed while writing this document. S1–S6 of `GUIDED_CAPTURE_PLAN.md` stay blocked.
**Base:** `main` @ `d5653ba`, plus the uncommitted `tools/synth.py` preset entries from S0/S0b.
**Inputs read:** `GUIDED_CAPTURE_PLAN.md` (§11, §12, §12a), `GUIDED_CAPTURE_PROGRESS.md` (S0, S0b), `architecture.md` (§3.5–§3.8a, §7), `landslide/volume.py`, `landslide/gates.py`, `landslide/pipeline.py`, `landslide/ortho.py`, `landslide/ground.py`, `tools/synth.py`, `tools/benchmark.py`, `tests/test_volume.py`, `tests/test_gates.py`, `tests/test_e2e_presets.py`, and the cached `data/bench/twosided{,_b}/work` models.

## 1. Summary

S0b's `twosided_b` reports `status = ok` with 98.6 % coverage and a +15.4 % (ortho) to +16.8 % (photo) volume error. The request states that the dense cloud is biased in the outer rim used for the datum. **The measurements below do not support that.** The cause is different:

1. **The rim cloud is not the cause.** The biased rim points are the ArUco board and its mat. The board stands 7.5 m north of the bowl centre, inside the 0.5–2.5 m rim band. In `twosided_b`, all 13 632 rim points with a residual above 0.3 m lie on the board footprint, and the robust plane fit rejects all 13 632 of them. Outside the board, the median rim residual is +0.006 m. If every rim point's height is replaced by the true terrain height, the cut changes by only +0.28 m³ (+0.4 %).
2. **The datum *model* is the cause.** The synthetic pre-slide terrain is not planar at the scale of the rim ring. `build_terrain` adds value noise with a 0.35 m amplitude at about 7 m wavelength. A plane fitted to the **true** rim sits +0.099 m above the true pre-slide surface when averaged over the polygon. Over about 100 m², that alone is +10 m³ (+15 %). With a perfect cloud (true rim *and* true interior), the pipeline still reports +18.2 %.
3. **Scale adds about +4 %.** The metric model is 1.42 % too large (`twosided_b`), and volume scales with the cube: +4.3 %.
4. **S0's 1.3 % "accuracy" was a cancellation, not a measurement.** `twosided` has the same datum bias (+0.13 m, about +19 %) and +4.9 % from scale. Both are cancelled by 16.3 m² of the polygon (16 %) that the TIN never measured (`area_m2` 85.1 of 101.4 m²). S0b closed the voids, so the cancellation stopped and the datum bias became visible. The capture change did not introduce the error. It unmasked it.
5. **No existing gate or uncertainty term can see a datum model error.** G6 measures only coverage. The bootstrap CI resamples rim points i.i.d., so with 117 k inliers the plane barely moves. The CI (−82.9…−74.0 m³) excludes the truth (−67.16 m³). The rim warnings fire the same way on every preset, so they do not discriminate. No gate reads `datum`, `datum_rms_m` or `est_volume_error_m3`.

**Correction (§6):** add one truth-free datum stability check and one gate. The check fits the adopted datum model with part of the rim ring held out, and reports how far that fit misses the held-out ground. The gate blocks `ok` when this implied datum uncertainty × area exceeds 5 % of |net|, or when the rim is not observed all around. It also blocks `ok` when the datum fell back to the region surface. It is detection only: no volume number changes.

**Consequence the user must decide on (§7):** with this gate, `twosided_b` becomes `indicative`. On the seed-7 benchmark terrain, no rim datum can honestly reach the ±5 % target. S0b §12a.5 criteria 2 and 5 cannot both pass on this scene with any capture. Validating the capture protocol needs a scene whose datum is predictable from its rim. That is a `tools/synth.py` decision, not a gate change.

## 2. Evidence

All numbers come from read-only re-runs on the cached work directories. SfM is reused and the dense cloud is loaded from `dense_1280_v2_*.npz`. `measure()` was called without `artifacts_dir`, so no file was written. The ground-truth frame comes from the benchmark's own `_umeyama_pose_aware` fit. The scripts ran from the session scratchpad and are not committed. §9 describes how to rebuild them in about 60 lines.

### 2.1 Error decomposition (photo mode, the `prism_volume` inputs captured from `measure()`)

The rim and interior point sets are the exact arrays `measure()` passed to `prism_volume`. "TRUE" replaces each point's height with the analytic terrain height at the same (x, y). Truth is 67.16 m³.

| Case | `twosided_b` cut (err) | `twosided` cut (err) |
| --- | --- | --- |
| actual rim, actual interior (= shipped result) | 78.47 (+16.8 %) | 68.07 (+1.3 %) |
| TRUE rim, actual interior | 78.75 (+17.3 %) | 66.91 (−0.4 %) |
| actual rim, TRUE interior | 79.09 (+17.8 %) | 70.91 (+5.6 %) |
| TRUE rim, TRUE interior | 79.37 (+18.2 %) | 69.74 (+3.8 %) |
| measured TIN area / polygon area | 99.8 / 101.8 m² | **85.1 / 101.4 m²** |
| mean datum − true pre-slide surface over the interior | +0.098 m (true rim: +0.099) | +0.136 m (true rim: +0.112) |
| scale error (model too large) → volume | 1.42 % → +4.3 % | 1.64 % → +4.9 % |

Reading: in `twosided_b`, swapping in a perfect rim changes the answer by 0.4 %. The error survives a perfect cloud. In `twosided`, the same datum bias is present. Its result looked accurate only because 16 m² of the polygon was never integrated.

### 2.2 Where the "rim bias" comes from

Rim residual (cloud z − true z) by distance from the bowl centre (`twosided_b`):

| r (m) | n | mean | median | RMS |
| --- | --- | --- | --- | --- |
| 5.5–6.5 | 25 371 | +0.008 | +0.007 | 0.026 |
| 6.5–7.0 | 32 482 | +0.010 | +0.007 | 0.067 |
| 7.0–7.5 | 37 292 | +0.119 | +0.009 | 0.423 |
| 7.5–8.0 | 43 535 | +0.370 | +0.013 | 0.799 |
| 8.0–9.0 | 3 854 | +0.007 | +0.006 | 0.022 |

By azimuth, the heavy tail sits only in 135–225° (north), and there the robust-fit inlier fraction drops to 0.70–0.72. Every other sector has a median bias of +0.001 to +0.010 m and inlier fraction 1.00. The board (`MARKER`: x 17.5, y 26.0, 2.5 m mat, vertical) is at r ≈ 7.5 m, az 180°. That is exactly the 7–8 m ring and the north sector. All 13 632 rim points with dz > 0.3 m fall inside the board footprint, and the robust fit keeps 0 of them. `GUIDED_CAPTURE_PROGRESS.md`'s S0b root-cause section reads the heavy tail as grazing-view terrain bias from the wider arms. That reading is wrong and should be corrected when this plan is implemented. `twosided` shows the same board tail, plus a slightly larger genuine terrain bias (median +0.018 to +0.059 m). S0b's rim is *better* reconstructed than S0's.

### 2.3 The datum model, with truth (world frame, interior footprint 98.6 m²; 0.01 m offset = 1.47 % of volume)

| Rim input | plane | quad | TPS |
| --- | --- | --- | --- |
| `twosided_b` cloud rim (robust inliers) | +0.102 m (+15.0 %) | +0.031 m (+4.6 %) | +0.034 m (+5.0 %) |
| `twosided_b` TRUE rim | +0.102 m (+15.0 %) | +0.016 m (+2.3 %) | +0.019 m (+2.8 %) |
| `twosided` cloud rim | +0.133 m (+19.5 %) | +0.040 m (+5.9 %) | +0.042 m (+6.1 %) |
| `twosided` TRUE rim | +0.110 m (+16.1 %) | +0.019 m (+2.8 %) | +0.029 m (+4.3 %) |

The pipeline adopted `rim_plane` in both runs. The paraboloid was not adopted: `σ_plane − σ_quad` fell below `max(0.02, 0.25σ)` at σ = 0.072 m. The membrane was rejected by its interior-deviation guard (0.19 m > `max(0.10, 1.5σ)`). A richer model would have been closer on this seed. But across 40 terrain seeds (§2.5), the quad and TPS datums also miss by 5–7 % on 6 seeds. **Switching the datum model is not a fix.** The interior is unobserved ground, and the rim cannot fully determine it.

### 2.4 Truth-free signals on the real rims

These can be computed inside `prism_volume` without ground truth:

| Signal | `twosided_b` | `twosided` | Would it block `ok`? |
| --- | --- | --- | --- |
| Sector holdout, adopted model, 8 × 45° windows (RMS of held-out medians × polygon area / \|net\|) | 0.072 m → 9.3 % | 0.073 m → 10.9 % | yes |
| **Sector holdout, adopted model, 8 windows of 90° at 45° steps** | **0.068 m → 8.9 %** | **0.094 m → 14.0 %** | **yes** |
| Plane-vs-quad disagreement over the interior | 0.075 m → 9.5 % | 0.064 m → 8.0 % | yes |
| Inner-half-band vs outer-half-band plane, interior offset | 0.033 m → 4.9 % | 0.043 m → 6.3 % | marginal |
| In-sample per-sector median residual of the plane | −0.080…+0.074 m | −0.103…+0.064 m | (in-sample form of holdout) |
| Rim angular coverage (8 × 45° sectors with ≥ 20 inliers) | 8/8 | 8/8 | no |
| Rim truth bias outside the board | +0.006 m | +0.024 m | no (not observable in the field anyway) |

Holdout cost on the real rims: 1.0–1.7 s, including the steepness filter and robust fit that `prism_volume` already runs.

### 2.5 Calibration: 40 terrain seeds, perfect points (no SfM)

Setup: `build_terrain(seed)` sampled at 0.12 m, interior r < 5.6 m, rim 6.1–8.1 m (the pipeline's 0.5–2.5 m band), `prism_volume` with its own datum selection, error vs `vol_true_poly`. This isolates the datum. There is no cloud or scale error here.

- 15 of 40 seeds have a datum-only error above 5 % (up to +13.3 % on seed 7, the benchmark seed). With full coverage, **all 15 would be `ok` today.**
- Holdout, 90° windows, threshold 5 %: flags 11 of 15. It misses 4 (seeds 32, 37, 38, 39: holdout 3.1–4.7 %, real error 6.0–9.2 %). It also flags 24 of 25 seeds whose realized error was ≤ 5 %.
- Holdout, 45° windows: misses 5 of 15, including two TPS-adopted seeds (31, 35: 3.9 % and 1.5 %). TPS bridges a 45° gap from both sides, which the interior gap does not allow. 90° windows give a held-out arc of about 11 m, about the interior diameter, and catch both.
- Inner/outer radial consistency: misses 13 of 15. Adding it to the holdout catches nothing new. Rejected.
- The median ratio of realized error to 90° holdout is 0.36. The holdout is a conservative scale for datum uncertainty, not a prediction of the error's sign or size.

"24 of 25 flagged" is not a false-block rate in the usual sense. On this terrain, the datum uncertainty really is 5–20 % of the volume. A seed whose realized error happened to be small was right by luck, not by measurement. `indicative` is the correct class for those seeds.

Unit fixtures (`tests/test_volume.py` builders), 90° holdout as % of |net|: flat bowl 0.2, curved hillside with quad adopted 0.1, wall-rim pile 0.0, 2 m rim + 5 cm i.i.d. noise 0.6. All are well under 5 %, so the gate stays silent on planar or correctly-modelled ground. The s-curve road (TPS, 0.85 m³ pile) gives 216 %: a small pile is small against even 1 cm of datum uncertainty over the road. §8 R8 records this as expected `indicative`.

## 3. Root cause: how the datum error reaches the volume

1. `select_region_world` takes every cloud point 0.5–2.5 m outside the traced polygon as rim (`ortho.py:143-154`).
2. `prism_volume` drops steep rim points, then fits a robust plane (`fit_plane_robust`). It upgrades to a paraboloid only on an absolute RMS gain, and to a membrane only if CV wins and the interior-deviation guard passes (`volume.py:298-410`).
3. The adopted surface is extrapolated across the whole interior. Interior heights are `h = (p − c)·n − datum(u, v)` (`volume.py:413-419`). A datum that is δ too high adds about δ × A_cut to `cut`, uniformly. On `twosided_b`, δ ≈ +0.10 m over about 100 m² gives about +10 m³.
4. None of the datum-quality numbers measure extrapolation error:
   - `datum_rms_m` is the in-sample residual *on the ring*. A plane through undulating ground can have a small ring residual (0.072 m) and still be offset at the centre.
   - `bootstrap_volume_ci` resamples rim points with replacement. It measures the sampling variance of the fit, which is close to zero with 10⁵ points. It cannot measure model misspecification. The docstring already notes it never refits the TPS.
   - The TPS interior-deviation guard compares the TPS to the incumbent model, not to the ground. It rejected the richer model and kept the plane, which was the most biased option here.
   - `est_volume_error_m3` (9.9 m³, close to the real 11.3 m³ here) comes mostly from the flat `σ·area` heuristic. No gate reads it.

## 4. Why the existing gates miss it

| Gate or term | What it checks | Why it is blind here |
| --- | --- | --- |
| G1–G5, up-vector | cloud, marker, calibration, path, ray-cast | all clean on `twosided_b` |
| G6 | occupancy and largest void inside the polygon | coverage says nothing about the datum. Closing G6 removed S0's cancelling under-count. |
| G7 | scale floor | the scale is honest (1.17 %). It contributes about 4 % and is not the issue. |
| `net_volume_ci95_m3` | rim i.i.d. bootstrap + interior cell bootstrap + coverage term | no model-error term, so the CI excludes the truth |
| `res["warnings"]` (steep rim, "rim heights span 3.3 m", "6 % outliers") | informational | no gate reads them. Wiring them in would not help: the same three fire on S0, on every preset, and on any sloped site. The 3.3 m span is the 0.1 terrain tilt plus the board, and a plane handles both correctly. |
| `datum == "surface_plane"` (fewer than 15 rim points) | only a warning | **a separate false-`ok` path.** The datum is the region itself, and nothing blocks `ok`. |

## 5. Options evaluated

| # | Option | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Rim support quality and spatial-distribution checks | **Adopt as the precondition of the holdout** (≥ 6 of 8 windows populated) | Does not fire on `twosided{,_b}` (8/8). It does catch a one-sided rim, where a plane's tilt is unconstrained and the holdout cannot be computed. |
| 2 | Residual direction / regional bias detection | **Adopt, in its held-out form** | In-sample sector medians of ±0.08 m show the pattern, but the in-sample fit includes the sector being tested. The holdout is the same idea without that leak. |
| 3 | Robust rim segmentation | **Reject** | The only real contamination (the board) is already removed completely. A perfect rim changes the volume by 0.4 %. |
| 4 | Camera/view support requirements for datum points | **Reject for now** | The rim truth bias outside the board is +0.006 m (b) and +0.024 m (S0). View support is not the driver. It would add projection work per rim point for no measured gain. |
| 5 | Inner vs outer rim consistency | **Reject** | Misses 13 of 15 bad seeds and adds no detection on top of the holdout. The inner/outer split is only 1 m apart radially. |
| 6 | Holdout validation for datum fitting | **Adopt: primary signal** (90° windows, adopted model) | Flags 11 of 15 bad seeds and both real runs. Silent on planar and correctly-curved fixtures. Costs about 1–2 s. |
| 7 | Disagreement between independent datum fits | **Reject as a gate** | False-fires on the flat-bowl fixture (13.5 %). There a 0.35 m ring on the bowl lip makes the paraboloid extrapolate a dip that is not there. Quad/TPS extrapolation from a thin ring is ill-posed, so "the alternatives disagree" is often the alternatives' own fault. |
| 8 | Gate that prevents `ok` when datum stability is insufficient | **Adopt** (G9, §6.2) | The only option that changes the status. Detection only, like G1–G6. |

Also considered and rejected:
- **Switching or forcing a richer datum.** Quad/TPS still miss by 5–7 % on 6 seeds. It changes every volume number and every pinned test.
- **Folding the holdout into `net_volume_ci95_m3`.** On `twosided_b`, hypot(1.75, 6.96) + 2.75 gives a CI of −88.4…−68.5 m³. That still excludes −67.16. The holdout is a *scale*, not a calibrated interval (median ratio 0.36, tail ratio up to about 3). Inflating it to cover this one case would be tuning to the result. Deferred: §10.

## 6. Approved design (smallest safe correction)

Three changes in two files. They add fields and one gate block. They do not change any volume, coverage, CI or existing threshold.

### 6.1 `landslide/volume.py`: datum holdout (D1)

A new private helper next to `_cv_rmse`, reusing `_fit_surface`:

```python
def _datum_holdout(kind, uv, h, center, width_deg=90.0, n_win=8, min_pts=20):
    """Leave-one-arc-out check of the rim datum (DATUM_STABILITY_PLAN.md).
    Fits `kind` with a `width_deg` arc of the rim ring (around `center`)
    held out and returns (rms of the held-out median residuals, windows used).
    The held-out arc spans about the interior's width, so this measures how
    well the rim predicts ground it did not see at interior-like distances.
    Holdout, not in-sample, residual: the in-sample fit absorbs the undulation
    it is meant to detect."""
    ang = np.degrees(np.arctan2(uv[:, 1] - center[1], uv[:, 0] - center[0])) % 360
    meds = []
    for k in range(n_win):
        te = ((ang - k * 360.0 / n_win) % 360) < width_deg
        if te.sum() < min_pts or (~te).sum() < 60:
            continue
        tr_uv, tr_h = uv[~te], h[~te]
        if kind == "tps" and len(tr_uv) > 2500:       # same cap as _cv_rmse's caller
            j = np.random.default_rng(0).choice(len(tr_uv), 2500, replace=False)
            tr_uv, tr_h = tr_uv[j], tr_h[j]
        f = _fit_surface(kind, tr_uv, tr_h)
        if f is not None:
            meds.append(float(np.median(h[te] - f(uv[te]))))
    return (float(np.sqrt(np.mean(np.square(meds)))) if meds else None), len(meds)
```

In `prism_volume`, inside the existing `if datum_pts is rim:` block, after the datum is final (after the TPS block, before `cap`):

- `kind = {"rim_plane": "plane", "rim_quad": "quad", "rim_tps": "tps"}[datum]`.
- `center = ((interior - c) @ basis.T).mean(axis=0)`. This uses the pre-cap interior, the same frame as `uv_r`.
- Call `_datum_holdout(kind, uv_r, h_r, center)` on the plane-inlier rim that the datum was fitted to.
- Add the result fields `datum_holdout_m` (float or None) and `datum_holdout_windows` (int).
- Surface-plane path: `datum_holdout_m = None`, `datum_holdout_windows = 0`.

`dem_volume` is not touched. G6's DEM fallback already makes DEM mode `indicative`.

### 6.2 `landslide/gates.py`: G9 datum stability (D2)

A new block after G6. It uses the same `flag()` helper and the same `indicative` level:

```python
# G9 — datum stability (DATUM_STABILITY_PLAN.md): the rim datum is
# extrapolated across the whole interior; G6 proves the interior was seen,
# not that the datum under it is right.
datum = res.get("datum")
if datum == "surface_plane":
    flag("indicative", "no usable rim around the region — the datum was fitted "
         "to the region itself and the volume is biased toward zero")
elif datum in ("rim_plane", "rim_quad", "rim_tps"):
    dh, nwin = res.get("datum_holdout_m"), res.get("datum_holdout_windows", 0)
    area = res.get("polygon_area_m2") or res.get("area_m2") or 0.0
    net = abs(res.get("net_volume_m3") or 0.0)
    if dh is None or nwin < DATUM_MIN_WINDOWS:
        flag("indicative", f"undisturbed ground seen on only {nwin} of 8 sides "
             "of the region — the datum cannot be cross-checked")
    elif dh * area > DATUM_HOLDOUT_REL * net:
        flag("indicative", f"datum uncertain: the ground around the region "
             f"departs ±{dh:.2f} m from the fitted surface, about "
             f"{dh * area:.1f} m³ ({dh * area / max(net, 1e-9):.0%}) of the volume")
```

Constants, at module level in `gates.py`:
- `DATUM_HOLDOUT_REL = 0.05`. This is the ±5 % volume accuracy that an `ok` result is validated against (`GUIDED_CAPTURE_PLAN.md` §12a.5 item 5). It is not a new or looser target.
- `DATUM_MIN_WINDOWS = 6`. A rim seen on fewer than 6 of the 8 overlapping 90° arcs has one side missing, and the datum tilt there is unconstrained.

Why |net| and not `cut`: a datum offset δ shifts `net` by exactly δ × area, and `cut` by δ × A_cut ≤ δ × area. So |net| is the conservative choice. It is also defined for fill-dominated piles.

### 6.3 Surface-plane fallback (D3)

This is the `surface_plane` branch above. It closes the second false-`ok` path from §4. It is one branch, in the same block, with the same level.

### 6.4 Not changed

- G6 thresholds (0.6 / 0.85 / 2 m²).
- G1–G5, G7, the up-vector gate, and `aruco_scale` limits.
- Volume, cut, fill, CI, `est_volume_error_m3` and datum selection. D1/D2 are detection only, so every existing volume assertion holds byte-for-byte.
- The ±5 % target.
- `tools/synth.py`, `tools/benchmark.py`, and the S0b presets.
- UI: `result.js` already lists `reasons` when `status != "ok"`, so G9's text shows with no frontend change.

## 7. Consequences and decisions for the user

1. **`twosided_b` becomes `indicative`** (G9: 8.9 %, 6.96 m³). `twosided` stays `indicative` and gains a G9 reason (14.0 %). The other presets use the same seed-7 terrain. They are already `indicative` or `rejected` and gain the same reason. No status improves.
2. **S0b cannot pass on this scene with any capture.** A perfect cloud already gives +18.2 % with the adopted datum, and at best about +2–5 % datum error plus about +4 % scale with a richer datum. With G9, criterion 2 (`ok`) fails. Without G9, criterion 5 (±5 %) fails. Neither target is weakened here. The S0b NO-GO stands, for a different reason than recorded.
3. **Decision needed (not part of this plan's implementation):** to validate the *capture protocol* separately from the datum, the benchmark needs a scene whose pre-slide surface the rim can predict. The recommended option is an opt-in `build_terrain` flag that keeps existing presets byte-identical. For example: no noise octaves with a wavelength above the 16 m rim diameter, or a planar-plus-fine-texture surface. Plus a `twosided_b_flat` preset. This is a `tools/synth.py` scope change inside S0-territory and needs explicit approval. The alternative is to accept that G9 holds this scene at `indicative` and to rewrite §12a.5 around that. That would be a criteria change and is also the user's call.
4. **The scale budget is tight.** 1.2–1.6 % scale error is already +3.5–4.9 % volume, before any datum error. This plan does not change it. It is noted here because any future ±5 % acceptance run will be decided mostly by scale.

## 8. Regression scenarios

Fast (`pytest -q -k "not e2e"`) unless marked slow. Each one names the defect it guards.

| # | Scenario | Setup | Expected |
| --- | --- | --- | --- |
| R1 | **Bench-terrain false `ok` (the S0b defect, without SfM)** | `tools.synth.build_terrain(7)` sampled at 0.12 m. Interior r < 5.6, rim r 6.1–8.1, `up = z`. `polygon_ground` = 72-vertex r = 5.6 circle (ground frame = xy). `prism_volume` → `evaluate_gates(_FakeCtx(), res, "ortho")`. | Cut error vs `vol_true_poly` > +10 % (the fixture reproduces the mechanism). `coverage_frac` ≥ 0.85 and void ≤ 2 m² (G6 alone would say `ok`). `status == "indicative"`, with a reason containing "datum uncertain". |
| R2 | Planar ground stays `ok` | `make_bowl_points(rim_width=2.0)`, tilted by `0.1x + 0.03y`, with and without 5 cm i.i.d. noise, full `polygon_ground` | `datum_holdout_m` × area < 1 % of \|net\|. `status == "ok"`. |
| R3 | Correctly curved datum stays `ok` | `make_curved_bowl_points()` (quad adopted) | Holdout < 1 %. No G9 reason. |
| R4 | Surface-plane fallback never `ok` | `make_bowl_points(rim_width=0.0)`, rim = None | `datum == "surface_plane"`, `status == "indicative"`, "no usable rim" reason |
| R5 | One-sided rim never `ok` | R2's fixture with rim points kept only for polar angle in [0°, 150°) | `datum_holdout_windows < 6` or the holdout fires. `status != "ok"`. |
| R6 | Gate mapping (pure `evaluate_gates`) | Hand-built `res` dicts: (a) `rim_plane`, holdout 0.001 m, 8 windows, area 100, net 60 → `ok`. (b) holdout 0.07 m → `indicative`. (c) 5 windows → `indicative`. (d) `dem` unchanged (the existing test still passes). | as listed. The existing `test_non_dem_mode_unaffected_by_dem_guard` needs `datum_holdout_m`/`windows` added to its dict to stay `ok`. That is the only edit to an existing test, and it adds input rather than relaxing an assertion. |
| R7 | Volumes untouched | Existing `tests/test_volume.py` suite, unchanged | All pass. `cut/net/fill/datum/CI` identical to before the change. |
| R8 | Small pile on curved road: known conservative case | `make_s_curve_road_with_pile()` → gates | `indicative` (holdout about 216 % of a 0.85 m³ net). Assert and document it as intended: 1 cm of datum uncertainty over the road is a large share of a sub-m³ pile. |
| R9 (slow) | `twosided_b` false `ok` closed | cached `data/bench/twosided_b`, photo and ortho `measure()` | `status == "indicative"`, G9 reason present. `coverage_frac`, `largest_void_m2`, `cut_measured_m3` equal the S0b record (0.9859 / 0.94 m² / 78.47 photo, 77.49 ortho) to 1e-6 relative. |
| R10 (slow) | No preset gets more optimistic | `tests/test_e2e_presets.py` + `test_e2e_synth.py` | All existing status assertions hold. No preset moves toward `ok`. |

R1 imports `tools.synth` from a fast test. `build_terrain` is pure NumPy/OpenCV and costs well under 1 s. `tests/test_e2e_presets.py` already imports `tools.*`.

## 9. Acceptance criteria

1. R1–R8 pass in `pytest -q -k "not e2e"`. The total count equals 182 passed + the new tests, 3 skipped. No existing assertion is weakened or deleted. The only edit to an existing test is R6's input addition.
2. R1 reproduces the mechanism: without G9 the fixture would be `ok` (asserted through the G6 fields), and its real error is above 10 %.
3. R9: `twosided_b` is `indicative` in both modes with the G9 reason. All G6 numbers and all volume numbers are unchanged from `GUIDED_CAPTURE_PROGRESS.md`'s S0b table.
4. `twosided` stays `indicative`. Its `reasons` gain G9. Its volume numbers are unchanged.
5. The 40-seed calibration (§2.5) re-run against the implemented code flags at least 11 of the 15 seeds with datum error > 5 %. It flags none of R2/R3's planar or correctly-curved fixtures.
6. Runtime: `measure()` on `twosided_b` grows by ≤ 3 s. Measure it with and without the holdout on the cached work dir.
7. Diff scope: `landslide/volume.py` (helper + 2 result fields), `landslide/gates.py` (G9 + 2 constants), `tests/test_volume.py` or `tests/test_gates.py` (new tests + R6's input edit), `architecture.md` §3.6/§3.8a/§5.5 (document G9 and the fields), and a correction note in `GUIDED_CAPTURE_PROGRESS.md` S0b root cause (§2.2). No change to `tools/`, `server/`, the frontend, thresholds, or G6.

## 10. Focused validation procedure

1. **Fast suite:** `.venv/bin/python -m pytest -q -k "not e2e"`. Expect 182 + N passed, 3 skipped.
2. **Real-run check (about 3 min, no SfM, cached):** reuse S0's read pattern. `reconstruct(images, work, reuse=True)`, then `aruco_scale(ctx, side_m=2.0, dict_name="auto")`, then `measure(ctx, gt["polygon_image"], gt["polygon_px"], dense=True, rim_px=14.0, log=…)` without `artifacts_dir`. Repeat in ortho mode using `tools/benchmark.py`'s polygon construction. Run it for `twosided_b` and `twosided`. Record `status`, `reasons`, `datum_holdout_m`, `datum_holdout_windows`, `coverage_frac`, `largest_void_m2`, `cut_measured_m3`, and wall time. Compare against §9 items 3, 4 and 6.
3. **Seed calibration (about 15 min, no SfM):** re-run §2.5 against the implemented `prism_volume` + `evaluate_gates`. Rebuild recipe: loop over seeds 1–40, sample `build_terrain(seed)` on a 0.12 m grid inside r ≤ 8.1 m, split interior and rim as in R1, compare `cut_volume_m3` to `vol_true_poly`, and read `status`. Report the flagged / missed counts. Pass: §9 item 5.
4. **Slow e2e (optional, about 1 h):** `.venv/bin/python -m pytest -q -m slow tests/test_e2e_presets.py tests/test_e2e_synth.py` (R10).
5. Record everything in a new "Datum stability" section of `GUIDED_CAPTURE_PROGRESS.md`. Include the §2.2 correction to the S0b root cause.

**Decision rule, fixed in advance:**
- All of §9 passes → merge. `twosided_b` is recorded as `indicative` (G9), and the S0b NO-GO is re-attributed to the datum model. S1–S6 stay blocked until the §7.3 decision is made.
- R2 or R3 fires G9 (false block on planar or correctly-modelled ground) → stop. The holdout estimator is wrong for that geometry. Do not raise the 5 % threshold to fit.
- §9 item 5 flags fewer than 11 of 15 → stop and report. Do not widen the windows or lower the threshold to fit the seeds.
- Any volume number changes → the change is not detection-only. Revert that part.

## 11. Known limits and deferred work

- **Radially symmetric undulation is invisible from the rim.** A dome or dip centred on the region looks the same from every arc. This is the 4 missed seeds (§2.5). No rim-only method can see it. The mitigation is a pre-event DEM (`dem_volume`), which already exists and is already held at `indicative` by the G6 fallback.
- **The CI still excludes truth on `twosided_b`.** Adding an honest model-error term to `net_volume_ci95_m3` needs a calibrated scale (the holdout under-reads by 1.5–4× in the tail). Deferred until the real-site data from §11 criterion 8 exists. G9 keeps such results out of `ok` in the meantime.
- **`est_volume_error_m3` / `res["warnings"]` are still not gated.** The warnings are not discriminative (§4). A gate on `est_volume_error_m3` would duplicate G9 with a less principled estimate.
- **Datum model selection is unchanged.** The quad/TPS adoption rules picked the most biased model on seed 7. Whether to change them should follow the §7.3 scene decision and the §11-criterion-8 field data, not this synthetic seed.

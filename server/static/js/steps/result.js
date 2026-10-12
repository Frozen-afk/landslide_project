/* Step 4: show the measurement result — table, warnings, artifacts, and
 * (T2.2) the bootstrap 95% CI when the backend computed one. */
import { state, $ } from "../state.js";
import { artifactURL } from "../api.js";

const datumNames = {
  rim_plane: "plane fitted to the rim",
  rim_quad: "curved surface (paraboloid) fitted to the rim",
  rim_tps: "membrane surface (thin-plate spline) fitted to the rim",
  surface_plane: "region surface itself (no rim!)",
  dem: "prior-survey DEM",
  prior_epoch: "earlier-epoch surface (change detection)",
};

const statusLabels = {
  ok: "ok — gates passed",
  indicative: "indicative — see reasons below",
  rejected: "rejected — see reasons below",
};

// S6a (GUIDED_CAPTURE_PLAN.md §7, §7a, §9.3): mirrors landslide/gates.py's
// RULES — the "what to do next time" text per reason id, and whether the
// reason is a capture fault (recapture can fix it), a datum fault (it
// can't), or informational. gates.py stays the source of truth for the
// classification (`measurement_path`); this copy only supplies the display
// text, since no endpoint serves RULES to the browser yet.
const REASON_RULES = {
  G1_thin_dense: { cls: "capture", action:
    "Not enough overlapping texture for 3-D. Shoot from an angle (not " +
    "straight down), keep 70%+ overlap between photos, and include " +
    "textured ground, not just sky or water." },
  G2_scale_warn: { cls: "capture", action:
    "Marker scale is uncertain. Move closer, turn the board to face the " +
    "camera, flatten it if bent, and include it in more photos." },
  G3_focal: { cls: "capture", action:
    "Camera zoom or lens changed mid-capture. Stay on the 1x camera for " +
    "every photo." },
  G4_collinear: { cls: "capture", action:
    "You walked a straight line. Walk a curve around the slide, and add " +
    "a second vantage point well away from the first." },
  G5_fallback: { cls: "capture", action:
    "Get higher (at least 20° above the debris). Keep the whole rim plus " +
    "a margin in every photo. Never shoot from below the crown." },
  G5_partial: { cls: "capture", action:
    "Get higher (at least 20° above the debris). Keep the whole rim plus " +
    "a margin in every photo. Never shoot from below the crown." },
  G5_rejected: { cls: "capture", action:
    "Get higher (at least 20° above the debris). Keep the whole rim plus " +
    "a margin in every photo. Never shoot from below the crown." },
  UP_disagree: { cls: "capture", action:
    "Include more flat, stable ground around the slide in frame." },
  G6_indicative: { cls: "capture", action:
    "Walk to a second vantage point at least 60° around the slide from " +
    "the first, keeping 60-80% overlap, so more of the interior is seen " +
    "from at least two angles." },
  G6_rejected: { cls: "capture", action:
    "Walk to a second vantage point at least 60° around the slide from " +
    "the first, keeping 60-80% overlap, so more of the interior is seen " +
    "from at least two angles." },
  G9_no_rim: { cls: "capture", action:
    "No undisturbed ground was measured around the outline. First check " +
    "the outline traces the slide edge, not the photo edge. If it's " +
    "right, recapture with 2-3 m of undisturbed ground around the slide " +
    "in every photo." },
  G9_rim_partial: { cls: "capture", action:
    "Recapture so the missing side of the slide is in frame. If that " +
    "side cannot be seen (cliff, river, no access), this result can be " +
    "kept as indicative." },
  G9_datum_unstable: { cls: "datum", action:
    "Not a photo problem — more photos will not help. The ground around " +
    "the slide varies, so its edge does not predict the surface the " +
    "slide removed. For a measured result, upload a pre-event elevation " +
    "model (survey, LiDAR, earlier scan) and re-measure." },
  DEM_no_coverage_gate: { cls: "info", action:
    "Measured against your pre-event model. Indicative until SlopeLens " +
    "can check how much of the slide the model covers." },
  // multi-view marking (landslide/gates.py RULES, class "marking")
  MV_insufficient_pov: { cls: "marking", action:
    "Outline the slide on 2 photos from each of 4 different viewpoints " +
    "(8 outlines). Follow the suggested photos; if the photo set has " +
    "fewer than 4 viewpoints, add photos from further around the slide." },
  MV_mask_disagreement: { cls: "marking", action:
    "One or more outlines mark a different area than the others. Open " +
    "the flagged photos and redraw the outline on the same slide edge." },
  MV_poor_visibility: { cls: "marking", action:
    "Part of the region is seen from fewer than 2 viewpoints, or mostly " +
    "hidden in an outlined photo. Outline it on photos that show that " +
    "part, from another direction." },
  MV_uncertain_boundary: { cls: "marking", action:
    "The outlines do not agree on where the edge is. Redraw the loosest " +
    "outlines along the line between disturbed and undisturbed ground, " +
    "and add an outline from another viewpoint." },
  MV_occlusion_support: { cls: "marking", action:
    "Too little 3-D surface was reconstructed under an outline to tell " +
    "what hides what. Use photos where the slide surface is textured and " +
    "reconstructed, or outline on a different photo." },
  MV_disconnected: { cls: "marking", action:
    "The outlines agree on separate patches, not one region. Outline " +
    "one connected slide; measure separate slides one at a time." },
};

const confidenceLabels = {
  high: "high — 4 viewpoints × 2 photos outlined, every check passed",
  lower: "lower — single-photo outline (legacy); outline several viewpoints for a final result",
  indicative: "indicative only",
  rejected: "rejected",
};

const pathLabels = {
  rim: "Measured — the rim datum passed every check.",
  dem: "Measured against your pre-event model.",
  indicative_only: "Not a photo problem — more photos will not help.",
  capture_failure: "Capture problem — fixable.",
  marking: "Outline problem — fixable by redrawing or adding outlines, no new photos needed.",
};

// S6b (GUIDED_CAPTURE_PLAN.md §9.3, §12 stage table row 6): `result.capture_report`
// (server/jobs.py::_capture_report) is only present on a job that went through
// guided capture — a normal multi-file upload has no `job.capture`, so this
// block stays hidden for every pre-existing job.
function renderCaptureReport(r) {
  const box = document.getElementById("result-capture-report");
  if (!box) return;
  const cr = r.capture_report;
  if (!cr) { box.classList.add("hidden"); box.innerHTML = ""; return; }

  const fmtCounts = (obj) => Object.entries(obj || {}).map(([k, n]) => `${k}: ${n}`).join("  ") || "—";
  const lines = [`<b>Guided capture report</b>`];
  lines.push(`<div>photos — ${fmtCounts(cr.arms)} (total ${cr.total_photos})</div>`);
  lines.push(`<div>marker views — total ${cr.marker_views_total}` +
    (Object.keys(cr.marker_views || {}).length ? ` (${fmtCounts(cr.marker_views)})` : "") + `</div>`);
  if (typeof cr.elevation_deg === "number") {
    lines.push(`<div>on-site preview: median camera elevation ${cr.elevation_deg.toFixed(1)}°` +
      (typeof cr.arm_separation_deg === "number" ? `, arm separation ${cr.arm_separation_deg.toFixed(0)}°` : "") +
      `</div>`);
  } else if (cr.preview_status && cr.preview_status !== "done") {
    lines.push(`<div class="note">on-site preview: ${cr.preview_status}` +
      (cr.preview_status === "stale" ? " (interrupted by a restart, never re-run)" : "") + `</div>`);
  } else if (!cr.preview_status) {
    lines.push(`<div class="note">no on-site preview was run for this session</div>`);
  }
  if (cr.preview_checks.length) {
    // §7a.2 "predicted" findings: what the cheap on-site preview flagged
    // before the real reconstruction ran, each already carrying its own
    // "what to do next time" action text (landslide/capture.py T3_MESSAGES
    // / gates.py RULES, prefixed "Predicted: " by preview_report()).
    lines.push(`<div>what the on-site preview flagged:</div><ul>` +
      cr.preview_checks.map((c) => `<li class="${c.level === "block" ? "err" : "warn"}">${c.text}</li>`).join("") +
      `</ul>`);
  }
  if (typeof cr.datum_estimate_ratio === "number") {
    // §3a.4 / §12b.6: this ratio never discriminates reliably (calibration
    // failed on every scene tested) and is shown as raw context only — never
    // as a predicted path or a verdict on the result above.
    lines.push(`<div class="note">early on-site datum estimate (informational ` +
      `only, not a prediction — see the result's own "path" and reasons ` +
      `above for the actual measurement): ratio ${cr.datum_estimate_ratio.toFixed(2)}</div>`);
  }
  // S5C (§12c.8): the predicted-vs-actual pair criterion 8 needs to check
  // the coverage proxy against real field sites. `predicted_g6` is null
  // until the §12c.5 calibration freezes C_REJ/C_OK.
  if (typeof cr.preview_coverage === "number") {
    const pct = Math.round(cr.preview_coverage * 100);
    const actual = typeof r.coverage_frac === "number"
      ? `${Math.round(r.coverage_frac * 100)}% (${r.status || "?"})` : (r.status || "?");
    lines.push(`<div>predicted G6 ${cr.predicted_g6 || `~${pct}%`} → actual ${actual}` +
      (cr.roi_source ? ` (roi: ${cr.roi_source})` : "") + `</div>`);
  }
  if (cr.finalized_override) {
    lines.push(`<div class="err">⚠ finalized with override — ` +
      `${cr.overridden_checks.length} on-site preview warning(s) were bypassed:</div><ul>` +
      cr.overridden_checks.map((c) => `<li>${c.text}</li>`).join("") + `</ul>`);
  }
  box.innerHTML = lines.join("");
  box.classList.remove("hidden");
}

function scrollToDemUpload() {
  const input = document.getElementById("dem-input");
  if (input) input.closest("section")?.scrollIntoView({ behavior: "smooth" });
  input?.focus();
}

export function showResult(r) {
  $("step-result").classList.remove("hidden");
  state.lastResult = r;
  const swell = parseFloat($("mat-factor").value) || 1.0;

  // T2.2: net_volume_ci95_m3 is a [lo, hi] resample-based 95% CI, only
  // present when the bootstrap ran (surface-fallback datum, <15 rim points,
  // or too few valid resamples fall back to the flat est_volume_error_m3
  // heuristic instead — see pipeline.py / IMPLEMENTATION_PROGRESS.md T2.2).
  const ci = r.net_volume_ci95_m3;
  const uncertainty = Array.isArray(ci) && ci.length === 2
    ? `${ci[0].toFixed(1)} – ${ci[1].toFixed(1)} m³ (95% CI)`
    : `± ${r.est_volume_error_m3.toFixed(0)} m³`;

  const rows = [];
  // G8: status/reasons (A3) up front — a confident number with no coverage
  // or calibration context is exactly what let past runs hide a 40-60%
  // unobserved void behind a "1.7% error" (see REMAINING_ACCURACY_PLAN.md).
  if (r.status) {
    rows.push(["status", statusLabels[r.status] || r.status]);
    if (r.measurement_path && pathLabels[r.measurement_path]) {
      rows.push(["path", pathLabels[r.measurement_path]]);
    }
  } else {
    // S6a (§9.3): a result saved before the gates existed has no status —
    // label it rather than silently rendering as if it had been checked.
    rows.push(["status", "pre-gate result — not checked"]);
  }
  // multi-view marking: a photo-mode result with no `annotation_mode` was
  // saved before multi-view existed — it is a single-photo outline too
  const singleView = r.mode === "photo" && r.annotation_mode !== "multi_view";
  const conf = r.confidence || (singleView ? "lower" : null);
  if (conf) rows.push(["confidence", confidenceLabels[conf] || conf]);
  const mvr = r.multiview;
  if (mvr) {
    const rq = mvr.requirement;
    rows.push(["outlines used", `${mvr.n_used} of ${mvr.n_annotations} · ` +
      `${rq.n_groups_complete} of ${rq.min_groups} viewpoints complete`]);
    const b = r.volume_bounds_m3;
    if (b && b.lower && b.upper) {
      rows.push(["cut, region bounds (confident – best – outer limit)",
        `${b.lower.cut_volume_m3.toFixed(1)} – ${b.best.cut_volume_m3.toFixed(1)} – ` +
        `${b.upper.cut_volume_m3.toFixed(1)} m³`]);
      rows.push(["net, region bounds",
        `${b.lower.net_volume_m3.toFixed(1)} – ${b.best.net_volume_m3.toFixed(1)} – ` +
        `${b.upper.net_volume_m3.toFixed(1)} m³`]);
    }
    rows.push(["fused region area (confident – best – outer limit)",
      `${mvr.area_lower_m2.toFixed(1)} – ${mvr.area_best_m2.toFixed(1)} – ${mvr.area_upper_m2.toFixed(1)} m²`]);
    if (typeof mvr.weak_support_frac === "number") {
      rows.push(["region seen from fewer than 2 viewpoints", `${(mvr.weak_support_frac * 100).toFixed(0)}%`]);
    }
  }
  rows.push(
    ["net volume (fill − cut)", `${r.net_volume_m3.toFixed(1)} m³`],
    ["cut (depression below rim)", `${r.cut_volume_m3.toFixed(1)} m³`],
    ["fill (material above rim)", `${r.fill_volume_m3.toFixed(1)} m³`],
    ["area", `${r.area_m2.toFixed(1)} m²`],
    ["max depth below rim", `${r.max_depth_m.toFixed(2)} m`],
    ["datum", datumNames[r.datum] || r.datum],
    ["datum rms residual", `${r.datum_rms_m.toFixed(2)} m`],
    ["net volume uncertainty", uncertainty],
  );
  if (typeof r.cut_upper_m3 === "number" && r.bridged_area_m2 > 0) {
    rows.push(["cut, upper bound (unmeasured area at max depth)",
              `${r.cut_upper_m3.toFixed(1)} m³`]);
  }
  if (r.region_method) {
    rows.push(["region selection", r.region_method]);
  }
  if (r.up_source) {
    rows.push(["up vector source", r.up_source
      + (typeof r.up_disagree_deg === "number"
         ? ` (candidates disagreed by ${r.up_disagree_deg.toFixed(0)}°)` : "")]);
  }
  if (typeof r.coverage_frac === "number") {
    rows.push(["coverage of traced region", `${(r.coverage_frac * 100).toFixed(0)}%`
      + (r.largest_void_m2 ? ` (largest gap ${r.largest_void_m2.toFixed(1)} m²)` : "")]);
  }
  if (typeof r.hit_frac === "number") {
    rows.push(["ray-cast hit fraction", `${(r.hit_frac * 100).toFixed(0)}%`]);
  }
  if (typeof r.volume_raster_m3 === "number") {
    rows.push(["raster cross-check (independent)", `${r.volume_raster_m3.toFixed(1)} m³`]);
  }
  if (r.unmeasured_area_m2) {
    rows.push(["unmeasured area (no raster data)", `${r.unmeasured_area_m2.toFixed(1)} m²`]);
  }
  if (swell > 1.0) {
    rows.push(["loose volume to haul (cut × swell)", `${(r.cut_volume_m3 * swell).toFixed(1)} m³`]);
    rows.push(["loose volume to haul (|net| × swell)", `${(Math.abs(r.net_volume_m3) * swell).toFixed(1)} m³`]);
  }
  if (r.scale_rel_error) {
    rows.push(["scale accuracy", `± ${(r.scale_rel_error * 100).toFixed(1)}%`]);
  }
  rows.push(
    ["cloud / points used", `${r.cloud} · ${r.n_points.toLocaleString()}`],
    ["scale", `${r.scale.toPrecision(4)} m/unit (${r.scale_method})`],
    ["points in region / rim", `${r.n_points.toLocaleString()} / ${r.n_rim_points.toLocaleString()}`],
  );
  $("result-table").innerHTML =
    "<table>" + rows.map((x) => `<tr><td>${x[0]}</td><td><b>${x[1]}</b></td></tr>`).join("") + "</table>";

  const reasons = r.reasons || [];
  const reasonIds = r.reason_ids || [];
  const gated = r.status && r.status !== "ok";
  const reasonItems = gated
    ? reasons.map((w, i) => {
        const rule = REASON_RULES[reasonIds[i]];
        return rule ? `<li>${w}<br><i>${rule.action}</i></li>` : `<li>${w}</li>`;
      })
    : [];
  const warnItems = (r.warnings || []).map((w) => `<li>${w}</li>`);
  const wb = $("result-warnings");
  if (reasonItems.length || warnItems.length) {
    wb.classList.remove("hidden");
    let html = "<b>⚠ check before trusting the numbers:</b><ul>" +
      reasonItems.join("") + warnItems.join("") + "</ul>";
    if (r.measurement_path === "indicative_only") {
      // §7a.2 / §9.4: the existing DEM upload control (step 3) already
      // does the re-measure — point at it instead of duplicating it here.
      html += '<p><button type="button" id="result-upload-dem-btn">' +
        "Upload pre-event DEM</button></p>";
    }
    wb.innerHTML = html;
    document.getElementById("result-upload-dem-btn")
      ?.addEventListener("click", scrollToDemUpload);
  } else {
    wb.classList.add("hidden");
  }
  renderCaptureReport(r);
  $("img-overlay").src = artifactURL("overlay.jpg", true);
  $("img-height").src = artifactURL("heightmap.png", true);
  $("img-slope").src = artifactURL("slopemap.png", true);
  const hasRegion = (r.artifacts || []).includes("regionmap.png");
  $("fig-region").classList.toggle("hidden", !hasRegion);
  if (hasRegion) $("img-region").src = artifactURL("regionmap.png", true);
  $("step-result").scrollIntoView({ behavior: "smooth" });
}

export function initResult() {
  $("mat-factor").addEventListener("change", () => {
    if (state.lastResult) showResult(state.lastResult);
  });
}

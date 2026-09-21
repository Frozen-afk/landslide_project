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

  const rows = [
    ["net volume (fill − cut)", `${r.net_volume_m3.toFixed(1)} m³`],
    ["cut (depression below rim)", `${r.cut_volume_m3.toFixed(1)} m³`],
    ["fill (material above rim)", `${r.fill_volume_m3.toFixed(1)} m³`],
    ["area", `${r.area_m2.toFixed(1)} m²`],
    ["max depth below rim", `${r.max_depth_m.toFixed(2)} m`],
    ["datum", datumNames[r.datum] || r.datum],
    ["datum rms residual", `${r.datum_rms_m.toFixed(2)} m`],
    ["net volume uncertainty", uncertainty],
  ];
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

  const warns = r.warnings || [];
  const wb = $("result-warnings");
  if (warns.length) {
    wb.classList.remove("hidden");
    wb.innerHTML = "<b>⚠ check before trusting the numbers:</b><ul>" +
      warns.map((w) => `<li>${w}</li>`).join("") + "</ul>";
  } else {
    wb.classList.add("hidden");
  }
  $("img-overlay").src = artifactURL("overlay.jpg", true);
  $("img-height").src = artifactURL("heightmap.png", true);
  $("img-slope").src = artifactURL("slopemap.png", true);
  $("step-result").scrollIntoView({ behavior: "smooth" });
}

export function initResult() {
  $("mat-factor").addEventListener("change", () => {
    if (state.lastResult) showResult(state.lastResult);
  });
}

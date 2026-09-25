/* Step: guided capture (GUIDED_CAPTURE_PLAN.md S3, approved slice — "sequence
 * and viewpoint guidance"). Wires the S2 backend (`POST /api/jobs/capture`,
 * `PUT/DELETE .../capture/photos/{uuid}`, `GET .../capture`,
 * `POST .../capture/finalize`) to a minimal take-a-photo UI, and turns the
 * server's per-photo (T1) and per-sequence (T2) results plus optional
 * client-side sensor readings into the actionable guidance in
 * capture_guidance.js: photo count, overlap gaps, marker views, arm
 * transition, camera height and viewpoint diversity.
 *
 * Out of this slice (see GUIDED_CAPTURE_PROGRESS.md "S3" for why): the
 * marker-placement checklist (§6), the rim-clearing step (§3 step 2), the
 * top-down sketch (§5.4, needs S4's preview SfM), and an IndexedDB
 * offline-blob queue (§10) — the server manifest (`GET .../capture`) is
 * used directly as the resume source of truth instead, per §9.1/§10's own
 * "the server manifest is the truth for accepted photos" principle.
 */
import { state, $, LS_CAPTURE_KEY, LS_KEY } from "../state.js";
import { api } from "../api.js";
import { resumeJob } from "./upload.js";
import * as cm from "../capture_metrics.js";
import * as cg from "../capture_guidance.js";

function uuidv4() {
  if (window.crypto && crypto.randomUUID) {
    try { return crypto.randomUUID(); } catch (e) { /* insecure context */ }
  }
  const b = new Uint8Array(16);
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(b);
  else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-` +
         `${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
}

// --- optional sensors (§8: advisory only, never required) -----------------
// Camera pitch below horizontal has no standard API; `beta` (front-back
// tilt) is the plan's own named proxy (§4.2 "T0 (sensor only) ... proxy
// only"). This mapping assumes the phone is held upright in its normal
// photographing stance (beta ~90deg at level); it is not corrected for
// landscape/portrait or mounting angle, same caveat the plan gives T3 as
// the reason it, not this, is authoritative.
let sensorsEnabled = false;
let latestHeading = null;
let latestPitch = null;

function onOrientation(e) {
  if (e.webkitCompassHeading != null) latestHeading = e.webkitCompassHeading;
  else if (e.alpha != null) latestHeading = e.alpha;
  if (e.beta != null) latestPitch = Math.max(-90, Math.min(90, 90 - e.beta));
}

async function enableSensors() {
  if (!window.isSecureContext || typeof DeviceOrientationEvent === "undefined") return false;
  if (typeof DeviceOrientationEvent.requestPermission === "function") {
    try {
      if ((await DeviceOrientationEvent.requestPermission()) !== "granted") return false;
    } catch (e) { return false; }
  }
  window.addEventListener("deviceorientation", onOrientation);
  sensorsEnabled = true;
  return true;
}

// --- manifest helpers -------------------------------------------------
function kept() { return state.capture.photos.filter((p) => !p.excluded); }

function seqByUuid() {
  const out = {};
  state.capture.photos.forEach((p, i) => { out[p.uuid] = i + 1; });
  return out;
}

function headingsByArm() {
  const out = { A: [], B: [] };
  for (const p of kept()) {
    const h = p.client && p.client.heading;
    if ((p.arm === "A" || p.arm === "B") && typeof h === "number") out[p.arm].push(h);
  }
  return out;
}

function pitchesByArm() {
  const out = { A: [], B: [] };
  for (const p of kept()) {
    const v = p.client && p.client.pitch;
    if ((p.arm === "A" || p.arm === "B") && typeof v === "number") out[p.arm].push(v);
  }
  return out;
}

function lastBridgeOk() {
  const bridge = [...state.capture.photos].reverse()
    .find((p) => !p.excluded && p.server && p.server.arm_bridge);
  if (!bridge) return null;
  return !(bridge.reasons || []).some((r) => r.id === "arm_bridge");
}

// --- rendering ----------------------------------------------------------
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function guidanceHTML(items) {
  if (!items.length) return "";
  return "<ul>" + items.map((g) =>
    `<li class="${g.level === "warn" ? "warn" : "note"}">${esc(g.text)}</li>`).join("") + "</ul>";
}

function renderArms() {
  const c = kept().reduce((acc, p) => { acc[p.arm] = (acc[p.arm] || 0) + 1; return acc; }, {});
  $("capture-arm-counts").textContent =
    `A: ${c.A || 0}   transition: ${c.T || 0}   B: ${c.B || 0}   total: ${kept().length}`;
  document.querySelectorAll("#capture-arm-select button").forEach((b) => {
    b.classList.toggle("primary", b.dataset.arm === state.capture.arm);
  });
}

let lastSequence = {};

function renderSequencePanel(sequence) {
  lastSequence = sequence;
  const guidance = [
    ...cg.photoCountGuidance(sequence),
    ...cg.markerViewGuidance(sequence),
    ...cg.overlapGapGuidance(sequence, seqByUuid()),
    ...cg.armTransitionGuidance(state.capture.arm, lastBridgeOk()),
  ];
  if (sensorsEnabled) {
    guidance.push(...cg.viewpointDiversityGuidance(headingsByArm()));
    guidance.push(...cg.cameraHeightGuidance(pitchesByArm()));
  }
  const mv = sequence.marker_views || {};
  $("capture-sequence").innerHTML =
    `<div class="note">marker: ${sequence.marker_views_total || 0} of ${cg.MARKER_VIEWS_MIN_TOTAL} views ` +
    `(A ${mv.A || 0}, B ${mv.B || 0})${sensorsEnabled ? "" : " · sensors off: viewpoint/height guidance unavailable"}</div>` +
    (guidance.length ? guidanceHTML(guidance) : '<div class="ok">no blocking sequence issues</div>');
  $("capture-finish").disabled = kept().length < 3;
}

function chip(label, verdict, msg) {
  if (!verdict || verdict === "ok") return "";
  const cls = verdict === "fail" ? "err" : "warn";
  return ` <span class="${cls}" title="${esc(msg || "")}">${label}: ${verdict}</span>`;
}

function renderLastPhoto(entry, t0) {
  const reasonText = (entry.reasons || []).map((r) => r.text).join(" ");
  let row = `#${entry.seq} (arm ${entry.arm}): `;
  row += chip("server", entry.verdict, reasonText) || '<span class="ok">ok</span>';
  if (t0 && t0.blurV !== "ok") row += chip("blur(T0)", t0.blurV, "blurry at capture time");
  if (entry.reasons && entry.reasons.length) {
    row += `<div class="note">${esc(reasonText)}</div>`;
  }
  $("capture-last-photo").innerHTML = row;
}

// --- session lifecycle ----------------------------------------------------
async function startSession() {
  const marker_side_m = parseFloat($("capture-marker-side").value) || 0.5;
  const marker_dict = $("capture-marker-dict").value;
  const { id } = await api("/api/jobs/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ marker_side_m, marker_dict }),
  });
  state.capture = { jobId: id, arm: "A", seq: 0, photos: [] };
  localStorage.setItem(LS_CAPTURE_KEY, id);
  $("capture-setup").classList.add("hidden");
  $("capture-session").classList.remove("hidden");
  renderArms();
  renderSequencePanel({});
}

async function loadSession(id) {
  let snap;
  try { snap = await api(`/api/jobs/${id}/capture`); }
  catch (e) { localStorage.removeItem(LS_CAPTURE_KEY); return; }
  state.capture = { jobId: id, arm: "A", seq: snap.photos.length, photos: snap.photos };
  $("capture-setup").classList.add("hidden");
  $("capture-session").classList.remove("hidden");
  renderArms();
  renderSequencePanel(snap.sequence || {});
}

async function takePhoto(file) {
  if (!file) return;
  const uuid = uuidv4();
  const seq = state.capture.seq++;
  const client = { t: Date.now() };
  if (latestHeading != null) client.heading = latestHeading;
  if (latestPitch != null) client.pitch = latestPitch;

  let t0 = null;
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const s = Math.min(1, cm.MAX_SIDE / Math.max(bitmap.width, bitmap.height));
    const w = Math.max(1, Math.round(bitmap.width * s)), h = Math.max(1, Math.round(bitmap.height * s));
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0, w, h);
    const gray = cm.toGray(ctx.getImageData(0, 0, w, h).data);
    bitmap.close();
    t0 = { blurV: cm.classifyBlur(cm.laplacianVariance(gray, w, h), null) };
  } catch (e) { /* T0 preview is best-effort; the server check is authoritative */ }

  const fd = new FormData();
  fd.append("file", file, `${uuid}.jpg`);
  fd.append("meta", JSON.stringify({ seq, arm: state.capture.arm, client }));

  $("capture-last-photo").innerHTML = '<span class="note">checking photo…</span>';
  let resp;
  try {
    resp = await api(`/api/jobs/${state.capture.jobId}/capture/photos/${uuid}`, { method: "PUT", body: fd });
  } catch (e) {
    $("capture-last-photo").innerHTML = `<span class="err">upload failed: ${esc(e.message)} — retake</span>`;
    state.capture.seq--;
    return;
  }
  const entry = { uuid, seq, arm: state.capture.arm, client, server: resp.server,
                  verdict: resp.verdict, reasons: resp.reasons, excluded: false };
  state.capture.photos.push(entry);
  renderLastPhoto(entry, t0);
  renderArms();
  renderSequencePanel(resp.sequence || {});
}

async function refreshFromServer() {
  const snap = await api(`/api/jobs/${state.capture.jobId}/capture`);
  state.capture.photos = snap.photos;
  renderArms();
  renderSequencePanel(snap.sequence || {});
}

async function retakeLast() {
  const last = [...state.capture.photos].reverse().find((p) => !p.excluded);
  if (!last) return;
  await api(`/api/jobs/${state.capture.jobId}/capture/photos/${last.uuid}`, { method: "DELETE" });
  await refreshFromServer();
}

async function finish() {
  $("capture-finish").disabled = true;
  try {
    // No preview report exists yet (S4), so finalize never actually blocks
    // on gate grounds today (server/routes.py::finalize_capture) — `override`
    // is accepted by the endpoint but has nothing to override in this slice.
    await api(`/api/jobs/${state.capture.jobId}/capture/finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ override: false }),
    });
  } catch (e) {
    $("capture-finish").disabled = false;
    alert("could not finalize: " + e.message);
    return;
  }
  localStorage.removeItem(LS_CAPTURE_KEY);
  localStorage.setItem(LS_KEY, state.capture.jobId);
  $("step-capture").classList.add("hidden");
  resumeJob(state.capture.jobId);
}

export function initCapture() {
  $("capture-start").addEventListener("click", () => startSession().catch((e) => alert(e.message)));
  $("capture-enable-sensors").addEventListener("click", async () => {
    const ok = await enableSensors();
    $("capture-sensor-note").textContent = ok
      ? "sensors on — heading/pitch guidance included below"
      : "sensors unavailable or declined — capture still works, that guidance is just skipped";
    renderSequencePanel(lastSequence);
  });
  document.querySelectorAll("#capture-arm-select button").forEach((b) => {
    b.addEventListener("click", () => {
      state.capture.arm = b.dataset.arm;
      renderArms();
      renderSequencePanel(lastSequence);
    });
  });
  $("capture-take").addEventListener("change", (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    takePhoto(f);
  });
  $("capture-retake").addEventListener("click", () => retakeLast().catch((e) => alert(e.message)));
  $("capture-finish").addEventListener("click", () => finish());

  const saved = localStorage.getItem(LS_CAPTURE_KEY);
  if (saved) loadSession(saved);
}

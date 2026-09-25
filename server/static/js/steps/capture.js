/* Step: guided capture. S3 (GUIDED_CAPTURE_PLAN.md, approved slice) wired
 * the S2 backend to a take-a-photo UI with sequence/viewpoint guidance. This
 * pass (S5, approved slice — "capture persistence and recovery") closes the
 * gap S3 explicitly left open: a killed tab or dropped connection mid-upload
 * used to lose that one photo ("the user sees the gap in the count and
 * retakes"). Now every photo is written to IndexedDB (`capture_store.js`)
 * before the upload is attempted, per §10's own stated principle:
 *
 *   "the server manifest is the truth for accepted photos. The client
 *   IndexedDB holds only what the server has not acked. Photo identity is a
 *   client-generated UUID plus sha256, so every retry is idempotent."
 *
 * This pass also restores preview status (§9.1 `capture.preview`,
 * running/stale/done/error) and finalize-crash recovery (§10 "finalize
 * interrupted") across a reload, and hands a session off to the normal
 * job view once it has moved past `capturing` — so reopening the page mid-
 * reconstruction doesn't strand the user on a capture screen that can only
 * 409 on every photo action from here on.
 *
 * Still out of this slice (S4/S5/S6b territory, unchanged): the marker
 * checklist (§6), the rim-clearing step (§3 step 2), the top-down sketch,
 * coverage/recapture instructions (§5) and a "capture report" result row —
 * the preview UI added here is only enough to trigger/observe/recover a
 * preview run, not the coverage map it will eventually show.
 */
import { state, $, LS_CAPTURE_KEY, LS_KEY } from "../state.js";
import { api } from "../api.js";
import { resumeJob } from "./upload.js";
import * as cm from "../capture_metrics.js";
import * as cg from "../capture_guidance.js";
import * as store from "../capture_store.js";

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

// --- retry-safe client persistence (§10) ---------------------------------
// Every photo is written to IndexedDB before its upload is attempted. A
// failed attempt leaves it queued; `scheduleRetry` drains the queue with
// exponential backoff (2s -> 60s cap, §10) until it empties. A reload calls
// `reconcilePending` once instead of waiting for the timer, so a session
// resumed after a crash starts retrying immediately, not after a full
// backoff delay.
let backoffMs = 2000;
let retryTimer = null;

function renderPending(n) {
  $("capture-pending").textContent = n
    ? `${n} photo${n === 1 ? "" : "s"} waiting for server checks`
    : "";
}

async function uploadEntry(uuid, blob, meta) {
  const fd = new FormData();
  fd.append("file", blob, `${uuid}.jpg`);
  fd.append("meta", JSON.stringify(meta));
  return api(`/api/jobs/${state.capture.jobId}/capture/photos/${uuid}`, { method: "PUT", body: fd });
}

function mergeManifestEntry(uuid, meta, resp) {
  if (state.capture.photos.some((p) => p.uuid === uuid)) return;
  state.capture.photos.push({ uuid, seq: meta.seq, arm: meta.arm, client: meta.client,
                              server: resp.server, verdict: resp.verdict,
                              reasons: resp.reasons, excluded: false });
  state.capture.photos.sort((a, b) => a.seq - b.seq);
}

/** Try every queued entry once, in order. Stops at the first failure (the
 * network or server is presumably still down) rather than racing the rest
 * — returns true only if the whole queue drained. */
async function drainQueue() {
  const items = await store.listPending(state.capture.jobId);
  if (!items.length) return true;
  let freshSequence = null;
  for (const item of items) {
    let resp;
    try { resp = await uploadEntry(item.uuid, item.blob, item.meta); }
    catch (e) { renderPending((await store.listPending(state.capture.jobId)).length); return false; }
    await store.ack(item.uuid);
    mergeManifestEntry(item.uuid, item.meta, resp);
    freshSequence = resp.sequence || freshSequence;
  }
  renderArms();
  renderSequencePanel(freshSequence || lastSequence);
  renderPending(0);
  return true;
}

function scheduleRetry() {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(async () => {
    const ok = await drainQueue();
    backoffMs = cg.nextBackoffMs(backoffMs, ok);
    const remaining = await store.listPending(state.capture.jobId);
    if (remaining.length) scheduleRetry();
  }, backoffMs);
}

/** On resume: ack whatever the server already has (avoids re-sending a
 * photo that made it through before the crash/refresh — "avoid duplicate
 * photos"), then retry only what's genuinely still missing, immediately
 * rather than waiting for the first backoff tick. */
async function reconcilePending(jobId, manifestPhotos, pending) {
  if (!pending.length) { renderPending(0); return; }
  const missing = new Set(cg.pendingNotYetOnServer(manifestPhotos, pending.map((p) => p.uuid)));
  for (const item of pending) {
    if (!missing.has(item.uuid)) await store.ack(item.uuid);
  }
  const remaining = await store.listPending(jobId);
  renderPending(remaining.length);
  if (!remaining.length) return;
  backoffMs = 2000;
  const ok = await drainQueue();
  if (!ok) scheduleRetry();
}

// --- preview-state restoration (§9.1 `capture.preview`, §10 "server
// restart during preview") ---------------------------------------------
let previewPollTimer = null;

function renderPreview(preview) {
  clearTimeout(previewPollTimer);
  const el = $("capture-preview");
  const runBtn = '<button id="capture-preview-run">Run preview</button>';
  if (!preview) {
    el.innerHTML = runBtn;
  } else if (preview.status === "running") {
    el.innerHTML = '<span class="note">preview reconstruction running…</span>';
    previewPollTimer = setTimeout(pollPreview, 1200);
  } else if (preview.status === "stale") {
    el.innerHTML = '<span class="warn">preview was interrupted by a server restart.</span> ' +
      '<button id="capture-preview-run">Re-run preview</button>';
  } else if (preview.status === "error") {
    el.innerHTML = `<span class="err">preview failed: ${esc(preview.error || "")}</span> ` +
      '<button id="capture-preview-run">Re-run preview</button>';
  } else {
    const blocking = (preview.report && preview.report.blocking) || [];
    el.innerHTML = (blocking.length
        ? `<span class="warn">preview: ${blocking.length} item(s) predicted to block finalize</span>`
        : '<span class="ok">preview: no blocking items predicted</span>') +
      ' <button id="capture-preview-run">Re-run preview</button>';
  }
  const btn = $("capture-preview-run");
  if (btn) btn.addEventListener("click", () => runPreview().catch((e) => alert(e.message)));
}

async function runPreview() {
  $("capture-preview").innerHTML = '<span class="note">starting preview…</span>';
  try {
    await api(`/api/jobs/${state.capture.jobId}/capture/preview`, { method: "POST" });
  } catch (e) {
    $("capture-preview").innerHTML = `<span class="err">${esc(e.message)}</span> ` +
      '<button id="capture-preview-run">Run preview</button>';
    $("capture-preview-run").addEventListener("click", () => runPreview().catch((err) => alert(err.message)));
    return;
  }
  pollPreview();
}

async function pollPreview() {
  let snap;
  try { snap = await api(`/api/jobs/${state.capture.jobId}/capture`); }
  catch (e) { previewPollTimer = setTimeout(pollPreview, 1200); return; }
  renderPreview(snap.preview);
}

// --- finalize recovery (§10 "finalize interrupted") -----------------------
function renderFinalizeRetry(errorMsg) {
  const el = $("capture-finalize-retry");
  if (!errorMsg) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="err">Finalize was interrupted: ${esc(errorMsg)}</span> ` +
    '<button id="capture-finalize-retry-btn">Retry finalize</button>';
  $("capture-finalize-retry-btn").addEventListener("click", () => finish());
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
  renderPreview(null);
  renderFinalizeRetry(null);
  renderPending(0);
}

async function loadSession(id) {
  let snap;
  try { snap = await api(`/api/jobs/${id}`); }
  catch (e) { localStorage.removeItem(LS_CAPTURE_KEY); return; }
  if (!snap.capture) { localStorage.removeItem(LS_CAPTURE_KEY); return; }

  if (snap.status !== "capturing" && snap.status !== "error") {
    // Finalize already went through (this session moved on to reconstructing
    // / ready / etc. before the reload) — the capture screen would now 409
    // on every photo action, so hand off straight to the normal job view.
    localStorage.removeItem(LS_CAPTURE_KEY);
    localStorage.setItem(LS_KEY, id);
    resumeJob(id);
    return;
  }

  const manifestPhotos = snap.capture.photos || [];
  const pending = await store.listPending(id);

  state.capture = {
    jobId: id,
    arm: cg.lastCaptureArm(manifestPhotos, pending.map((p) => p.meta)),
    seq: cg.nextCaptureSeq(manifestPhotos, pending.map((p) => p.meta)),
    photos: manifestPhotos,
  };
  $("capture-setup").classList.add("hidden");
  $("capture-session").classList.remove("hidden");
  renderArms();
  renderSequencePanel(snap.capture.sequence || {});
  renderPreview(snap.capture.preview);
  renderFinalizeRetry(snap.status === "error" && !snap.capture.finalized ? snap.error : null);

  await reconcilePending(id, manifestPhotos, pending);
}

async function takePhoto(file) {
  if (!file) return;
  const uuid = uuidv4();
  const seq = state.capture.seq++;
  const client = { t: Date.now() };
  if (latestHeading != null) client.heading = latestHeading;
  if (latestPitch != null) client.pitch = latestPitch;
  const meta = { seq, arm: state.capture.arm, client };

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

  // §10: write the blob plus metadata to IndexedDB *before* the upload
  // attempt, so a tab killed mid-request still has it to retry on reload.
  const persisted = await store.put(state.capture.jobId, uuid, file, meta);

  $("capture-last-photo").innerHTML = '<span class="note">checking photo…</span>';
  let resp;
  try {
    resp = await uploadEntry(uuid, file, meta);
  } catch (e) {
    if (persisted) {
      $("capture-last-photo").innerHTML =
        `<span class="warn">upload queued — will retry automatically (${esc(e.message)})</span>`;
      renderPending((await store.listPending(state.capture.jobId)).length);
      scheduleRetry();
    } else {
      // No IndexedDB to fall back on: same behavior as before this pass.
      $("capture-last-photo").innerHTML = `<span class="err">upload failed: ${esc(e.message)} — retake</span>`;
      state.capture.seq--;
    }
    return;
  }
  if (persisted) await store.ack(uuid);
  mergeManifestEntry(uuid, meta, resp);
  renderLastPhoto(state.capture.photos.find((p) => p.uuid === uuid), t0);
  renderArms();
  renderSequencePanel(resp.sequence || {});
  renderPending((await store.listPending(state.capture.jobId)).length);
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
  const pending = await store.listPending(state.capture.jobId);
  if (pending.length && !confirm(
    `${pending.length} photo(s) are still uploading and would not be included. Finish anyway?`)) {
    return;
  }
  $("capture-finish").disabled = true;
  try {
    // Preview blocking is enforced server-side (S4, server/routes.py::
    // finalize_capture); `override` stays false here — an "expert" override
    // control is S5/S6b's preview-panel scope, not this pass's.
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
  clearTimeout(retryTimer);
  clearTimeout(previewPollTimer);
  await store.clear(state.capture.jobId);
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

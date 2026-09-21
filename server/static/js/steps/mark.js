/* Step 3: mark the landslide region — polygon tracing (click / freehand /
 * auto-detect), zoom/pan/vertex-drag/insert/delete (F2), keyboard shortcuts,
 * top-down (ortho) toggle, prior-surface DEM import, and "compute volume". */
import { state, $ } from "../state.js";
import { api } from "../api.js";
import { storedToCanvas, dist, closestOnSegment } from "../coords.js";
import { poll } from "./upload.js";
import { showResult } from "./result.js";

const HIT_R = 10; // canvas backing-store px

let markCanvas = null;
let freehand = false, fhDragging = false;

function polygonDecorator(ctx, s) {
  const p = state.polygon;
  if (!p.length) return;
  ctx.strokeStyle = "#ffe14d"; ctx.lineWidth = 2.5; ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(p[0].x * s, p[0].y * s);
  for (const v of p.slice(1)) ctx.lineTo(v.x * s, v.y * s);
  if (state.polygonClosed) ctx.closePath();
  ctx.stroke();
  if (state.polygonClosed) {
    ctx.fillStyle = "rgba(255,225,77,0.18)";
    ctx.fill();
  }
  ctx.setLineDash([]);
  p.forEach((v, i) => {
    const sel = i === state.selectedVertex;
    ctx.fillStyle = sel ? "#ff5d5d" : "#ffe14d";
    const r = sel ? 4.5 : 3;
    ctx.fillRect(v.x * s - r, v.y * s - r, r * 2, r * 2);
  });
}

function nearestVertexIndex(cpx) {
  let best = -1, bestD = HIT_R;
  state.polygon.forEach((v, i) => {
    const c = storedToCanvas(v, markCanvas.view, markCanvas.view.zoomPan);
    const d = dist(c, cpx);
    if (d < bestD) { bestD = d; best = i; }
  });
  return best;
}

function nearestEdgeInsertIndex(cpx) {
  const p = state.polygon;
  if (p.length < 2) return -1;
  const n = state.polygonClosed ? p.length : p.length - 1;
  let best = -1, bestD = HIT_R;
  for (let i = 0; i < n; i++) {
    const a = storedToCanvas(p[i], markCanvas.view, markCanvas.view.zoomPan);
    const b = storedToCanvas(p[(i + 1) % p.length], markCanvas.view, markCanvas.view.zoomPan);
    const d = dist(cpx, closestOnSegment(cpx, a, b));
    if (d < bestD) { bestD = d; best = i + 1; }
  }
  return best;
}

function hitTest(cpx) {
  if (freehand || !state.polygon.length) return null;
  const vi = nearestVertexIndex(cpx);
  if (vi >= 0) return { kind: "vertex", index: vi };
  const ei = nearestEdgeInsertIndex(cpx);
  if (ei >= 0) return { kind: "edge", index: ei };
  return null;
}

function resetPolygon() {
  state.polygon = []; state.polygonClosed = false; state.selectedVertex = -1;
  $("poly-close").disabled = true;
  $("auto-note").textContent = "";
  markCanvas.draw(markCanvas.decorator);
}

function deleteSelectedVertex() {
  if (state.selectedVertex < 0) return;
  state.polygon.splice(state.selectedVertex, 1);
  state.selectedVertex = -1;
  if (state.polygon.length < 3) state.polygonClosed = false;
  $("poly-close").disabled = state.polygon.length < 3;
  markCanvas.draw(markCanvas.decorator);
  updateScaleStatusRef();
}

/* set by scale.js at init time to avoid a circular import (scale.js also
 * needs markCanvas/mark state indirectly via measure-btn enablement) */
let updateScaleStatusRef = () => {};
export function setScaleStatusUpdater(fn) { updateScaleStatusRef = fn; }

function updateTraceUI() {
  const ortho = state.traceMode === "ortho";
  $("mark-img").parentElement.classList.toggle("hidden", ortho);
  $("ortho-btn").classList.toggle("hidden", ortho ? !!state.ortho : true);
  if (!ortho) {
    $("ortho-note").textContent = "";
    markCanvas.load(state.markImg, 940);
  } else if (state.ortho) {
    $("ortho-note").textContent =
      `top-down view (${(state.ortho.res * 100).toFixed(1)} cm/px)`;
    markCanvas.loadURL(`/api/jobs/${state.jobId}/artifact/ortho.jpg`, 940);
  } else {
    $("ortho-note").textContent =
      "needs the dense cloud — takes ~1-3 min the first time";
  }
}

async function generateOrtho() {
  $("ortho-btn").disabled = true;
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "building top-down view…";
  try {
    await api(`/api/jobs/${state.jobId}/ortho`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    poll((snap) => {
      $("progress").classList.add("hidden");
      state.ortho = snap.ortho || null;
      $("ortho-btn").disabled = false;
      if (state.ortho) updateTraceUI();
      else if (snap.error) $("ortho-note").innerHTML = `<span class="err">${snap.error}</span>`;
    });
  } catch (e) {
    $("progress").classList.add("hidden");
    $("ortho-note").innerHTML = `<span class="err">${e.message}</span>`;
    $("ortho-btn").disabled = false;
  }
}

async function autoDetect() {
  const note = $("auto-note");
  const useOrtho = state.traceMode === "ortho";
  if (useOrtho && !state.ortho) {
    note.innerHTML = '<span class="err">generate the top-down view first</span>';
    return;
  }
  $("poly-auto").disabled = true;
  note.textContent = "detecting landslide…";
  try {
    const q = useOrtho ? "" : `?image=${encodeURIComponent(state.markImg)}`;
    const r = await api(`/api/jobs/${state.jobId}/auto-detect${q}`);
    if (!r.regions.length) {
      note.textContent = r.message || "no landslide detected — trace manually";
      return;
    }
    const best = r.regions[0];
    state.polygon = best.polygon.map(([x, y]) => ({ x, y }));
    state.polygonClosed = true;
    state.selectedVertex = -1;
    $("poly-close").disabled = true;
    updateScaleStatusRef();
    note.innerHTML =
      `detected (${(best.confidence * 100).toFixed(0)}% confidence, ` +
      `${best.polygon.length} pts on ${useOrtho ? "top-down view" : r.image}) — ` +
      `tweak by dragging vertices, clearing and retracing, or hit "Compute volume"` +
      (r.regions.length > 1 ? ` · ${r.regions.length - 1} smaller region(s) ignored` : "");
    markCanvas.draw(markCanvas.decorator);
  } catch (e) {
    note.innerHTML = `<span class="err">${e.message}</span>`;
  } finally {
    $("poly-auto").disabled = false;
  }
}

async function uploadDem() {
  const f = $("dem-input").files[0];
  const note = $("dem-note");
  if (!f) return;
  if (!state.scale || !state.scale.applied) {
    note.innerHTML = '<span class="err">set the scale first — the DEM aligns to the metric model</span>';
    return;
  }
  $("dem-btn").disabled = true;
  note.textContent = "importing & aligning DEM (trimmed ICP)…";
  try {
    const fd = new FormData();
    fd.append("file", f);
    const r = await api(`/api/jobs/${state.jobId}/dem`, { method: "POST", body: fd });
    note.innerHTML = `<span class="ok">prior surface aligned (ICP rms ` +
      `${r.rms_m.toFixed(3)} m) — volume now uses surface − DEM, no rim</span>`;
    $("dem-remove").classList.remove("hidden");
  } catch (e) {
    note.innerHTML = `<span class="err">${e.message}</span>`;
  } finally {
    $("dem-btn").disabled = false;
  }
}

async function removeDem() {
  try {
    await api(`/api/jobs/${state.jobId}/dem`, { method: "DELETE" });
    $("dem-note").textContent = "prior surface removed — rim datum back in use";
    $("dem-remove").classList.add("hidden");
  } catch (e) { /* ignore */ }
}

async function runMeasure() {
  $("measure-btn").disabled = true;
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "measuring…";
  $("step-result").classList.add("hidden");
  try {
    const body = {
      polygon: state.polygon.map((p) => [p.x, p.y]),
      dense: $("use-dense").checked,
      mode: state.traceMode,
    };
    if (state.traceMode === "photo") body.image = state.markImg;
    await api(`/api/jobs/${state.jobId}/measure`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (e) {
    $("progress").classList.add("hidden");
    $("progress-text").innerHTML = "";
    alert("could not start measurement: " + e.message);
    $("measure-btn").disabled = false;
    return;
  }
  poll((snap) => {
    $("progress").classList.add("hidden");
    if (snap.result) showResult(snap.result);
    else if (snap.error) alert("measurement failed: " + snap.error);
    $("measure-btn").disabled = false;
  });
}

export function initMark(canvas) {
  markCanvas = canvas;
  markCanvas.decorator = polygonDecorator;

  markCanvas.hitTest = hitTest;
  markCanvas.onDragMove = (hit, storedPx, isFirst) => {
    if (hit.kind === "vertex") {
      state.polygon[hit.index] = storedPx;
    } else if (isFirst) {
      state.polygon.splice(hit.index, 0, storedPx);
      $("poly-close").disabled = state.polygon.length < 3;
    } else {
      state.polygon[hit.index] = storedPx;
    }
    state.selectedVertex = hit.index;
  };
  markCanvas.onDragEnd = () => updateScaleStatusRef();
  markCanvas.onTap = (p) => {
    if (freehand) return;
    if (!state.polygonClosed) {
      state.polygon.push(p);
      $("poly-close").disabled = state.polygon.length < 3;
    } else {
      state.selectedVertex = -1;
    }
    markCanvas.draw(markCanvas.decorator);
    updateScaleStatusRef();
  };
  markCanvas.gestureEnabled = () => !freehand;

  $("mark-img").addEventListener("change", (e) => {
    if (state.traceMode !== "photo") return;
    state.markImg = e.target.value;
    resetPolygon();
    markCanvas.load(state.markImg, 940);
    updateScaleStatusRef();
  });
  $("trace-mode").addEventListener("change", (e) => {
    state.traceMode = e.target.value;
    resetPolygon();
    updateTraceUI();
    updateScaleStatusRef();
  });
  $("ortho-btn").addEventListener("click", generateOrtho);
  $("poly-auto").addEventListener("click", autoDetect);
  $("dem-btn").addEventListener("click", uploadDem);
  $("dem-remove").addEventListener("click", removeDem);
  $("dem-input").addEventListener("change", (e) => {
    $("dem-btn").disabled = !e.target.files.length;
  });
  $("view-reset")?.addEventListener("click", () => markCanvas.resetView());

  $("poly-free").addEventListener("click", () => {
    freehand = !freehand;
    $("poly-free").textContent = freehand ? "Freehand: on" : "Freehand: off";
    $("poly-free").classList.toggle("primary", freehand);
    $("freehand-note").textContent = freehand
      ? "press & drag along the boundary — release to close"
      : "";
    resetPolygon();
  });
  markCanvas.addEventListener("pointerdown", (ev) => {
    if (!freehand || state.polygonClosed) return;
    ev.preventDefault();
    markCanvas.setPointerCapture(ev.pointerId);
    fhDragging = true;
    state.polygon = [markCanvas.toOriginal(ev)];
    markCanvas.draw(markCanvas.decorator);
  });
  markCanvas.addEventListener("pointermove", (ev) => {
    if (!fhDragging) return;
    const p = markCanvas.toOriginal(ev);
    const q = state.polygon[state.polygon.length - 1];
    const drawScale = markCanvas.view.scale / (markCanvas.view.k || 1);
    if (Math.hypot(p.x - q.x, p.y - q.y) * drawScale < 3) return;
    state.polygon.push(p);
    markCanvas.draw(markCanvas.decorator);
  });
  window.addEventListener("pointerup", () => {
    if (!fhDragging) return;
    fhDragging = false;
    if (state.polygon.length >= 3) {
      if (state.polygon.length > 500) {
        const step = Math.ceil(state.polygon.length / 500);
        state.polygon = state.polygon.filter((_, i) => i % step === 0 || i === state.polygon.length - 1);
      }
      state.polygonClosed = true;
      $("poly-close").disabled = true;
      updateScaleStatusRef();
    } else {
      state.polygon = [];
    }
    markCanvas.draw(markCanvas.decorator);
  });

  $("poly-undo").addEventListener("click", () => {
    if (state.polygonClosed) { state.polygonClosed = false; }
    else state.polygon.pop();
    state.selectedVertex = -1;
    markCanvas.draw(markCanvas.decorator);
    $("poly-close").disabled = state.polygon.length < 3;
    updateScaleStatusRef();
  });
  $("poly-close").addEventListener("click", () => {
    state.polygonClosed = true;
    markCanvas.draw(markCanvas.decorator);
    updateScaleStatusRef();
  });
  $("poly-clear").addEventListener("click", resetPolygon);
  $("measure-btn").addEventListener("click", runMeasure);

  /* keyboard shortcuts (F2): Delete/Backspace removes the selected vertex,
   * Escape deselects. Ignored while typing in a form field. */
  window.addEventListener("keydown", (ev) => {
    if ($("step-mark").classList.contains("hidden")) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if ((ev.key === "Delete" || ev.key === "Backspace") && state.selectedVertex >= 0) {
      ev.preventDefault();
      deleteSelectedVertex();
    } else if (ev.key === "Escape") {
      state.selectedVertex = -1;
      markCanvas.draw(markCanvas.decorator);
    }
  });
}

export function onJobReady(snap) {
  const names = (snap.images || []).map((i) => i.name);
  const mid = names[Math.floor(names.length / 2)];
  const sel = $("mark-img");
  sel.innerHTML = "";
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  const want = state.markImg && names.includes(state.markImg) ? state.markImg : mid;
  if (want) sel.value = want;
  state.markImg = sel.value;
  updateTraceUI();
}

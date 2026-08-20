/* Landslide volume UI — no frameworks, talks to /api/jobs. */
const $ = (id) => document.getElementById(id);
const LS_KEY = "lsv-last-job";
const state = {
  jobId: null,
  images: [],          // [{name, width, height, points}]
  scale: null,         // scale info from server
  ortho: null,         // orthophoto metadata (when rendered)
  manual: { a: { img: null, pts: [] }, b: { img: null, pts: [] } },
  markImg: null,
  traceMode: "photo",  // "photo" | "ortho"
  polygon: [],         // original-image px (or ortho px)
  polygonClosed: false,
  lastResult: null,
};

/* ---------- helpers ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
const photoURL = (name, w) => `/api/jobs/${state.jobId}/photo/${name}?w=${w || 1400}`;

function fillSelect(sel, names, current) {
  sel.innerHTML = "";
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  if (current && names.includes(current)) sel.value = current;
}

/* ---------- canvases (click-to-mark) ---------- */
function setupCanvas(canvas) {
  const ctx = canvas.getContext("2d");
  // view.k = stored-photo px per served-image px. Photos are SERVED at
  // <=1400px (see /photo endpoint) but clicks/polygons must reach the server
  // in the ORIGINAL stored resolution, or every point lands ~2x off on
  // full-size phone photos.
  const view = { img: null, name: null, scale: 1, k: 1 };
  canvas.draw = function (decorator) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!view.img) return;
    canvas.width = view.img.naturalWidth * view.scale;
    canvas.height = view.img.naturalHeight * view.scale;
    ctx.drawImage(view.img, 0, 0, canvas.width, canvas.height);
    // decorators receive points in STORED px: stored -> canvas = scale / k
    if (decorator) decorator(ctx, view.scale / (view.k || 1));
  };
  canvas.loadURL = function (url, displayW, k = 1) {
    view.name = url;
    view.img = null;
    view.k = k;
    const img = new Image();
    img.onload = () => {
      view.img = img;
      view.scale = Math.min(1, displayW / img.naturalWidth);
      canvas.draw(canvas.decorator);
    };
    img.src = url;
  };
  canvas.load = function (name, displayW) {
    const meta = (state.images || []).find((i) => i.name === name);
    const k = meta ? meta.width / Math.min(1400, meta.width) : 1;
    canvas.loadURL(photoURL(name), displayW, k);
  };
  canvas.toOriginal = (ev) => {
    const r = canvas.getBoundingClientRect();
    const sx = canvas.width / r.width;
    const sy = canvas.height / r.height;
    // canvas px -> served px (÷scale) -> stored px (×k)
    return {
      x: (ev.clientX - r.left) * sx / view.scale * (view.k || 1),
      y: (ev.clientY - r.top) * sy / view.scale * (view.k || 1),
    };
  };
  canvas.view = view;
  return canvas;
}

/* polygon decorator for the marking canvas */
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
  for (const v of p) {
    ctx.fillStyle = "#ffe14d";
    ctx.fillRect(v.x * s - 3, v.y * s - 3, 6, 6);
  }
}

function manualDecorator(which) {
  return (ctx, s) => {
    const m = state.manual[which];
    if (!m.pts.length) return;
    ctx.strokeStyle = "#6fd6ff"; ctx.lineWidth = 2.5;
    if (m.pts.length === 2) {
      ctx.beginPath();
      ctx.moveTo(m.pts[0].x * s, m.pts[0].y * s);
      ctx.lineTo(m.pts[1].x * s, m.pts[1].y * s);
      ctx.stroke();
    }
    m.pts.forEach((p, i) => {
      ctx.fillStyle = "#6fd6ff";
      ctx.beginPath();
      ctx.arc(p.x * s, p.y * s, 6, 0, 7);
      ctx.fill();
      ctx.fillStyle = "#04121f"; ctx.font = "bold 11px sans-serif";
      ctx.fillText(`p${i + 1}`, p.x * s + 9, p.y * s - 7);
    });
  };
}

/* ---------- upload & polling ---------- */
let pollTimer = null;
function poll(onDone) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    let snap;
    try {
      snap = await api(`/api/jobs/${state.jobId}`);
    } catch (e) {
      $("progress-text").textContent = "connection lost, retrying…";
      poll(onDone); return;
    }
    $("progress-log").textContent = snap.log.slice(-14).join("\n");
    state.ortho = snap.ortho || state.ortho;
    const busy = ["reconstructing", "measuring", "orthorectifying"]
      .includes(snap.status);
    // ready but no images yet = ctx still reloading after a server restart
    const loading = snap.status === "ready" && !(snap.images || []).length;
    if (busy || loading) {
      $("progress-text").textContent =
        snap.status === "reconstructing" ? "reconstructing (SfM)…" :
        snap.status === "measuring" ? "measuring…" :
        snap.status === "orthorectifying" ? "building top-down view…" :
        "loading reconstruction…";
      poll(onDone);
    } else {
      onDone(snap);
    }
  }, 1200);
}

async function upload(files) {
  if (files.length < 3) {
    $("progress").classList.remove("hidden");
    $("progress-text").innerHTML =
      '<span class="err">select at least 3 photos (15–60 recommended)</span>';
    return;
  }
  if (files.length > 200) {
    $("progress").classList.remove("hidden");
    $("progress-text").innerHTML =
      '<span class="err">too many photos (max 200)</span>';
    return;
  }
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "uploading…";
  $("progress-log").textContent = "";
  try {
    const { id } = await api("/api/jobs", { method: "POST", body: fd });
    localStorage.setItem(LS_KEY, id);
    switchJob(id, { keepProgress: true });
    poll((snap) => {
      if (snap.status === "error") {
        $("progress-text").innerHTML = `<span class="err">failed: ${snap.error}</span>`;
        return;
      }
      $("progress").classList.add("hidden");
      onReady(snap);
    });
  } catch (e) {
    $("progress-text").innerHTML = `<span class="err">upload failed: ${e.message}</span>`;
  }
}

/* reset per-job UI state and (re)populate everything from a snapshot */
function switchJob(id, { keepProgress = false } = {}) {
  clearTimeout(pollTimer);
  state.jobId = id;
  localStorage.setItem(LS_KEY, id);
  state.polygon = []; state.polygonClosed = false;
  state.manual = { a: { img: null, pts: [] }, b: { img: null, pts: [] } };
  state.scale = null; state.images = []; state.ortho = null;
  state.lastResult = null;
  $("step-result").classList.add("hidden");
  $("step-scale").classList.add("hidden");
  $("step-mark").classList.add("hidden");
  $("poly-close").disabled = true;
  $("measure-btn").disabled = true;
  if (!keepProgress) {
    $("progress").classList.add("hidden");
  }
}

function onReady(snap) {
  state.images = snap.images || [];
  state.scale = snap.scale;
  state.ortho = snap.ortho || null;
  const names = state.images.map((i) => i.name);
  $("step-scale").classList.remove("hidden");
  $("step-mark").classList.remove("hidden");
  updateScaleStatus();

  fillSelect($("man-img-a"), names, names[1] || names[0]);
  fillSelect($("man-img-b"), names, names[names.length - 2] || names[0]);
  const mid = names[Math.floor(names.length / 2)];
  fillSelect($("mark-img"), names, state.markImg && names.includes(state.markImg)
    ? state.markImg : mid);
  state.markImg = $("mark-img").value;
  state.manual.a.img = $("man-img-a").value;
  state.manual.b.img = $("man-img-b").value;

  $("man-canvas-a").load(state.manual.a.img, 460);
  $("man-canvas-b").load(state.manual.b.img, 460);
  updateTraceUI();
  if (snap.result) showResult(snap.result);
  refreshJobList();
}

/* ortho/photo tracing toggle + ortho generation */
function updateTraceUI() {
  const ortho = state.traceMode === "ortho";
  $("mark-img").parentElement.classList.toggle("hidden", ortho);
  $("ortho-btn").classList.toggle("hidden", ortho ? !!state.ortho : true);
  if (!ortho) {
    $("ortho-note").textContent = "";
    $("mark-canvas").load(state.markImg, 940);
  } else if (state.ortho) {
    $("ortho-note").textContent =
      `top-down view (${(state.ortho.res * 100).toFixed(1)} cm/px)`;
    $("mark-canvas").loadURL(
      `/api/jobs/${state.jobId}/artifact/ortho.jpg`, 940);
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
      method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    poll((snap) => {
      $("progress").classList.add("hidden");
      state.ortho = snap.ortho || null;
      $("ortho-btn").disabled = false;
      if (state.ortho) updateTraceUI();
      else if (snap.error)
        $("ortho-note").innerHTML = `<span class="err">${snap.error}</span>`;
    });
  } catch (e) {
    $("progress").classList.add("hidden");
    $("ortho-note").innerHTML = `<span class="err">${e.message}</span>`;
    $("ortho-btn").disabled = false;
  }
}

/* AI region detection: fills the polygon from hosted segmentation; the
   result is editable like any manual trace (Clear / freehand still work) */
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
    $("poly-close").disabled = true;
    updateScaleStatus();
    note.innerHTML =
      `detected (${(best.confidence * 100).toFixed(0)}% confidence, ` +
      `${best.polygon.length} pts on ${useOrtho ? "top-down view" : r.image}) — ` +
      `tweak by clearing and retracing, or hit “Compute volume”` +
      (r.regions.length > 1
        ? ` · ${r.regions.length - 1} smaller region(s) ignored` : "");
    document.querySelectorAll("#step-mark canvas").forEach((c) =>
      c.draw(c.decorator));
  } catch (e) {
    note.innerHTML = `<span class="err">${e.message}</span>`;
  } finally {
    $("poly-auto").disabled = false;
  }
}

/* resume the last job (or one picked from the list) after a page reload */
async function resumeJob(id) {
  switchJob(id);
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "reconnecting…";
  poll((snap) => {
    if (snap.status === "error") {
      $("progress-text").innerHTML =
        `<span class="err">job failed: ${snap.error}</span>`;
      $("step-scale").classList.remove("hidden");
      return;
    }
    $("progress").classList.add("hidden");
    onReady(snap);
  });
}

/* ---------- scaling ---------- */
function updateScaleStatus() {
  const el = $("scale-status");
  if (state.scale && state.scale.applied) {
    const m = state.scale.method === "aruco"
      ? `ArUco id ${state.scale.marker_id} in ${state.scale.views_used.length} photos`
      : `manual segment (${state.scale.images.join(" + ")})`;
    const acc = state.scale.scale_rel_error
      ? ` · scale accuracy ± ${(state.scale.scale_rel_error * 100).toFixed(1)}%` : "";
    el.innerHTML = `scale set ✅ ${m} — 1 model unit = ${state.scale.scale.toPrecision(4)} m${acc}`;
  } else {
    el.innerHTML = '<span class="err">scale not set yet</span>';
  }
  $("measure-btn").disabled = !(state.polygonClosed && state.scale);
}

async function arucoDetect() {
  const body = {
    side_m: parseFloat($("aruco-side").value),
    dict: $("aruco-dict").value,
  };
  $("aruco-result").textContent = "detecting…";
  try {
    const info = await api(`/api/jobs/${state.jobId}/scale/aruco`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.scale = { ...info, applied: true };
    $("aruco-result").innerHTML =
      `<span class="ok">found ${info.dict} id ${info.marker_id} in ` +
      `${info.views_used.length} photos · scale ${info.scale.toPrecision(4)} m/unit` +
      (info.side_spread_rel > 0.03 ? " · ⚠ corner spread high" : "") + "</span>";
  } catch (e) {
    $("aruco-result").innerHTML = `<span class="err">${e.message}</span>`;
  }
  updateScaleStatus();
}

async function manualApply() {
  const a = state.manual.a, b = state.manual.b;
  if (a.pts.length !== 2 || b.pts.length !== 2) {
    $("man-result").innerHTML = '<span class="err">click both endpoints in BOTH photos</span>';
    return;
  }
  const body = {
    length_m: parseFloat($("man-length").value),
    a: { image: a.img, p1: [a.pts[0].x, a.pts[0].y], p2: [a.pts[1].x, a.pts[1].y] },
    b: { image: b.img, p1: [b.pts[0].x, b.pts[0].y], p2: [b.pts[1].x, b.pts[1].y] },
  };
  try {
    const info = await api(`/api/jobs/${state.jobId}/scale/manual`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.scale = { ...info, applied: true };
    const warn = (info.warnings || []).length
      ? " · ⚠ " + info.warnings.join(" · ") : "";
    $("man-result").innerHTML =
      `<span class="ok">scale ${info.scale.toPrecision(4)} m/unit set` +
      ` (± ${(info.scale_rel_error * 100).toFixed(1)}%)</span>${warn ? `<br>${warn}` : ""}`;
  } catch (e) {
    $("man-result").innerHTML = `<span class="err">${e.message}</span>`;
  }
  updateScaleStatus();
}

/* ---------- measure ---------- */
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
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

function showResult(r) {
  $("step-result").classList.remove("hidden");
  state.lastResult = r;
  const swell = parseFloat($("mat-factor").value) || 1.0;
  const datumNames = {
    rim_plane: "plane fitted to the rim",
    rim_quad: "curved surface (paraboloid) fitted to the rim",
    surface_plane: "region surface itself (no rim!)",
  };
  const rows = [
    ["net volume (fill − cut)", `${r.net_volume_m3.toFixed(1)} m³`],
    ["cut (depression below rim)", `${r.cut_volume_m3.toFixed(1)} m³`],
    ["fill (material above rim)", `${r.fill_volume_m3.toFixed(1)} m³`],
    ["area", `${r.area_m2.toFixed(1)} m²`],
    ["max depth below rim", `${r.max_depth_m.toFixed(2)} m`],
    ["datum", datumNames[r.datum] || r.datum],
    ["datum rms residual", `${r.datum_rms_m.toFixed(2)} m`],
    ["≈ volume uncertainty", `± ${r.est_volume_error_m3.toFixed(0)} m³`],
  ];
  if (swell > 1.0) {
    rows.push(["loose volume to haul (cut × swell)",
               `${(r.cut_volume_m3 * swell).toFixed(1)} m³`]);
    rows.push(["loose volume to haul (|net| × swell)",
               `${(Math.abs(r.net_volume_m3) * swell).toFixed(1)} m³`]);
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
  const bust = `?t=${Date.now()}`;
  $("img-overlay").src = `/api/jobs/${state.jobId}/artifact/overlay.jpg${bust}`;
  $("img-height").src = `/api/jobs/${state.jobId}/artifact/heightmap.png${bust}`;
  $("step-result").scrollIntoView({ behavior: "smooth" });
}

/* ---------- job list ---------- */
async function refreshJobList() {
  try {
    const jobs = await api("/api/jobs");
    if (!jobs.length) return;
    $("job-list").innerHTML = "recent jobs: " + jobs.slice(0, 6).map((j) => {
      const cls = j.id === state.jobId ? "jobchip cur" : "jobchip";
      const label = `${j.id.slice(9)} · ${j.status}` +
        (j.n_photos ? ` · ${j.n_photos}p` : "") + (j.has_result ? " ✓" : "");
      return `<button class="${cls}" data-job="${j.id}">${label}</button>` +
        `<button class="link" data-del="${j.id}" title="delete job">✕</button>`;
    }).join(" ");
  } catch (e) { /* ignore */ }
}

/* ---------- wire up ---------- */
window.addEventListener("DOMContentLoaded", async () => {
  const markCanvas = setupCanvas($("mark-canvas"));
  const manA = setupCanvas($("man-canvas-a"));
  const manB = setupCanvas($("man-canvas-b"));
  markCanvas.decorator = polygonDecorator;
  manA.decorator = manualDecorator("a");
  manB.decorator = manualDecorator("b");

  $("file-input").addEventListener("change", (e) => {
    const n = e.target.files.length;
    $("upload-btn").disabled = n < 3;
    $("upload-note").textContent = n === 0
      ? "select photos in capture order (left → right)"
      : `${n} photo${n === 1 ? "" : "s"} selected` +
        (n < 3 ? " — need at least 3" : "");
  });
  $("upload-btn").addEventListener("click", () => upload($("file-input").files));

  document.querySelectorAll('input[name=scalemode]').forEach((r) => {
    r.addEventListener("change", () => {
      const aruco = document.querySelector('input[name=scalemode]:checked').value === "aruco";
      $("scale-aruco").classList.toggle("hidden", !aruco);
      $("scale-manual").classList.toggle("hidden", aruco);
    });
  });
  $("aruco-btn").addEventListener("click", arucoDetect);
  $("man-btn").addEventListener("click", manualApply);

  const manSelect = (which, sel, canvas) => {
    sel.addEventListener("change", () => {
      state.manual[which].img = sel.value;
      state.manual[which].pts = [];
      $(`man-clicks-${which}`).textContent = "0 / 2";
      canvas.load(sel.value, 460);
    });
    canvas.addEventListener("click", (ev) => {
      if (state.manual[which].pts.length >= 2) return;
      state.manual[which].pts.push(canvas.toOriginal(ev));
      $(`man-clicks-${which}`).textContent = `${state.manual[which].pts.length} / 2`;
      canvas.draw(canvas.decorator);
    });
  };
  manSelect("a", $("man-img-a"), manA);
  manSelect("b", $("man-img-b"), manB);
  document.querySelectorAll("[data-clear]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const w = btn.dataset.clear;
      state.manual[w].pts = [];
      $(`man-clicks-${w}`).textContent = "0 / 2";
      (w === "a" ? manA : manB).draw((w === "a" ? manA : manB).decorator);
    }));

  $("mark-img").addEventListener("change", (e) => {
    if (state.traceMode !== "photo") return;
    state.markImg = e.target.value;
    resetPolygon();
    markCanvas.load(state.markImg, 940);
    updateScaleStatus();
  });
  $("trace-mode").addEventListener("change", (e) => {
    state.traceMode = e.target.value;
    resetPolygon();
    updateTraceUI();
    updateScaleStatus();
  });
  $("ortho-btn").addEventListener("click", generateOrtho);
  $("poly-auto").addEventListener("click", autoDetect);
  $("mat-factor").addEventListener("change", () => {
    if (state.lastResult) showResult(state.lastResult);
  });
  const resetPolygon = () => {
    state.polygon = []; state.polygonClosed = false;
    $("poly-close").disabled = true;
    $("auto-note").textContent = "";
    markCanvas.draw(markCanvas.decorator);
  };

  /* freehand tracing: hold the mouse down and draw along the boundary;
     releasing closes the polygon (click-to-add-point stays available) */
  let freehand = false, fhDragging = false;
  $("poly-free").addEventListener("click", () => {
    freehand = !freehand;
    $("poly-free").textContent = freehand ? "Freehand: on" : "Freehand: off";
    $("poly-free").classList.toggle("primary", freehand);
    $("freehand-note").textContent = freehand
      ? "press & drag along the boundary — release to close"
      : "";
    resetPolygon();
  });
  markCanvas.addEventListener("mousedown", (ev) => {
    if (!freehand || state.polygonClosed) return;
    ev.preventDefault();
    fhDragging = true;
    state.polygon = [markCanvas.toOriginal(ev)];
    markCanvas.draw(markCanvas.decorator);
  });
  markCanvas.addEventListener("mousemove", (ev) => {
    if (!fhDragging) return;
    const p = markCanvas.toOriginal(ev);
    const q = state.polygon[state.polygon.length - 1];
    const drawScale = markCanvas.view.scale / (markCanvas.view.k || 1);
    if (Math.hypot(p.x - q.x, p.y - q.y) * drawScale < 3) return;
    state.polygon.push(p);
    markCanvas.draw(markCanvas.decorator);
  });
  window.addEventListener("mouseup", () => {
    if (!fhDragging) return;
    fhDragging = false;
    if (state.polygon.length >= 3) {
      if (state.polygon.length > 500) {          // thin very dense paths
        const step = Math.ceil(state.polygon.length / 500);
        state.polygon = state.polygon.filter(
          (_, i) => i % step === 0 || i === state.polygon.length - 1);
      }
      state.polygonClosed = true;
      $("poly-close").disabled = true;
      updateScaleStatus();
    } else {
      state.polygon = [];
    }
    markCanvas.draw(markCanvas.decorator);
  });

  markCanvas.addEventListener("click", (ev) => {
    if (freehand || state.polygonClosed) return;
    state.polygon.push(markCanvas.toOriginal(ev));
    markCanvas.draw(markCanvas.decorator);
    $("poly-close").disabled = state.polygon.length < 3;
  });
  $("poly-undo").addEventListener("click", () => {
    if (state.polygonClosed) { state.polygonClosed = false; }
    else state.polygon.pop();
    markCanvas.draw(markCanvas.decorator);
    $("poly-close").disabled = state.polygon.length < 3;
    updateScaleStatus();
  });
  $("poly-close").addEventListener("click", () => {
    state.polygonClosed = true;
    markCanvas.draw(markCanvas.decorator);
    updateScaleStatus();
  });
  $("poly-clear").addEventListener("click", () => {
    state.polygon = []; state.polygonClosed = false;
    markCanvas.draw(markCanvas.decorator);
    $("poly-close").disabled = true;
    $("auto-note").textContent = "";
    updateScaleStatus();
  });
  $("measure-btn").addEventListener("click", runMeasure);

  $("job-list").addEventListener("click", async (ev) => {
    const del = ev.target.closest("[data-del]");
    if (del) {
      if (!confirm("delete this job and its photos?")) return;
      await api(`/api/jobs/${del.dataset.del}`, { method: "DELETE" });
      if (del.dataset.del === state.jobId) window.location.reload();
      refreshJobList();
      return;
    }
    const chip = ev.target.closest("[data-job]");
    if (chip && chip.dataset.job !== state.jobId) resumeJob(chip.dataset.job);
  });

  refreshJobList();

  // resume the last session's job after a page refresh
  const saved = localStorage.getItem(LS_KEY);
  if (saved) {
    try {
      await api(`/api/jobs/${saved}`);
      resumeJob(saved);
    } catch (e) {
      localStorage.removeItem(LS_KEY);
    }
  }
});

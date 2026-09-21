/* Step 2: set the metric scale — ArUco marker (auto) or a manual two-point
 * reference measured in two photos. */
import { state, $ } from "../state.js";
import { api } from "../api.js";
import { setScaleStatusUpdater } from "./mark.js";

let manA = null, manB = null;

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

export function updateScaleStatus() {
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
  const body = { side_m: parseFloat($("aruco-side").value), dict: $("aruco-dict").value };
  $("aruco-result").textContent = "detecting…";
  try {
    const info = await api(`/api/jobs/${state.jobId}/scale/aruco`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
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
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    state.scale = { ...info, applied: true };
    const warn = (info.warnings || []).length ? " · ⚠ " + info.warnings.join(" · ") : "";
    $("man-result").innerHTML =
      `<span class="ok">scale ${info.scale.toPrecision(4)} m/unit set` +
      ` (± ${(info.scale_rel_error * 100).toFixed(1)}%)</span>${warn ? `<br>${warn}` : ""}`;
  } catch (e) {
    $("man-result").innerHTML = `<span class="err">${e.message}</span>`;
  }
  updateScaleStatus();
}

export function initScale(canvasA, canvasB) {
  manA = canvasA; manB = canvasB;
  manA.decorator = manualDecorator("a");
  manB.decorator = manualDecorator("b");
  setScaleStatusUpdater(updateScaleStatus);

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
    canvas.onTap = (p) => {
      if (state.manual[which].pts.length >= 2) return;
      state.manual[which].pts.push(p);
      $(`man-clicks-${which}`).textContent = `${state.manual[which].pts.length} / 2`;
      canvas.draw(canvas.decorator);
    };
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
}

function fillSelect(sel, names, current) {
  sel.innerHTML = "";
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  if (current && names.includes(current)) sel.value = current;
}

export function onJobReady(snap) {
  state.scale = snap.scale;
  const names = (snap.images || []).map((i) => i.name);
  fillSelect($("man-img-a"), names, names[1] || names[0]);
  fillSelect($("man-img-b"), names, names[names.length - 2] || names[0]);
  state.manual.a.img = $("man-img-a").value;
  state.manual.b.img = $("man-img-b").value;
  manA.load(state.manual.a.img, 460);
  manB.load(state.manual.b.img, 460);
  updateScaleStatus();
}

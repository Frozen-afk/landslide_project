/* Step 3, multi-view marking: outline the SAME landslide on 2 photos from
 * each of 4 viewpoints (POV groups). The server owns the outlines
 * (/api/jobs/{id}/annotations, persisted in state.json) and everything
 * derived from them — POV groups, progress, suggestions, agreement, the
 * reprojected 3D region; this module renders that and saves edits.
 *
 * Saves are idempotent PUTs keyed by photo, queued per photo (last write
 * wins) and retried until they land, so a dropped connection, a double
 * click or a reload mid-edit cannot lose or duplicate an outline. */
import { state, $ } from "../state.js";
import { api } from "../api.js";

const SAVE_DEBOUNCE_MS = 600;
const RETRY_MS = 3000;

let hooks = { openImage: () => {}, redraw: () => {}, refreshMeasure: () => {} };
const pending = new Map();      // image -> request body awaiting a successful PUT
let saveTimer = null, flushing = null, loadSeq = 0;

const mv = () => state.mv;
const annURL = (image) =>
  `/api/jobs/${state.jobId}/annotations` + (image ? `/${encodeURIComponent(image)}` : "");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function resetMultiview() {
  pending.clear();
  clearTimeout(saveTimer);
  state.mv = { annotations: {}, summary: null, rev: 0, projected: null, resultRev: null };
}

/* ---- saving ---- */
function setSaveNote(html) { $("mv-save-note").innerHTML = html; }

function queueSave(image, body, now = false) {
  pending.set(image, body);
  mv().annotations[image] = { ...body };          // optimistic; server copy replaces it
  clearTimeout(saveTimer);
  if (now) return flush();
  setSaveNote("saving…");
  saveTimer = setTimeout(flush, SAVE_DEBOUNCE_MS);
  return Promise.resolve(false);
}

/** Send every queued outline. Resolves true when nothing is left unsaved. */
export function flush() {
  clearTimeout(saveTimer);
  if (flushing) return flushing.then(flush);
  flushing = (async () => {
    const jobId = state.jobId;
    for (const [image, body] of [...pending]) {
      try {
        const r = await api(annURL(image), {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (state.jobId !== jobId) return true;   // job switched mid-save
        if (pending.get(image) === body) pending.delete(image);   // not re-edited meanwhile
        mv().annotations[image] = r.annotation;
        mv().rev = r.rev;
      } catch (e) {
        // 4xx = the outline itself was refused: retrying cannot help
        if (e.status >= 400 && e.status < 500 && e.status !== 409) {
          pending.delete(image);
          setSaveNote(`<span class="err">${esc(image)} not saved: ${esc(e.message)}</span>`);
          return false;
        }
        setSaveNote('<span class="warn">not saved yet (connection?) — retrying…</span>');
        saveTimer = setTimeout(flush, RETRY_MS);
        return false;
      }
    }
    setSaveNote(pending.size ? "saving…" : '<span class="ok">all outlines saved</span>');
    return pending.size === 0;
  })();
  const p = flushing;
  p.finally(() => { if (flushing === p) flushing = null; });
  return p;
}

/* ---- loading ---- */
export async function refresh() {
  const seq = ++loadSeq, jobId = state.jobId;
  let r;
  try {
    r = await api(annURL());
  } catch (e) {
    $("mv-instructions").innerHTML = `<span class="err">could not load outlines: ${esc(e.message)}</span>`;
    return false;
  }
  if (seq !== loadSeq || jobId !== state.jobId) return false;   // superseded
  // keep local edits that have not reached the server yet
  const local = mv().annotations;
  mv().annotations = r.annotations;
  for (const image of pending.keys()) mv().annotations[image] = local[image];
  mv().rev = r.rev;
  mv().summary = r.summary;
  render();
  hooks.refreshMeasure();
  return true;
}

async function fetchProjected(image) {
  const jobId = state.jobId;
  mv().projected = null;
  try {
    const r = await api(annURL(image));
    if (state.markImg !== image || state.jobId !== jobId) return;
    mv().projected = r.projected;
  } catch (e) { /* overlay is optional */ }
  $("mv-suggest").disabled = !(mv().projected && mv().projected.best);
  hooks.redraw();
}

/** Outline stored for `image` as canvas state, or null. */
export function polygonFor(image) {
  const a = mv().annotations[image];
  if (!a || !a.polygon || a.polygon.length < 3) return null;
  return a.polygon.map(([x, y]) => ({ x, y }));
}

export function onImageOpened(image) {
  render();
  fetchProjected(image);
}

/* ---- edits coming from the canvas (mark.js) ---- */
export function polygonEdited() {
  const image = state.markImg;
  if (!image || !state.polygonClosed || state.polygon.length < 3) { render(); return; }
  const prev = mv().annotations[image];
  queueSave(image, {
    polygon: state.polygon.map((p) => [p.x, p.y]),
    status: prev && prev.status === "accepted" ? "accepted" : "draft",
    source: prev ? prev.source || "manual" : "manual",
  });
  render();
}

async function afterChange() {
  if (await flush()) await refresh();
  if (state.markImg) fetchProjected(state.markImg);
}

async function accept() {
  const image = state.markImg;
  if (!state.polygonClosed || state.polygon.length < 3) return;
  const prev = mv().annotations[image];
  queueSave(image, {
    polygon: state.polygon.map((p) => [p.x, p.y]), status: "accepted",
    source: prev ? prev.source || "manual" : "manual",
  }, true);
  render();
  await afterChange();
}

async function skip() {
  const image = state.markImg;
  const closed = state.polygonClosed && state.polygon.length >= 3;
  queueSave(image, {
    polygon: closed ? state.polygon.map((p) => [p.x, p.y]) : [], status: "skipped",
    source: "manual",
  }, true);
  await afterChange();
  next();
}

async function remove() {
  const image = state.markImg;
  pending.delete(image);
  try {
    const r = await api(annURL(image), { method: "DELETE" });
    delete mv().annotations[image];
    mv().rev = r.rev;
  } catch (e) {
    setSaveNote(`<span class="err">could not delete: ${esc(e.message)}</span>`);
    return;
  }
  hooks.openImage(image);            // reloads the (now empty) outline
  await refresh();
}

function useSuggestion() {
  const best = mv().projected && mv().projected.best;
  if (!best) return;
  state.polygon = best.map(([x, y]) => ({ x, y }));
  state.polygonClosed = true;
  state.selectedVertex = -1;
  const image = state.markImg, prev = mv().annotations[image];
  queueSave(image, {
    polygon: best, status: prev && prev.status === "accepted" ? "accepted" : "draft",
    source: "suggested",
  });
  render();
  hooks.redraw();
  hooks.refreshMeasure();
}

function next() {
  const sug = (mv().summary && mv().summary.suggestions) || [];
  const s = sug.find((x) => x.image !== state.markImg);
  if (s) hooks.openImage(s.image);
}

/* ---- measuring ---- */
export function accepted() {
  return Object.values(mv().annotations).filter((a) => a.status === "accepted").length;
}

export function requirementMet() {
  return !!(mv().summary && mv().summary.requirement.met);
}

/** A final measurement needs 4 viewpoints x 2 photos; fewer only as an
 * explicitly indicative result. */
export function canMeasure() {
  return requirementMet() || ($("mv-indicative").checked && accepted() >= 1);
}

export const wantsIndicative = () => !requirementMet() && $("mv-indicative").checked;

export function setResultRev(result, snapRev) {
  mv().resultRev = result && result.multiview ? result.multiview.annotations_rev : null;
  if (typeof snapRev === "number") mv().rev = snapRev;
  render();
}

/* ---- canvas overlay: the current 3D region reprojected into this photo ---- */
export function decorate(ctx, s) {
  const p = mv().projected;
  if (!p) return;
  const ring = (pts, color, dash, width) => {
    if (!pts || pts.length < 3) return;
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(pts[0][0] * s, pts[0][1] * s);
    for (const q of pts.slice(1)) ctx.lineTo(q[0] * s, q[1] * s);
    ctx.closePath();
    ctx.stroke();
  };
  ring(p.upper, "#ff9d3c", [3, 4], 1.5);     // outer limit of the uncertain boundary
  ring(p.lower, "#4fd964", [], 1.5);         // confident interior
  ring(p.best, "#4fd0ff", [8, 5], 2);        // best estimate = the editable suggestion
  ctx.setLineDash([]);
}

/* ---- panel ---- */
function agreementText(image) {
  const a = mv().summary && mv().summary.agreement[image];
  if (!a) return "";
  const pct = a.iou == null ? "" : ` ${(a.iou * 100).toFixed(0)}%`;
  if (a.rejected) return ` · <span class="err">⚠ disagrees with the others${pct} — excluded, redraw it</span>`;
  if (a.weight < 1) return ` · <span class="warn">⚠ partly disagrees${pct} — down-weighted</span>`;
  let t = a.iou == null ? "" : ` · <span class="ok">agrees${pct}</span>`;
  if (a.visible_frac < 0.3) t += ` · <span class="warn">region mostly hidden here</span>`;
  if (a.zbuf_support < 0.5) t += ` · <span class="warn">little 3-D surface under it</span>`;
  return t;
}

function render() {
  const sm = mv().summary;
  const cur = state.markImg;
  const curAnn = mv().annotations[cur];
  const closed = state.polygonClosed && state.polygon.length >= 3;
  $("mv-accept").disabled = !closed || (curAnn && curAnn.status === "accepted" && !pending.has(cur));
  $("mv-delete").disabled = !curAnn;
  $("mv-suggest").disabled = !(mv().projected && mv().projected.best);
  if (!sm) return;

  const req = sm.requirement;
  const curGroup = sm.views[cur] ? sm.views[cur].group : null;
  const chips = sm.groups.map((g) => {
    const done = g.n_accepted >= req.min_per_group;
    const cls = "jobchip" + (g.id === curGroup ? " cur" : "");
    return `<button class="${cls}" data-mv-group="${g.id}" title="viewpoint P${g.id}: ` +
      `${g.azimuth_deg.toFixed(0)}° around, ${g.elevation_deg.toFixed(0)}° above · ${g.n_images} photos">` +
      `P${g.id} <span class="${done ? "ok" : g.n_accepted ? "warn" : "note"}">` +
      `${g.n_accepted}/${req.min_per_group}${done ? " ✓" : ""}</span></button>`;
  }).join(" ");
  const head = req.met
    ? `<span class="ok">✓ ${req.n_groups_complete} viewpoints × ${req.min_per_group} photos outlined — ready for a final measurement</span>`
    : `<span class="warn">${req.n_groups_complete} of ${req.min_groups} viewpoints complete · ` +
      `${req.n_annotations} of ${req.min_annotations} outlines</span>`;
  $("mv-progress").innerHTML = `<div>${head}</div><div class="mv-chips">${chips}</div>`;

  const todo = sm.instructions.map((t) => `<li>${esc(t)}</li>`).join("");
  $("mv-instructions").innerHTML = todo ? `<ul>${todo}</ul>` : "";

  const f = sm.fusion;
  let fus = "";
  if (f) {
    const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
    const area = state.scale && state.scale.applied
      ? `confident ${f.area_lower_m2.toFixed(1)} m² · best ${f.area_best_m2.toFixed(1)} m² · ` +
        `outer limit ${f.area_upper_m2.toFixed(1)} m² · ` : "";
    fus = `3D region from ${f.n_used} outline(s) in ${f.n_groups} viewpoint(s): ${area}` +
      `uncertain boundary ${pct(f.uncertain_area_frac)} of the area · ` +
      `seen from fewer than 2 viewpoints ${pct(f.weak_support_frac)}` +
      (f.components > 1 ? ` · <span class="err">${f.components} separate patches</span>` : "");
  }
  if (mv().resultRev != null && mv().resultRev !== mv().rev) {
    fus += `<div class="warn">outlines changed since the result below was computed — measure again</div>`;
  }
  $("mv-fusion").innerHTML = fus;

  const names = Object.keys(mv().annotations).sort();
  $("mv-list").innerHTML = names.map((n) => {
    const a = mv().annotations[n];
    const g = sm.views[n] ? `P${sm.views[n].group}` : "not reconstructed";
    const st = a.status === "accepted" ? '<span class="ok">accepted</span>'
      : a.status === "skipped" ? '<span class="note">skipped</span>'
      : '<span class="warn">draft — not accepted yet</span>';
    return `<div><button class="link" data-mv-image="${esc(n)}">${esc(n)}</button> · ${g} · ${st}` +
      (pending.has(n) ? ' · <span class="warn">unsaved</span>' : "") +
      (a.status === "accepted" ? agreementText(n) : "") +
      (n === cur ? " ◀" : "") + `</div>`;
  }).join("") || '<span class="note">no outlines yet — start with the suggested photo</span>';

  const sug = sm.suggestions.find((x) => x.image !== cur);
  $("mv-next").disabled = !sug;
  $("mv-next").textContent = sug ? `Next suggested photo → P${sug.group}` : "No more suggestions";
}

export function initMultiview(h) {
  hooks = h;
  resetMultiview();
  $("mv-accept").addEventListener("click", accept);
  $("mv-skip").addEventListener("click", skip);
  $("mv-delete").addEventListener("click", remove);
  $("mv-suggest").addEventListener("click", useSuggestion);
  $("mv-next").addEventListener("click", next);
  $("mv-indicative").addEventListener("change", () => hooks.refreshMeasure());
  $("mv-list").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-mv-image]");
    if (b) hooks.openImage(b.dataset.mvImage);
  });
  $("mv-progress").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-mv-group]");
    if (!b || !mv().summary) return;
    const gid = Number(b.dataset.mvGroup);
    const g = mv().summary.groups.find((x) => x.id === gid);
    const sug = mv().summary.suggestions.find((x) => x.group === gid);
    const pick = (sug && sug.image) || (g && g.images[0]);
    if (pick) hooks.openImage(pick);
  });
  // an unsaved outline must not be lost to a tab close
  window.addEventListener("beforeunload", (ev) => {
    if (pending.size) { ev.preventDefault(); ev.returnValue = ""; }
  });
}

/** Enter the multi-view step (job ready, or the mode was switched to it):
 * load the outlines and open `image`, else the first suggested photo. */
export async function enter(image) {
  if (!(await refresh())) return;
  const sug = mv().summary.suggestions[0];
  const known = mv().summary.views;
  const pick = image && known[image] ? image : sug ? sug.image : state.markImg;
  hooks.openImage(pick);
}

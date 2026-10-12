/* Step 1: upload photos, poll job status, and switch/resume jobs. Also the
 * cross-step "job ready" orchestration (calls each step module's
 * onJobReady once a snapshot with images/scale/ortho comes back). */
import { state, $, LS_KEY } from "../state.js";
import { api } from "../api.js";
import * as scale from "./scale.js";
import * as mark from "./mark.js";
import { showResult } from "./result.js";
import * as cm from "../capture_metrics.js";

let pollTimer = null;

export function poll(onDone) {
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
    const busy = ["reconstructing", "measuring", "orthorectifying"].includes(snap.status);
    // ready but no images yet = ctx still reloading (server restart, or the
    // background reload kicked off for a `ctx_loading` snapshot — see
    // server/jobs.py's ensure_ctx / T3.1's S4 fix)
    const loading = snap.status === "ready" && (snap.ctx_loading || !(snap.images || []).length);
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

export async function upload(files) {
  if (files.length < 3) {
    $("progress").classList.remove("hidden");
    $("progress-text").innerHTML = '<span class="err">select at least 3 photos (15–60 recommended)</span>';
    return;
  }
  if (files.length > 200) {
    $("progress").classList.remove("hidden");
    $("progress-text").innerHTML = '<span class="err">too many photos (max 200)</span>';
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

function switchJob(id, { keepProgress = false } = {}) {
  clearTimeout(pollTimer);
  state.jobId = id;
  localStorage.setItem(LS_KEY, id);
  state.polygon = []; state.polygonClosed = false; state.selectedVertex = -1;
  state.manual = { a: { img: null, pts: [] }, b: { img: null, pts: [] } };
  state.scale = null; state.images = []; state.ortho = null;
  state.lastResult = null;
  state.traceMode = "multi";
  $("step-result").classList.add("hidden");
  $("step-scale").classList.add("hidden");
  $("step-mark").classList.add("hidden");
  $("poly-close").disabled = true;
  $("measure-btn").disabled = true;
  if (!keepProgress) $("progress").classList.add("hidden");
}

function onReady(snap) {
  state.images = snap.images || [];
  $("step-scale").classList.remove("hidden");
  $("step-mark").classList.remove("hidden");
  scale.onJobReady(snap);
  mark.onJobReady(snap);
  if (snap.result) showResult(snap.result);
  refreshJobList();
}

// Also used by capture.js (S3) to hand a freshly-finalized capture-session
// job to the normal ready/reconstructing poll flow (GUIDED_CAPTURE_PLAN.md
// §9.4), instead of duplicating this logic.
export async function resumeJob(id) {
  switchJob(id);
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "reconnecting…";
  poll((snap) => {
    if (snap.status === "error") {
      $("progress-text").innerHTML = `<span class="err">job failed: ${snap.error}</span>`;
      $("step-scale").classList.remove("hidden");
      return;
    }
    $("progress").classList.add("hidden");
    onReady(snap);
  });
}

export async function refreshJobList() {
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

// GUIDED_CAPTURE_PLAN.md S1: instant (T0) blur/exposure/duplicate/lens/
// resolution/orientation checks, run client-side on the already-selected
// files before upload. Read-only: never touches the File objects that get
// uploaded, so the originals (full resolution, EXIF intact) reach the
// server exactly as before.
const INSTR = {
  blur: "Blurry — hold still, tap to focus on the slope, retake.",
  exposure: "Sky or shadow is blowing out the frame — tilt down or step so the sun is behind you.",
  duplicate: "Same as the last photo — take 1–2 steps along the arc first.",
  lens: "Camera switched lens (0.5×/2×). Set it back to 1× and retake.",
  resOri: "Keep the phone in the same orientation for every photo, and use the full photo resolution.",
};

async function decodeForChecks(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const longSide = Math.max(bitmap.width, bitmap.height);
  const s = Math.min(1, cm.MAX_SIDE / longSide);
  const w = Math.max(1, Math.round(bitmap.width * s));
  const h = Math.max(1, Math.round(bitmap.height * s));
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0, w, h);
  const gray = cm.toGray(ctx.getImageData(0, 0, w, h).data);
  bitmap.close();
  const exif = cm.parseExif(await file.arrayBuffer());
  return { longSide, gray, w, h, exif };
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}

function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function chip(label, verdict, msg) {
  if (!verdict || verdict === "ok") return "";
  const cls = verdict === "fail" ? "err" : "warn";
  return ` <span class="${cls}" title="${msg || ""}">${label}: ${verdict}</span>`;
}

async function runInstantChecks(files) {
  const out = $("capture-checks");
  if (!files.length) { out.innerHTML = ""; return; }
  out.innerHTML = '<span class="note">checking photos…</span>';
  const decoded = [];
  for (const f of files) {
    try { decoded.push(await decodeForChecks(f)); }
    catch (e) { decoded.push(null); }
  }
  const laps = decoded.map((d) => d && cm.laplacianVariance(d.gray, d.w, d.h));
  const clips = decoded.map((d) => d && cm.clippedFraction(d.gray));
  const hashes = decoded.map((d) => d && cm.dHash(d.gray, d.w, d.h));
  const med = laps.filter((v) => v != null).length >= 5 ? median(laps.filter((v) => v != null)) : null;
  const ref = decoded.find((d) => d)?.exif || null;
  const refOrientation = ref?.orientation ?? null;

  let prevKept = -1, exifWarned = false;
  const rows = [];
  for (let i = 0; i < files.length; i++) {
    const d = decoded[i];
    if (!d) { rows.push(`${esc(files[i].name)}: <span class="err">could not read photo</span>`); continue; }
    const blurV = cm.classifyBlur(laps[i], med);
    const expV = cm.classifyExposure(clips[i]);
    let dupV = "ok";
    if (blurV !== "fail" && expV !== "fail") {
      if (prevKept === -1) {
        dupV = "ok";
      } else {
        const ham = cm.hamming(hashes[i], hashes[prevKept]);
        dupV = cm.classifyDuplicate(ham);
      }
      if (dupV === "fail" && prevKept !== -1 && laps[prevKept] > laps[i]) {
        // near-duplicate: the blurrier of the two would be dropped server-side
      } else {
        prevKept = i;
      }
    }
    // §4.1: a mismatch is shown every time; missing EXIF is shown once per
    // batch (the same camera model produces it on every photo otherwise).
    let lensV = "ok", lensMsg = INSTR.lens;
    if (i > 0) {
      const raw = cm.classifyLens(d.exif, ref);
      if (raw === "fail") lensV = "fail";
      else if (raw === "missing") {
        if (exifWarned) lensV = "ok";
        else { lensV = "warn"; lensMsg = "no EXIF metadata found — camera/lens can't be checked"; exifWarned = true; }
      }
    }
    const resV = cm.classifyResolution(d.longSide);
    const oriV = cm.classifyOrientation(d.exif?.orientation ?? null, refOrientation);
    const resOriV = resV === "warn" || oriV === "warn" ? "warn" : "ok";

    const prefix = `${esc(files[i].name)}:`;
    let row = prefix;
    row += chip("blur", blurV, INSTR.blur);
    row += chip("exposure", expV, INSTR.exposure);
    row += chip("duplicate", dupV, INSTR.duplicate);
    row += chip("lens", lensV, lensMsg);
    row += chip("photo", resOriV, INSTR.resOri);
    if (row === prefix) row += ' <span class="ok">ok</span>';
    rows.push(row);
  }
  out.innerHTML = rows.map((r) => `<div>${r}</div>`).join("");
}

export function initUpload() {
  $("file-input").addEventListener("change", (e) => {
    const n = e.target.files.length;
    $("upload-btn").disabled = n < 3;
    $("upload-note").textContent = n === 0
      ? "select photos in capture order (left → right)"
      : `${n} photo${n === 1 ? "" : "s"} selected` + (n < 3 ? " — need at least 3" : "");
    runInstantChecks([...e.target.files]);
  });
  $("upload-btn").addEventListener("click", () => upload($("file-input").files));

  $("job-list").addEventListener("click", async (ev) => {
    const del = ev.target.closest("[data-del]");
    if (del) {
      if (!confirm("delete this job and its photos?")) return;
      try {
        await api(`/api/jobs/${del.dataset.del}`, { method: "DELETE" });
      } catch (e) {
        alert("could not delete job: " + e.message);   // P3a: busy jobs 409
        return;
      }
      if (del.dataset.del === state.jobId) window.location.reload();
      refreshJobList();
      return;
    }
    const chip = ev.target.closest("[data-job]");
    if (chip && chip.dataset.job !== state.jobId) resumeJob(chip.dataset.job);
  });

  refreshJobList();
}

/** Resume the last session's job (localStorage) after a page refresh. */
export async function resumeLastJob() {
  const saved = localStorage.getItem(LS_KEY);
  if (!saved) return;
  try {
    await api(`/api/jobs/${saved}`);
    resumeJob(saved);
  } catch (e) {
    localStorage.removeItem(LS_KEY);
  }
}

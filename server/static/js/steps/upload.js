/* Step 1: upload photos, poll job status, and switch/resume jobs. Also the
 * cross-step "job ready" orchestration (calls each step module's
 * onJobReady once a snapshot with images/scale/ortho comes back). */
import { state, $, LS_KEY } from "../state.js";
import { api } from "../api.js";
import * as scale from "./scale.js";
import * as mark from "./mark.js";
import { showResult } from "./result.js";

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

export function initUpload() {
  $("file-input").addEventListener("change", (e) => {
    const n = e.target.files.length;
    $("upload-btn").disabled = n < 3;
    $("upload-note").textContent = n === 0
      ? "select photos in capture order (left → right)"
      : `${n} photo${n === 1 ? "" : "s"} selected` + (n < 3 ? " — need at least 3" : "");
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

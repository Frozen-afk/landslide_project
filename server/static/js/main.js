/* Entry point — wires the four workflow-step modules together. Loaded as
 * `<script type="module">` (no bundler, native browser ESM). */
import { $ } from "./state.js";
import { setupCanvas } from "./canvas.js";
import { initUpload, resumeLastJob } from "./steps/upload.js";
import { initScale } from "./steps/scale.js";
import { initMark } from "./steps/mark.js";
import { initResult } from "./steps/result.js";

window.addEventListener("DOMContentLoaded", async () => {
  const markCanvas = setupCanvas($("mark-canvas"));
  const manA = setupCanvas($("man-canvas-a"));
  const manB = setupCanvas($("man-canvas-b"));

  initScale(manA, manB);
  initMark(markCanvas);
  initResult();
  initUpload();

  await resumeLastJob();
});

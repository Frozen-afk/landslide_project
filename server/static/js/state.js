/* Shared app state — module-scoped instead of a global (F6). Same shape as
 * the old monolithic app.js's `state` object; imported by every step module
 * that needs to read or mutate it. */
export const LS_KEY = "lsv-last-job";
// GUIDED_CAPTURE_PLAN.md §9.4: separate from LS_KEY, since a capture session
// in progress is not yet the "last job" an upload-flow resume should jump to.
export const LS_CAPTURE_KEY = "lsv-capture";

export const state = {
  jobId: null,
  images: [],          // [{name, width, height, points}]
  scale: null,         // scale info from server
  ortho: null,         // orthophoto metadata (when rendered)
  manual: { a: { img: null, pts: [] }, b: { img: null, pts: [] } },
  markImg: null,
  traceMode: "multi",  // "multi" (outlines on several photos) | "photo" (legacy single) | "ortho"
  polygon: [],         // original-image px (or ortho px)
  polygonClosed: false,
  selectedVertex: -1,  // index into `polygon`, -1 = none selected
  lastResult: null,
  // multi-view outlines, mirrored from the server (steps/multiview.js)
  mv: { annotations: {}, summary: null, rev: 0, projected: null, resultRev: null },
  // S3 guided-capture session (GUIDED_CAPTURE_PLAN.md §9.4). `photos` mirrors
  // the server manifest (source of truth, §10) — not a separate client copy.
  capture: { jobId: null, arm: "A", seq: 0, photos: [] },
};

export const $ = (id) => document.getElementById(id);

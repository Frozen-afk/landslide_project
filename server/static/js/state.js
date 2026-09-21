/* Shared app state — module-scoped instead of a global (F6). Same shape as
 * the old monolithic app.js's `state` object; imported by every step module
 * that needs to read or mutate it. */
export const LS_KEY = "lsv-last-job";

export const state = {
  jobId: null,
  images: [],          // [{name, width, height, points}]
  scale: null,         // scale info from server
  ortho: null,         // orthophoto metadata (when rendered)
  manual: { a: { img: null, pts: [] }, b: { img: null, pts: [] } },
  markImg: null,
  traceMode: "photo",  // "photo" | "ortho"
  polygon: [],         // original-image px (or ortho px)
  polygonClosed: false,
  selectedVertex: -1,  // index into `polygon`, -1 = none selected
  lastResult: null,
};

export const $ = (id) => document.getElementById(id);

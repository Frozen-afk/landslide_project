/* Fetch wrappers to the backend routes (server/routes.py). */
import { state } from "./state.js";

export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    let detail = null;
    try {
      detail = (await r.json()).detail;
      // S5C (GUIDED_CAPTURE_PLAN.md §12c.9): a structured 409 detail (e.g.
      // finalize's `{blocking, stale_preview}`) used to stringify to
      // "[object Object]" here. Keep the object on the thrown error instead,
      // and fall back to statusText for the plain string it used to be.
      if (typeof detail === "string" && detail) msg = detail;
    } catch (e) { /* not JSON */ }
    const err = new Error(msg);
    err.status = r.status;
    if (detail && typeof detail === "object") err.detail = detail;
    throw err;
  }
  return r.json();
}

export const photoURL = (name, w) =>
  `/api/jobs/${state.jobId}/photo/${name}?w=${w || 1400}`;

export const artifactURL = (name, bust = false) =>
  `/api/jobs/${state.jobId}/artifact/${name}${bust ? `?t=${Date.now()}` : ""}`;

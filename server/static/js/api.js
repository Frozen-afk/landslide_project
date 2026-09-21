/* Fetch wrappers to the backend routes (server/routes.py). */
import { state } from "./state.js";

export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

export const photoURL = (name, w) =>
  `/api/jobs/${state.jobId}/photo/${name}?w=${w || 1400}`;

export const artifactURL = (name, bust = false) =>
  `/api/jobs/${state.jobId}/artifact/${name}${bust ? `?t=${Date.now()}` : ""}`;

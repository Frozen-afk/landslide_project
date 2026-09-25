/* IndexedDB pending-photo queue for the guided-capture session
 * (GUIDED_CAPTURE_PLAN.md §9.4 "capture_store.js", §10 persistence). Holds
 * photo blobs the server has not yet acked, so a killed tab or a dropped
 * network connection can retry the exact same photo (same uuid) instead of
 * losing it. The server manifest (`GET .../capture`) stays the authoritative
 * record (§10's own stated principle) — this is only the client-side buffer
 * for what it doesn't have yet.
 *
 * No DOM here, but it does need `indexedDB`, so unlike capture_metrics.js /
 * capture_guidance.js this file is not `node --test`-able in this repo (no
 * fake-indexeddb dependency installed — the same "no headless browser in
 * this environment" gap S1/S2/S3 already documented). Every function
 * degrades to a safe no-op (`put` returns false, reads return []) when
 * IndexedDB throws or is unavailable (private browsing, quota, an old
 * browser), so a photo still uploads directly — it just has no retry buffer
 * if that direct attempt fails, the same as before this file existed.
 */

const DB_NAME = "lsv-capture-store";
const DB_VERSION = 1;
const STORE = "pending";

let dbPromise = null;

function openDB() {
  if (!window.indexedDB) return Promise.reject(new Error("IndexedDB unavailable"));
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const os = db.createObjectStore(STORE, { keyPath: "uuid" });
        os.createIndex("jobId", "jobId", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB open failed"));
  });
  return dbPromise;
}

/** Store a photo that has not been acked by the server yet. Returns true if
 * persisted, false if IndexedDB isn't usable right now — the caller then has
 * no retry buffer for this photo and must fall back to asking the user to
 * retake it on a failed upload, the pre-existing behavior. */
export async function put(jobId, uuid, blob, meta) {
  try {
    const db = await openDB();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put({ uuid, jobId, blob, meta, addedAt: Date.now() });
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    return true;
  } catch (e) { return false; }
}

/** All pending entries for a job, oldest first. `[]` (not a throw) when
 * IndexedDB is unavailable or empty — callers never need a try/catch. */
export async function listPending(jobId) {
  try {
    const db = await openDB();
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const idx = tx.objectStore(STORE).index("jobId");
      const out = [];
      const req = idx.openCursor(IDBKeyRange.only(jobId));
      req.onsuccess = () => {
        const cur = req.result;
        if (cur) { out.push(cur.value); cur.continue(); }
        else resolve(out.sort((a, b) => a.addedAt - b.addedAt));
      };
      req.onerror = () => reject(req.error);
    });
  } catch (e) { return []; }
}

/** Remove one acked entry. Best-effort: a failure here just leaves a stale
 * queue row that the next `listPending` reconciliation (matched against the
 * server manifest) will ack again. */
export async function ack(uuid) {
  try {
    const db = await openDB();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(uuid);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } catch (e) { /* best-effort */ }
}

/** Drop every pending entry for a job (called once it's finalized — nothing
 * still queued can ever be uploaded to it again, so don't let it linger). */
export async function clear(jobId) {
  for (const item of await listPending(jobId)) await ack(item.uuid);
}

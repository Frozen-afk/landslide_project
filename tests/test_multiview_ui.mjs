/* steps/multiview.js against a stub DOM + stub fetch (no jsdom in this
 * repo): the save queue — retry after a dropped connection, no duplicate
 * PUT, nothing lost — and the 4x2 measure gate. Run: node --test tests/ */
import test from "node:test";
import assert from "node:assert/strict";

const els = new Map();
const el = (id) => {
  if (!els.has(id)) {
    els.set(id, { id, innerHTML: "", textContent: "", disabled: false, checked: false,
                  listeners: {}, addEventListener(t, f) { this.listeners[t] = f; } });
  }
  return els.get(id);
};
globalThis.document = { getElementById: el };
globalThis.window = { addEventListener() {} };

const calls = [];
let failPut = false;
let server = { annotations: {}, rev: 0 };
const summary = () => {
  const acc = Object.values(server.annotations).filter((a) => a.status === "accepted").length;
  return {
    groups: [{ id: 1, azimuth_deg: 0, elevation_deg: 20, n_images: 2, images: ["a.jpg", "b.jpg"], n_accepted: acc }],
    views: { "a.jpg": { group: 1 }, "b.jpg": { group: 1 } },
    requirement: { met: acc >= 2, n_groups_complete: acc >= 2 ? 4 : 0, min_groups: 4,
                   min_per_group: 2, n_annotations: acc, min_annotations: 8 },
    suggestions: ["a.jpg", "b.jpg"].filter((n) => !server.annotations[n]).map((image) => ({ image, group: 1 })),
    agreement: {}, fusion: null, instructions: ["next: outline <b>"],
  };
};
globalThis.fetch = async (url, opts = {}) => {
  const method = opts.method || "GET";
  calls.push(`${method} ${url}`);
  const json = (body, status = 200) => ({ ok: status < 400, status, statusText: "x", json: async () => body });
  const image = decodeURIComponent(url.split("/annotations/")[1] || "");
  if (method === "PUT") {
    if (failPut) throw new TypeError("network down");
    server.annotations[image] = JSON.parse(opts.body);
    server.rev += 1;
    return json({ image, annotation: server.annotations[image], rev: server.rev });
  }
  if (method === "DELETE") { delete server.annotations[image]; server.rev += 1; return json({ rev: server.rev }); }
  if (image) return json({ annotation: server.annotations[image] || null, projected: { lower: null, best: [[0, 0], [9, 0], [9, 9]], upper: null } });
  return json({ annotations: structuredClone(server.annotations), rev: server.rev, summary: summary() });
};

const { state } = await import("../server/static/js/state.js");
const mv = await import("../server/static/js/steps/multiview.js");

const opened = [];
mv.initMultiview({
  openImage: (n) => { opened.push(n); state.markImg = n; mv.onImageOpened(n); },
  redraw() {}, refreshMeasure() {},
});
state.jobId = "job1";
const square = [{ x: 0, y: 0 }, { x: 5, y: 0 }, { x: 5, y: 5 }];
const tick = () => new Promise((r) => setTimeout(r, 0));

test("enter opens the first suggested photo and escapes server text", async () => {
  await mv.enter(null);
  assert.deepEqual(opened, ["a.jpg"]);
  assert.match(el("mv-instructions").innerHTML, /&lt;b&gt;/);
});

test("an edit is saved once; a failed save is kept and retried", async () => {
  state.polygon = square; state.polygonClosed = true;
  failPut = true;
  mv.polygonEdited();
  assert.equal(await mv.flush(), false, "save failed: must report unsaved");
  assert.equal(state.mv.annotations["a.jpg"].status, "draft", "the edit is still held locally");
  assert.deepEqual(server.annotations, {});
  failPut = false;
  const before = calls.filter((c) => c.startsWith("PUT")).length;
  assert.equal(await mv.flush(), true);
  assert.equal(await mv.flush(), true, "nothing pending: no second PUT");
  assert.equal(calls.filter((c) => c.startsWith("PUT")).length, before + 1);
  assert.equal(server.annotations["a.jpg"].status, "draft");
});

test("measure gate: 4x2, or an explicit indicative request", async () => {
  assert.equal(mv.canMeasure(), false, "a draft does not count");
  el("mv-accept").listeners.click();
  for (let i = 0; i < 20 && server.annotations["a.jpg"].status !== "accepted"; i++) await tick();
  await tick(); await tick();
  assert.equal(server.annotations["a.jpg"].status, "accepted");
  assert.equal(mv.canMeasure(), false, "one outline: no final measurement");
  el("mv-indicative").checked = true;
  assert.equal(mv.canMeasure(), true);
  assert.equal(mv.wantsIndicative(), true);
  el("mv-indicative").checked = false;
  server.annotations["b.jpg"] = { polygon: [[0, 0], [1, 0], [1, 1]], status: "accepted", source: "manual" };
  await mv.refresh();
  assert.equal(mv.canMeasure(), true);
  assert.equal(mv.wantsIndicative(), false);
  assert.deepEqual(mv.polygonFor("b.jpg"), [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }]);
});

test("a result measured before the outlines changed is marked stale", async () => {
  mv.setResultRev({ multiview: { annotations_rev: server.rev - 1 } }, server.rev);
  assert.match(el("mv-fusion").innerHTML, /measure again/);
  mv.setResultRev({ multiview: { annotations_rev: server.rev } }, server.rev);
  assert.doesNotMatch(el("mv-fusion").innerHTML, /measure again/);
});

// Coordinate-chain regression test (T3.2 / F2, F6). Zero new dependencies —
// pure functions, no DOM/jsdom needed. Run: node --test tests/test_coords.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  storedToDraw, drawToStored, drawToCanvas, canvasToDraw,
  storedToCanvas, canvasToStored, zoomAt, panBy, resetZoomPan,
  closestOnSegment,
} from "../server/static/js/coords.js";

function near(a, b, eps = 1e-9) {
  assert.ok(Math.abs(a - b) < eps, `${a} !~ ${b}`);
}

test("storedToDraw / drawToStored round-trip", () => {
  const view = { scale: 0.5, k: 2.13 };
  const p = { x: 731.4, y: 222.9 };
  const d = storedToDraw(p, view);
  const back = drawToStored(d, view);
  near(back.x, p.x); near(back.y, p.y);
});

test("drawToCanvas / canvasToDraw round-trip under zoom+pan", () => {
  const zp = { zoom: 2.7, panX: -40, panY: 15 };
  const p = { x: 100, y: 50 };
  const c = drawToCanvas(p, zp);
  const back = canvasToDraw(c, zp);
  near(back.x, p.x); near(back.y, p.y);
});

test("identity zoomPan is a no-op", () => {
  const zp = resetZoomPan();
  const p = { x: 12.3, y: 45.6 };
  const c = drawToCanvas(p, zp);
  near(c.x, p.x); near(c.y, p.y);
});

test("full chain: stored px -> canvas px -> stored px, multiple views", () => {
  const cases = [
    { view: { scale: 1, k: 1 }, zp: resetZoomPan() },
    { view: { scale: 0.32, k: 2.5 }, zp: { zoom: 3, panX: 120, panY: -60 } },
    { view: { scale: 1, k: 1 }, zp: { zoom: 0.4, panX: 0, panY: 0 } },
  ];
  const pts = [{ x: 0, y: 0 }, { x: 1234.5, y: 987.6 }, { x: 42, y: 800 }];
  for (const { view, zp } of cases) {
    for (const p of pts) {
      const c = storedToCanvas(p, view, zp);
      const back = canvasToStored(c, view, zp);
      near(back.x, p.x, 1e-6); near(back.y, p.y, 1e-6);
    }
  }
});

test("zoomAt keeps the focal point stationary on screen", () => {
  let zp = resetZoomPan();
  const focal = { x: 300, y: 150 };
  zp = zoomAt(zp, 2.0, focal);
  // the canvas point that maps to `focal` in draw-space before the zoom
  // must still map to `focal` after it
  const drawPtBefore = canvasToDraw(focal, { zoom: 1, panX: 0, panY: 0 });
  const canvasPtAfter = drawToCanvas(drawPtBefore, zp);
  near(canvasPtAfter.x, focal.x, 1e-9);
  near(canvasPtAfter.y, focal.y, 1e-9);
});

test("zoomAt clamps to [minZoom, maxZoom]", () => {
  let zp = resetZoomPan();
  zp = zoomAt(zp, 0.01, { x: 0, y: 0 }, 1, 12);
  assert.equal(zp.zoom, 1);
  zp = zoomAt(zp, 1000, { x: 0, y: 0 }, 1, 12);
  assert.equal(zp.zoom, 12);
});

test("panBy is a pure translation of panX/panY", () => {
  const zp = { zoom: 2, panX: 10, panY: -5 };
  const zp2 = panBy(zp, 7, 3);
  assert.equal(zp2.zoom, 2);
  near(zp2.panX, 17); near(zp2.panY, -2);
  // original untouched
  assert.equal(zp.panX, 10);
});

test("closestOnSegment clamps to the segment endpoints", () => {
  const a = { x: 0, y: 0 }, b = { x: 10, y: 0 };
  const mid = closestOnSegment({ x: 5, y: 3 }, a, b);
  near(mid.x, 5); near(mid.y, 0); near(mid.t, 0.5);
  const before = closestOnSegment({ x: -5, y: 3 }, a, b);
  near(before.x, 0); near(before.t, 0);
  const after = closestOnSegment({ x: 15, y: 3 }, a, b);
  near(after.x, 10); near(after.t, 1);
});

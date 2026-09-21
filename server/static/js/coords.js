/* Pure coordinate math for the marking canvases: image px <-> canvas
 * backing-store px, through a zoom/pan view transform. No DOM references
 * anywhere in this file, so it can be unit-tested directly with
 * `node --test` (see tests/test_coords.mjs) without jsdom or a browser. */

/** stored-image px -> "draw px" (the pre-zoom canvas-backing-store
 * convention app.js always used: draw px = stored px * scale / k, where
 * `scale` is the display-vs-natural-image ratio and `k` is the
 * stored-vs-served-photo ratio). */
export function storedToDraw(p, view) {
  const s = view.scale / (view.k || 1);
  return { x: p.x * s, y: p.y * s };
}

export function drawToStored(p, view) {
  const s = view.scale / (view.k || 1);
  return { x: p.x / s, y: p.y / s };
}

/** draw px -> canvas backing-store px, applying the zoom/pan transform
 * (zoom about the draw-px origin, then pan by a backing-store-px offset —
 * this matches `ctx.setTransform(zoom, 0, 0, zoom, panX, panY)`). */
export function drawToCanvas(p, zoomPan) {
  return { x: p.x * zoomPan.zoom + zoomPan.panX, y: p.y * zoomPan.zoom + zoomPan.panY };
}

export function canvasToDraw(p, zoomPan) {
  return { x: (p.x - zoomPan.panX) / zoomPan.zoom, y: (p.y - zoomPan.panY) / zoomPan.zoom };
}

/** Compose both stages: stored-image px <-> canvas backing-store px. */
export function storedToCanvas(p, view, zoomPan) {
  return drawToCanvas(storedToDraw(p, view), zoomPan);
}

export function canvasToStored(p, view, zoomPan) {
  return drawToStored(canvasToDraw(p, zoomPan), view);
}

/** Zoom by `factor` (>1 = in, <1 = out) keeping the point `focal` (canvas
 * backing-store px) stationary on screen. Returns a new zoomPan, clamped to
 * [minZoom, maxZoom]. */
export function zoomAt(zoomPan, factor, focal, minZoom = 1, maxZoom = 12) {
  const newZoom = Math.min(maxZoom, Math.max(minZoom, zoomPan.zoom * factor));
  const applied = newZoom / zoomPan.zoom;
  return {
    zoom: newZoom,
    panX: focal.x - (focal.x - zoomPan.panX) * applied,
    panY: focal.y - (focal.y - zoomPan.panY) * applied,
  };
}

/** Pan by a canvas-backing-store-px delta. */
export function panBy(zoomPan, dx, dy) {
  return { zoom: zoomPan.zoom, panX: zoomPan.panX + dx, panY: zoomPan.panY + dy };
}

export function resetZoomPan() {
  return { zoom: 1, panX: 0, panY: 0 };
}

export function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

/** Closest point to `p` on segment a->b, clamped to the segment. */
export function closestOnSegment(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  if (len2 < 1e-9) return { x: a.x, y: a.y, t: 0 };
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2;
  t = Math.min(1, Math.max(0, t));
  return { x: a.x + t * dx, y: a.y + t * dy, t };
}

/* Marking-canvas setup: image display, zoom (wheel + pinch), pan
 * (click-drag on empty canvas), and a generic tap/vertex-drag gesture
 * engine (F2). Coordinate math lives in coords.js (kept DOM-free there so
 * it's unit-testable); this file is the DOM/event-handling half.
 *
 * Per-canvas hooks a caller can set to opt into polygon editing:
 *   canvas.hitTest(canvasPx)   -> {kind:'vertex', index} | {kind:'edge', index, point} | null
 *   canvas.onDragMove(hit, storedPx, isFirst)  -> called while dragging a hit
 *   canvas.onDragEnd(hit, moved)               -> called when the drag ends
 *   canvas.onTap(storedPx, ev)                 -> called on a plain tap (no hit, no pan)
 *   canvas.gestureEnabled()    -> false suppresses all of the above (e.g. freehand mode) */
import { state } from "./state.js";
import { photoURL } from "./api.js";
import { canvasToStored, zoomAt, panBy, resetZoomPan, dist } from "./coords.js";

const TAP_THRESHOLD = 4; // canvas backing-store px

export function setupCanvas(canvas) {
  const ctx = canvas.getContext("2d");
  const view = { img: null, name: null, scale: 1, k: 1, zoomPan: resetZoomPan() };
  canvas.view = view;
  canvas.gestureEnabled = () => true;
  canvas.hitTest = null;
  canvas.onDragMove = null;
  canvas.onDragEnd = null;
  canvas.onTap = null;

  canvas.draw = function (decorator) {
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.restore();
    if (!view.img) return;
    const { zoom, panX, panY } = view.zoomPan;
    ctx.save();
    ctx.setTransform(zoom, 0, 0, zoom, panX, panY);
    ctx.drawImage(view.img, 0, 0, canvas.width, canvas.height);
    if (decorator) decorator(ctx, view.scale / (view.k || 1));
    ctx.restore();
  };

  canvas.loadURL = function (url, displayW, k = 1) {
    view.name = url;
    view.img = null;
    view.k = k;
    view.zoomPan = resetZoomPan();
    const img = new Image();
    img.onload = () => {
      view.img = img;
      view.scale = Math.min(1, displayW / img.naturalWidth);
      // canvas backing-store size is set once per image load, not per draw
      // (T0.7/F5: a full width/height reassignment reallocates the buffer).
      canvas.width = img.naturalWidth * view.scale;
      canvas.height = img.naturalHeight * view.scale;
      canvas.draw(canvas.decorator);
    };
    img.src = url;
  };

  canvas.load = function (name, displayW) {
    const meta = (state.images || []).find((i) => i.name === name);
    const k = meta ? meta.width / Math.min(1400, meta.width) : 1;
    canvas.loadURL(photoURL(name), displayW, k);
  };

  canvas.resetView = function () {
    view.zoomPan = resetZoomPan();
    canvas.draw(canvas.decorator);
  };

  canvas.toCanvasPx = (ev) => {
    const r = canvas.getBoundingClientRect();
    return {
      x: (ev.clientX - r.left) * canvas.width / r.width,
      y: (ev.clientY - r.top) * canvas.height / r.height,
    };
  };
  canvas.toOriginal = (ev) => canvasToStored(canvas.toCanvasPx(ev), view, view.zoomPan);

  /* ---- zoom (wheel/pinch) + pan + tap/vertex-drag gesture engine ---- */
  const pointers = new Map();   // pointerId -> last canvas px (pinch tracking)
  let pinch = null;             // {dist, mid}
  let drag = null;              // {kind:'pan'|'hit', ...}

  canvas.addEventListener("wheel", (ev) => {
    if (!canvas.gestureEnabled() || !view.img) return;
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
    view.zoomPan = zoomAt(view.zoomPan, factor, canvas.toCanvasPx(ev));
    canvas.draw(canvas.decorator);
  }, { passive: false });

  canvas.addEventListener("pointerdown", (ev) => {
    if (!canvas.gestureEnabled() || !view.img) return;
    canvas.setPointerCapture(ev.pointerId);
    pointers.set(ev.pointerId, canvas.toCanvasPx(ev));
    if (pointers.size === 2) {
      drag = null;
      const [a, b] = [...pointers.values()];
      pinch = { dist: dist(a, b), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } };
      return;
    }
    if (pointers.size > 2) return;
    const cpx = canvas.toCanvasPx(ev);
    const hit = canvas.hitTest ? canvas.hitTest(cpx) : null;
    if (hit) {
      drag = { kind: "hit", hit, moved: false };
      canvas.onDragMove && canvas.onDragMove(hit, canvas.toOriginal(ev), true);
      canvas.draw(canvas.decorator);
    } else {
      drag = { kind: "pan", start: cpx, startClient: cpx, moved: false };
    }
  });

  canvas.addEventListener("pointermove", (ev) => {
    if (!canvas.gestureEnabled() || !view.img) return;
    if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, canvas.toCanvasPx(ev));
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = dist(a, b);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      view.zoomPan = zoomAt(view.zoomPan, d / pinch.dist, mid);
      view.zoomPan = panBy(view.zoomPan, mid.x - pinch.mid.x, mid.y - pinch.mid.y);
      pinch = { dist: d, mid };
      canvas.draw(canvas.decorator);
      return;
    }
    if (!drag) return;
    const cpx = canvas.toCanvasPx(ev);
    if (drag.kind === "pan") {
      if (!drag.moved && dist(cpx, drag.startClient) <= TAP_THRESHOLD) return;
      drag.moved = true;
      view.zoomPan = panBy(view.zoomPan, cpx.x - drag.start.x, cpx.y - drag.start.y);
      drag.start = cpx;
      canvas.draw(canvas.decorator);
    } else if (drag.kind === "hit") {
      drag.moved = true;
      canvas.onDragMove && canvas.onDragMove(drag.hit, canvas.toOriginal(ev), false);
      canvas.draw(canvas.decorator);
    }
  });

  const endPointer = (ev) => {
    pointers.delete(ev.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 0 && drag) {
      if (drag.kind === "hit") {
        canvas.onDragEnd && canvas.onDragEnd(drag.hit, drag.moved);
      } else if (drag.kind === "pan" && !drag.moved) {
        canvas.onTap && canvas.onTap(canvas.toOriginal(ev), ev);
      }
      drag = null;
      canvas.draw(canvas.decorator);
    }
  };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);

  return canvas;
}

/* Pure T0 instant-photo-check math (GUIDED_CAPTURE_PLAN.md S1, §4.1, §9.4).
 * No DOM anywhere in this file, so it is unit-testable directly with
 * `node --test` (see tests/test_capture_metrics.mjs), same pattern as
 * coords.js / tests/test_coords.mjs. Mirrors landslide/pipeline.py's
 * `_photo_metrics` (blur/exposure/duplicate) and the §4.1 threshold table.
 * Marker, feature and overlap checks are T1 (server-side, S2+) and are not
 * implemented here. */

// landslide/pipeline.py::_photo_metrics default max_side
export const MAX_SIDE = 900;

// landslide/pipeline.py::_cull: blur_cut = max(6.0, 0.25*median). The 6.0
// floor is the absolute value used before a session median is meaningful
// (§4.1 "before 5 photos exist, use an absolute floor calibrated in S1") —
// it is exactly the server's own hardcoded floor, so a small batch and a
// large one are judged by the same number.
export const BLUR_ABS_FLOOR = 6.0;
export const BLUR_WARN_FACTOR = 0.5;
// landslide/pipeline.py::EXPOSURE_CLIP_FRAC (fail/drop threshold)
export const EXPOSURE_FAIL = 0.5;
export const EXPOSURE_WARN = 0.10;
// landslide/pipeline.py::_cull duplicate Hamming cap
export const DUP_FAIL_HAMMING = 3;
export const DUP_WARN_HAMMING = 6;
export const MIN_LONG_SIDE = 1600;

/** RGBA (Uint8ClampedArray, e.g. from canvas ImageData.data) -> luma
 * Float32Array, PIL's `convert("L")` coefficients (ITU-R 601-2). */
export function toGray(rgba) {
  const n = rgba.length / 4;
  const gray = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    gray[i] = 0.299 * rgba[o] + 0.587 * rgba[o + 1] + 0.114 * rgba[o + 2];
  }
  return gray;
}

/** cv2.Laplacian(arr, CV_64F).var() with the default 1-ksize kernel
 * [[0,1,0],[1,-4,1],[0,1,0]], on the interior (border pixels skipped —
 * a documented approximation, see tests/test_capture_metrics.mjs). */
export function laplacianVariance(gray, w, h) {
  let sum = 0, sum2 = 0, n = 0;
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const lap = 4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - w] - gray[i + w];
      sum += lap; sum2 += lap * lap; n++;
    }
  }
  if (n === 0) return 0;
  const mean = sum / n;
  return sum2 / n - mean * mean;
}

/** Fraction of pixels pinned to the histogram rails (<=2 or >=253). */
export function clippedFraction(gray) {
  let clipped = 0;
  for (let i = 0; i < gray.length; i++) {
    if (gray[i] <= 2 || gray[i] >= 253) clipped++;
  }
  return clipped / gray.length;
}

/** Bilinear resize (OpenCV pixel-center convention: src = (dst+0.5)*scale-0.5,
 * edge-clamped), approximating cv2.resize(arr, (dw, dh)) default INTER_LINEAR. */
export function resizeBilinear(gray, w, h, dw, dh) {
  const out = new Float32Array(dw * dh);
  const sx = w / dw, sy = h / dh;
  for (let dy = 0; dy < dh; dy++) {
    let fy = (dy + 0.5) * sy - 0.5;
    fy = Math.min(Math.max(fy, 0), h - 1);
    const y0 = Math.floor(fy), y1 = Math.min(y0 + 1, h - 1), wy = fy - y0;
    for (let dx = 0; dx < dw; dx++) {
      let fx = (dx + 0.5) * sx - 0.5;
      fx = Math.min(Math.max(fx, 0), w - 1);
      const x0 = Math.floor(fx), x1 = Math.min(x0 + 1, w - 1), wx = fx - x0;
      const top = gray[y0 * w + x0] * (1 - wx) + gray[y0 * w + x1] * wx;
      const bot = gray[y1 * w + x0] * (1 - wx) + gray[y1 * w + x1] * wx;
      out[dy * dw + dx] = top * (1 - wy) + bot * wy;
    }
  }
  return out;
}

/** 64-bit dHash: resize to 9x8, then small[:,1:] > small[:,:-1], row-major
 * (matches landslide/pipeline.py::_photo_metrics). Returns a Uint8Array of
 * 64 bits (0/1), row-major (bit index = row*8 + col). */
export function dHash(gray, w, h) {
  const small = resizeBilinear(gray, w, h, 9, 8);
  const bits = new Uint8Array(64);
  for (let row = 0; row < 8; row++) {
    for (let col = 0; col < 8; col++) {
      bits[row * 8 + col] = small[row * 9 + col + 1] > small[row * 9 + col] ? 1 : 0;
    }
  }
  return bits;
}

export function hamming(a, b) {
  let d = 0;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) d++;
  return d;
}

/** §4.1 blur verdict. `median` is the session/batch median Laplacian
 * variance, or null/undefined when fewer than 5 photos exist yet. */
export function classifyBlur(lap, median) {
  const cut = Math.max(BLUR_ABS_FLOOR, median ? 0.25 * median : 0);
  if (lap < cut) return "fail";
  if (median && lap < BLUR_WARN_FACTOR * median) return "warn";
  return "ok";
}

export function classifyExposure(clip) {
  if (clip >= EXPOSURE_FAIL) return "fail";
  if (clip >= EXPOSURE_WARN) return "warn";
  return "ok";
}

/** `ham` is the Hamming distance to the previous *kept* photo, or null for
 * the first kept photo of a batch (nothing to compare against). */
export function classifyDuplicate(ham) {
  if (ham === null || ham === undefined) return "ok";
  if (ham <= DUP_FAIL_HAMMING) return "fail";
  if (ham <= DUP_WARN_HAMMING) return "warn";
  return "ok";
}

export function classifyResolution(longSide) {
  return longSide >= MIN_LONG_SIDE ? "ok" : "warn";
}

export function classifyOrientation(orientation, refOrientation) {
  if (refOrientation == null || orientation == null) return "ok";
  return orientation === refOrientation ? "ok" : "warn";
}

// EXIF fields §4.1's lens/zoom check compares against photo 1.
const TAG_ORIENTATION = 0x0112;
const TAG_EXIF_IFD = 0x8769;
const TAG_FOCAL_LENGTH = 0x920a;
const TAG_FOCAL_LENGTH_35MM = 0xa405;
const TAG_LENS_MODEL = 0xa434;
const TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8 };

function readIfd(view, tiffStart, ifdOffset, little, wantTags) {
  const out = {};
  const count = view.getUint16(tiffStart + ifdOffset, little);
  for (let i = 0; i < count; i++) {
    const entry = tiffStart + ifdOffset + 2 + i * 12;
    const tag = view.getUint16(entry, little);
    if (!wantTags.has(tag)) continue;
    const type = view.getUint16(entry + 2, little);
    const num = view.getUint32(entry + 4, little);
    const size = (TYPE_SIZE[type] || 1) * num;
    const valueAt = size <= 4 ? entry + 8 : tiffStart + view.getUint32(entry + 8, little);
    if (type === 3) out[tag] = view.getUint16(valueAt, little);
    else if (type === 4) out[tag] = view.getUint32(valueAt, little);
    else if (type === 5) {
      const numer = view.getUint32(valueAt, little), denom = view.getUint32(valueAt + 4, little);
      out[tag] = denom ? numer / denom : 0;
    } else if (type === 2) {
      const bytes = new Uint8Array(view.buffer, valueAt, Math.max(0, size - 1));
      out[tag] = new TextDecoder("ascii").decode(bytes);
    }
  }
  return out;
}

/** Parse the FocalLength / FocalLengthIn35mmFilm / LensModel / Orientation
 * EXIF tags out of a JPEG ArrayBuffer. Returns null if there is no Exif
 * APP1 segment (§4.1: "Missing EXIF -> warn once per session"). */
export function parseExif(buffer) {
  const view = new DataView(buffer);
  if (view.getUint16(0) !== 0xffd8) return null;
  let offset = 2;
  while (offset + 4 <= view.byteLength) {
    const marker = view.getUint16(offset);
    if ((marker & 0xff00) !== 0xff00) break;
    if (marker === 0xffd8 || marker === 0xffd9) { offset += 2; continue; }
    const segLen = view.getUint16(offset + 2);
    if (marker === 0xffe1 &&
        view.getUint32(offset + 4) === 0x45786966 && view.getUint16(offset + 8) === 0x0000) {
      const tiffStart = offset + 10;
      const little = view.getUint16(tiffStart) === 0x4949;
      const ifd0Offset = view.getUint32(tiffStart + 4, little);
      const ifd0 = readIfd(view, tiffStart, ifd0Offset, little, new Set([TAG_ORIENTATION, TAG_EXIF_IFD]));
      let exifIfd = {};
      if (ifd0[TAG_EXIF_IFD] != null) {
        exifIfd = readIfd(view, tiffStart, ifd0[TAG_EXIF_IFD], little,
          new Set([TAG_FOCAL_LENGTH, TAG_FOCAL_LENGTH_35MM, TAG_LENS_MODEL]));
      }
      return {
        orientation: ifd0[TAG_ORIENTATION] ?? null,
        focalLength: exifIfd[TAG_FOCAL_LENGTH] ?? null,
        focalLength35mm: exifIfd[TAG_FOCAL_LENGTH_35MM] ?? null,
        lensModel: exifIfd[TAG_LENS_MODEL] ?? null,
      };
    }
    if (marker === 0xffda) break; // start of scan: no more APPn segments
    offset += 2 + segLen;
  }
  return null;
}

/** §4.1 lens/zoom-consistency verdict against photo 1's parsed EXIF.
 * `cur`/`ref` are parseExif() results (or null). */
export function classifyLens(cur, ref) {
  if (!cur || !ref) return "missing";
  const fields = [
    ["focalLength", (a, b) => Math.abs(a - b) <= 0.02 * Math.max(a, b, 1e-6)],
    ["focalLength35mm", (a, b) => a === b],
    ["lensModel", (a, b) => a === b],
  ];
  let compared = false;
  for (const [key, eq] of fields) {
    const a = cur[key], b = ref[key];
    if (a == null || b == null) continue;
    compared = true;
    if (!eq(a, b)) return "fail";
  }
  return compared ? "ok" : "missing";
}

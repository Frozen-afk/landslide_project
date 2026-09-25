// T0 vs server metric parity (GUIDED_CAPTURE_PLAN.md S1, §11 criterion 1).
// Pure functions, no DOM/jsdom needed. Run: node --test tests/test_capture_metrics.mjs
// Fixtures: tests/fixtures/capture/expected.json, produced by
// tests/fixtures/capture/make_fixtures.py from the real
// landslide.pipeline._photo_metrics (regenerate after changing either side).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  laplacianVariance, clippedFraction, dHash, hamming,
  classifyBlur, classifyExposure, classifyDuplicate,
  classifyResolution, classifyOrientation, classifyLens, parseExif,
} from "../server/static/js/capture_metrics.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const expected = JSON.parse(readFileSync(join(HERE, "fixtures/capture/expected.json"), "utf8"));

function toGray(flat, w, h) {
  const g = new Float32Array(w * h);
  g.set(flat);
  return g;
}

test("Laplacian variance, clip fraction and dHash match _photo_metrics within tolerance", () => {
  for (const m of expected.metrics) {
    const gray = toGray(m.gray, m.w, m.h);
    const lap = laplacianVariance(gray, m.w, m.h);
    const clip = clippedFraction(gray);
    const dhash = dHash(gray, m.w, m.h);

    const relErr = Math.abs(lap - m.lap) / Math.max(m.lap, 1e-6);
    assert.ok(relErr <= 0.10, `${m.name}: lap ${lap} vs ${m.lap} (rel ${relErr})`);
    assert.ok(Math.abs(clip - m.clip) <= 0.01, `${m.name}: clip ${clip} vs ${m.clip}`);
    const ham = hamming(Array.from(dhash), m.dhash);
    assert.ok(ham <= 2, `${m.name}: dhash hamming ${ham} vs server's own hash`);
  }
});

test("blur/exposure verdicts agree with the server's own cull formula", () => {
  const median = expected.batchMedianLap;
  for (const m of expected.metrics) {
    const gray = toGray(m.gray, m.w, m.h);
    const lap = laplacianVariance(gray, m.w, m.h);
    const clip = clippedFraction(gray);
    assert.equal(classifyBlur(lap, median) === "fail", m.serverBlurFail, `${m.name} blur`);
    assert.equal(classifyExposure(clip) === "fail", m.serverExposureFail, `${m.name} exposure`);
  }
});

test("duplicate check: near-identical photo is caught, distinct photos are not", () => {
  const byName = Object.fromEntries(expected.metrics.map((m) => [m.name, m]));
  const a = byName["sharp_a.jpg"], dup = byName["dup_of_a.jpg"], b = byName["sharp_b.jpg"];
  const hashA = dHash(toGray(a.gray, a.w, a.h), a.w, a.h);
  const hashDup = dHash(toGray(dup.gray, dup.w, dup.h), dup.w, dup.h);
  const hashB = dHash(toGray(b.gray, b.w, b.h), b.w, b.h);
  assert.equal(classifyDuplicate(hamming(hashA, hashDup)), "fail");
  assert.equal(classifyDuplicate(hamming(hashA, hashB)), "ok");
  assert.equal(classifyDuplicate(null), "ok"); // nothing to compare yet
});

test("resolution and orientation verdicts (§4.1, pure arithmetic)", () => {
  assert.equal(classifyResolution(1600), "ok");
  assert.equal(classifyResolution(1599), "warn");
  assert.equal(classifyResolution(4032), "ok");
  assert.equal(classifyOrientation(1, 1), "ok");
  assert.equal(classifyOrientation(6, 1), "warn");
  assert.equal(classifyOrientation(null, 1), "ok"); // nothing to compare yet
});

test("EXIF parsing: focal length, 35mm-equivalent, lens model, orientation", () => {
  const byName = Object.fromEntries(expected.exifFixtures.map((e) => [e.name, e]));
  const buf = (b64) => Uint8Array.from(Buffer.from(b64, "base64")).buffer;

  const ref = parseExif(buf(byName["exif_ref.jpg"].jpeg_b64));
  assert.equal(ref.orientation, 1);
  assert.ok(Math.abs(ref.focalLength - 5.0) < 1e-6);
  assert.equal(ref.focalLength35mm, 26);
  assert.equal(ref.lensModel, "Test Cam 1x");

  const switched = parseExif(buf(byName["exif_lens_switch.jpg"].jpeg_b64));
  assert.equal(classifyLens(switched, ref), "fail");

  const none = parseExif(buf(byName["exif_none.jpg"].jpeg_b64));
  assert.equal(none, null);
  assert.equal(classifyLens(none, ref), "missing");
  assert.equal(classifyLens(ref, none), "missing");

  const rotated = parseExif(buf(byName["exif_rotated.jpg"].jpeg_b64));
  assert.equal(rotated.orientation, 6);
  assert.equal(classifyOrientation(rotated.orientation, ref.orientation), "warn");
  // same lens/focal as the reference: lens check itself stays ok
  assert.equal(classifyLens(rotated, ref), "ok");
});

test("classifyBlur uses the absolute floor before a session median exists", () => {
  assert.equal(classifyBlur(5, null), "fail");   // below the 6.0 floor
  assert.equal(classifyBlur(6, null), "ok");
  assert.equal(classifyBlur(200, 20), "ok");
  assert.equal(classifyBlur(4, 20), "fail");      // below max(6, 0.25*20)=6
  assert.equal(classifyBlur(9, 20), "warn");      // below 0.5*20=10
});

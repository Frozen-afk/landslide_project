// Sequence/viewpoint guidance (GUIDED_CAPTURE_PLAN.md S3, §4.2). Pure
// functions, no DOM. Run: node --test tests/test_capture_guidance.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  circularMeanDeg, circularDiffDeg, circularSpreadDeg,
  photoCountGuidance, markerViewGuidance, overlapGapGuidance,
  viewpointDiversityGuidance, cameraHeightGuidance, armTransitionGuidance,
} from "../server/static/js/capture_guidance.js";

test("circular stats: mean, diff and spread handle the 0/360 wraparound", () => {
  assert.ok(Math.abs(circularMeanDeg([350, 10]) - 0) < 1e-6 ||
             Math.abs(circularMeanDeg([350, 10]) - 360) < 1e-6);
  assert.equal(circularMeanDeg([]), null);
  assert.equal(circularDiffDeg(350, 10), 20);
  assert.equal(circularDiffDeg(10, 350), -20);
  assert.ok(Math.abs(circularSpreadDeg([350, 10, 0]) - 20) < 1e-6);
  assert.equal(circularSpreadDeg([45]), 0);
});

test("photo count guidance fires under the per-arm and total minimums, silent once met", () => {
  const low = photoCountGuidance({ counts: { A: 5, B: 3 }, total: 8 });
  assert.equal(low.length, 3); // A, B, total all short
  const met = photoCountGuidance({ counts: { A: 12, B: 12 }, total: 24 });
  assert.equal(met.length, 0);
});

test("marker view guidance names the short arm and the total", () => {
  const g = markerViewGuidance({ marker_views: { A: 2, B: 1 }, marker_views_total: 3 });
  assert.ok(g.some((x) => x.id === "marker_total"));
  assert.ok(g.some((x) => x.id === "marker_B"));
  assert.ok(!g.some((x) => x.id === "marker_A")); // A already has 2
  const g2 = markerViewGuidance({ marker_views: { A: 3, B: 2 }, marker_views_total: 5 });
  assert.equal(g2.length, 0);
});

test("overlap gap guidance resolves a uuid to its photo number when known", () => {
  const g = overlapGapGuidance({ overlap_gaps: ["u1", "u2"] }, { u1: 8 });
  assert.equal(g[0].text, "Gap before photo #8 — retake between them.");
  assert.ok(g[1].text.includes("Gap between two photos"));
});

test("viewpoint diversity: needs >=2 headings per arm, silent when sensors are unavailable", () => {
  assert.equal(viewpointDiversityGuidance({}).length, 0);
  assert.equal(viewpointDiversityGuidance({ A: [10] }).length, 0); // 1 reading, not enough
  const narrow = viewpointDiversityGuidance({ A: [0, 2, 4] });
  assert.ok(narrow.some((x) => x.id === "spread_A"));
  const wide = viewpointDiversityGuidance({ A: [-20, 0, 20] });
  assert.ok(!wide.some((x) => x.id === "spread_A"));
});

test("arm separation: warns both below 60deg and above 120deg, silent in between", () => {
  const close = viewpointDiversityGuidance({ A: [0, 5, -5], B: [30, 35, 25] });
  assert.ok(close.some((x) => x.id === "arm_separation_low"));
  const wide = viewpointDiversityGuidance({ A: [0, 5, -5], B: [150, 155, 145] });
  assert.ok(wide.some((x) => x.id === "arm_separation_high"));
  const ok = viewpointDiversityGuidance({ A: [0, 5, -5], B: [90, 95, 85] });
  assert.ok(!ok.some((x) => x.id.startsWith("arm_separation")));
});

test("camera height: warns when >30% of an arm's pitch readings are below 10deg", () => {
  const low = cameraHeightGuidance({ A: [2, 3, 25, 30] }); // 2/4 = 50% low
  assert.ok(low.some((x) => x.id === "pitch_A"));
  const ok = cameraHeightGuidance({ A: [25, 30, 22, 2] }); // 1/4 = 25% low
  assert.ok(!ok.some((x) => x.id === "pitch_A"));
  assert.equal(cameraHeightGuidance({ A: [5] }).length, 0); // 1 reading only
});

test("arm transition: hints while on the transition arm, warns on a weak bridge, silent on arm A", () => {
  assert.equal(armTransitionGuidance("A", null).length, 0);
  assert.equal(armTransitionGuidance("T", null)[0].id, "arm_bridge_hint");
  assert.equal(armTransitionGuidance("B", false)[0].id, "arm_bridge");
  assert.equal(armTransitionGuidance("B", true).length, 0);
});

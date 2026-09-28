// Sequence/viewpoint guidance (GUIDED_CAPTURE_PLAN.md S3, §4.2). Pure
// functions, no DOM. Run: node --test tests/test_capture_guidance.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  circularMeanDeg, circularDiffDeg, circularSpreadDeg,
  photoCountGuidance, markerViewGuidance, overlapGapGuidance,
  viewpointDiversityGuidance, cameraHeightGuidance, armTransitionGuidance,
  nextCaptureSeq, lastCaptureArm, nextBackoffMs, pendingNotYetOnServer,
  coverageSummaryText, instructionProgress, sketchLayout,
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

// S5 (§10 persistence and recovery): pure resume/retry-queue helpers.

test("nextCaptureSeq: one past the highest seq across manifest and still-queued photos", () => {
  assert.equal(nextCaptureSeq([], []), 0);
  assert.equal(nextCaptureSeq([{ seq: 0 }, { seq: 1 }], []), 2);
  // a queued-but-not-yet-acked photo can hold the highest seq after a crash
  assert.equal(nextCaptureSeq([{ seq: 0 }], [{ seq: 3, arm: "A" }]), 4);
});

test("lastCaptureArm: resumes on the arm of the highest-seq photo, 'A' when empty", () => {
  assert.equal(lastCaptureArm([], []), "A");
  assert.equal(lastCaptureArm([{ seq: 0, arm: "A" }, { seq: 1, arm: "T" }], []), "T");
  // a still-queued photo taken after the last acked one wins
  assert.equal(lastCaptureArm([{ seq: 0, arm: "A" }], [{ seq: 1, arm: "B" }]), "B");
});

test("nextBackoffMs: doubles on failure up to the 60s cap, resets to 2s on success", () => {
  assert.equal(nextBackoffMs(2000, false), 4000);
  assert.equal(nextBackoffMs(45000, false), 60000);
  assert.equal(nextBackoffMs(60000, false), 60000);
  assert.equal(nextBackoffMs(60000, true), 2000);
});

test("pendingNotYetOnServer: only uuids missing from the manifest need a re-send", () => {
  const manifest = [{ uuid: "a" }, { uuid: "b" }];
  assert.deepEqual(pendingNotYetOnServer(manifest, ["a", "c"]), ["c"]);
  assert.deepEqual(pendingNotYetOnServer(manifest, ["a", "b"]), []);
  assert.deepEqual(pendingNotYetOnServer([], ["a"]), ["a"]);
});

// S5C (§12c.9, §11 criterion 9): coverage/ROI display helpers.

test("coverageSummaryText: predicted class wins, else a flagged percentage", () => {
  assert.equal(coverageSummaryText(null), "Coverage estimate unavailable");
  assert.equal(coverageSummaryText({ unavailable: "marker scale failed" }),
    "Coverage estimate unavailable (marker scale failed)");
  assert.equal(coverageSummaryText({ preview_coverage: 0.71, roi_source: "default" }),
    "Coverage estimate 71% (lower-confidence: no outline drawn)");
  assert.equal(coverageSummaryText({ preview_coverage: 0.71, roi_source: "tap" }),
    "Coverage estimate 71%");
  assert.equal(coverageSummaryText({ preview_coverage: 0.9, roi_source: "tap", predicted_g6: "indicative" }),
    "Predicted: indicative");
});

test("instructionProgress: counts only photos after max_seq on the instruction's arm", () => {
  const manifest = [
    { uuid: "1", arm: "A", seq: 0, excluded: false },
    { uuid: "2", arm: "A", seq: 5, excluded: false },
    { uuid: "3", arm: "A", seq: 6, excluded: false },
    { uuid: "4", arm: "B", seq: 7, excluded: false },
    { uuid: "5", arm: "A", seq: 8, excluded: true },
  ];
  const instructions = [
    { kind: "extend_arm", arm: "A", n_photos: 2 },
    { kind: "new_arm", arm: "B", n_photos: 12 },
    { kind: "higher", arm: "A", n_photos: 0 },
  ];
  const out = instructionProgress(instructions, manifest, 4);
  assert.deepEqual(out[0], { k: 2, n: 2, done: true });    // seq 5, 6 on arm A
  assert.deepEqual(out[1], { k: 1, n: 12, done: false });  // seq 7 on arm B
  assert.deepEqual(out[2], { k: 0, n: 0, done: true });    // "higher" has no photo-count progress
});

test("sketchLayout: fits the ROI/cameras with margin, north arrow only with an offset", () => {
  const coverage = {
    grid: { u0: 0, v0: 0, cell: 1, nx: 4, ny: 4, classes: "gggg\nwwvv\nGGVV\n...." },
    roi_ground: [[0, 0], [3, 0], [3, 3], [0, 3]],
    roi_centroid_uv: [1.5, 1.5],
    cameras: [{ name: "A0", arm: "A", u: -2, v: -2 }, { name: "A1", arm: "A", u: 6, v: 6 }],
    instructions: [{ kind: "extend_arm", arm: "A", target_az: 90, stand_m: 5 }],
    compass_offset_deg: null,
  };
  const layout = sketchLayout(coverage, 200, 200, 8);
  assert.ok(layout);
  for (const p of [...layout.roiPath, ...layout.camDots]) {
    assert.ok(p.x >= 8 - 1e-6 && p.x <= 200 - 8 + 1e-6);
    assert.ok(p.y >= 8 - 1e-6 && p.y <= 200 - 8 + 1e-6);
  }
  assert.equal(layout.targets.length, 1);
  assert.equal(layout.north, null);

  const withNorth = sketchLayout({ ...coverage, compass_offset_deg: 12 }, 200, 200);
  assert.ok(withNorth.north);
  assert.equal(withNorth.north.angleDeg, -12);

  assert.equal(sketchLayout({ unavailable: "x" }, 200, 200), null);
});

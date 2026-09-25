/* Pure sequence/viewpoint guidance (GUIDED_CAPTURE_PLAN.md S3, §4.2). No DOM
 * anywhere in this file — unit-testable with `node --test`, same pattern as
 * capture_metrics.js / tests/test_capture_metrics.mjs.
 *
 * Two input sources:
 *  - the server's T2 `sequence` dict (landslide/capture.py::sequence_checks,
 *    returned by every PUT .../capture/photos/{uuid}): photo count, marker
 *    views, overlap gaps.
 *  - client-side sensor readings (compass heading, pitch), which the server
 *    does not aggregate yet (GUIDED_CAPTURE_PROGRESS.md S2: "sequence_checks
 *    omits heading/arm-separation/elevation ... need client sensor payloads
 *    S3's UI doesn't send yet"). These are advisory only (§4.2: "The sensor
 *    checks are advisory. The T3 preview decides.") and every function here
 *    simply omits guidance it cannot compute from < 2 readings, instead of
 *    treating a missing/declined sensor as a failure (§8).
 */

export const PHOTOS_PER_ARM_TARGET = 12;
export const TOTAL_PHOTOS_MIN = 15;
export const TOTAL_PHOTOS_TARGET = "25–35";
export const MARKER_VIEWS_MIN_TOTAL = 5;
export const MARKER_VIEWS_MIN_PER_ARM = 2;
export const HEADING_SPREAD_MIN_DEG = 30;
export const ARM_SEPARATION_WARN_MIN_DEG = 60;
export const ARM_SEPARATION_WARN_MAX_DEG = 120;
export const PITCH_WARN_DEG = 10;
export const PITCH_WARN_FRACTION = 0.3;

/** Mean direction of a set of angles (degrees), via the unit-vector sum.
 * Returns null for an empty input. */
export function circularMeanDeg(anglesDeg) {
  if (!anglesDeg.length) return null;
  let sx = 0, sy = 0;
  for (const a of anglesDeg) {
    const r = (a * Math.PI) / 180;
    sx += Math.cos(r); sy += Math.sin(r);
  }
  return ((Math.atan2(sy, sx) * 180) / Math.PI + 360) % 360;
}

/** Smallest signed difference b-a, in (-180, 180]. Robust to a constant
 * heading-convention offset or a flipped sign (both cancel in a difference),
 * which is why raw, uncalibrated compass readings are usable here even
 * though §5.4's model-azimuth alignment (S4/S5) is not implemented. */
export function circularDiffDeg(a, b) {
  return ((((b - a + 180) % 360) + 360) % 360) - 180;
}

/** Smallest arc (degrees) that covers every angle — the circular analogue of
 * max-min range. < 2 points has no meaningful spread. */
export function circularSpreadDeg(anglesDeg) {
  if (anglesDeg.length < 2) return 0;
  const s = anglesDeg.map((a) => ((a % 360) + 360) % 360).sort((a, b) => a - b);
  let maxGap = 0;
  for (let i = 0; i < s.length; i++) {
    const next = i + 1 < s.length ? s[i + 1] : s[0] + 360;
    maxGap = Math.max(maxGap, next - s[i]);
  }
  return 360 - maxGap;
}

/** §4.2 "Photo count" row: per-arm and total counts against the protocol
 * minimums. `sequence` is the server's T2 dict as-is. */
export function photoCountGuidance(sequence) {
  const out = [];
  const counts = sequence.counts || {};
  for (const arm of ["A", "B"]) {
    const n = counts[arm] || 0;
    if (n < PHOTOS_PER_ARM_TARGET) {
      out.push({ level: "warn", id: `count_${arm}`,
        text: `Arm ${arm}: ${n} of ${PHOTOS_PER_ARM_TARGET} — keep walking the arc.` });
    }
  }
  const total = sequence.total || 0;
  if (total < TOTAL_PHOTOS_MIN) {
    out.push({ level: "warn", id: "count_total",
      text: `${total} of ${TOTAL_PHOTOS_MIN} photos required (target ${TOTAL_PHOTOS_TARGET}).` });
  }
  return out;
}

/** §4.2 "Marker views" row. */
export function markerViewGuidance(sequence) {
  const mv = sequence.marker_views || {};
  const total = sequence.marker_views_total || 0;
  const out = [];
  if (total < MARKER_VIEWS_MIN_TOTAL) {
    out.push({ level: "warn", id: "marker_total",
      text: `Marker seen in only ${total} of ${MARKER_VIEWS_MIN_TOTAL} required photos — include it more often.` });
  }
  for (const arm of ["A", "B"]) {
    const n = mv[arm] || 0;
    if (n < MARKER_VIEWS_MIN_PER_ARM) {
      out.push({ level: "warn", id: `marker_${arm}`,
        text: `Marker seen in only ${n} arm-${arm} photo${n === 1 ? "" : "s"} — include it in ${MARKER_VIEWS_MIN_PER_ARM - n} more.` });
    }
  }
  return out;
}

/** §4.2 "Overlap chain" row. `seqByUuid` maps a photo uuid to its 1-based
 * position, so the message can name a photo number instead of a uuid. */
export function overlapGapGuidance(sequence, seqByUuid) {
  const gaps = sequence.overlap_gaps || [];
  return gaps.map((uuid) => {
    const n = seqByUuid ? seqByUuid[uuid] : null;
    return {
      level: "warn", id: `gap_${uuid}`,
      text: n != null
        ? `Gap before photo #${n} — retake between them.`
        : "Gap between two photos — retake between them.",
    };
  });
}

/** §4.2 "Heading spread per arm" and "Arm separation" rows. `headingsByArm`
 * is `{A: [deg,...], B: [deg,...]}`, built from whatever `client.heading`
 * values were sent with each kept photo (missing/declined sensor -> short
 * or empty arrays -> silently skipped, per §8). */
export function viewpointDiversityGuidance(headingsByArm) {
  const out = [];
  const a = headingsByArm.A || [], b = headingsByArm.B || [];
  for (const [arm, hs] of [["A", a], ["B", b]]) {
    if (hs.length < 2) continue;
    if (circularSpreadDeg(hs) < HEADING_SPREAD_MIN_DEG) {
      out.push({ level: "warn", id: `spread_${arm}`,
        text: `Arm ${arm}: your photos all point the same way — walk further around the slide.` });
    }
  }
  if (a.length >= 2 && b.length >= 2) {
    const meanA = circularMeanDeg(a), meanB = circularMeanDeg(b);
    const sep = Math.abs(circularDiffDeg(meanA, meanB));
    if (sep < ARM_SEPARATION_WARN_MIN_DEG) {
      const target = Math.round((meanA + 90) % 360);
      out.push({ level: "warn", id: "arm_separation_low",
        text: `Arm B is only ${sep.toFixed(0)}° from arm A — walk further around, to about bearing ${target}°.` });
    } else if (sep > ARM_SEPARATION_WARN_MAX_DEG) {
      out.push({ level: "warn", id: "arm_separation_high",
        text: `Arm B is ${sep.toFixed(0)}° from arm A — that's wide enough that one marker board may not face both arms.` });
    }
  }
  return out;
}

/** §4.2 "Elevation (pitch)" row. `pitchesByArm` is `{A: [deg,...], B: [...]}`
 * of camera pitch below horizontal (§4.1's proxy-only client metric). */
export function cameraHeightGuidance(pitchesByArm) {
  const out = [];
  for (const arm of ["A", "B"]) {
    const ps = pitchesByArm[arm] || [];
    if (ps.length < 2) continue;
    const lowFrac = ps.filter((p) => p < PITCH_WARN_DEG).length / ps.length;
    if (lowFrac > PITCH_WARN_FRACTION) {
      out.push({ level: "warn", id: `pitch_${arm}`,
        text: `Arm ${arm}: you're level with or below the debris for part of this arm — get higher, never below the crown.` });
    }
  }
  return out;
}

/** §3 step 4 / §4.1 "arm_bridge" — guidance for the walk between arms.
 * `bridgeOk` is `null` when no bridge check has run yet, `false` when the
 * most recent bridge-eligible photo's T1 check flagged a weak link. */
export function armTransitionGuidance(currentArm, bridgeOk) {
  if (currentArm !== "T" && currentArm !== "B") return [];
  if (bridgeOk === false) {
    return [{ level: "warn", id: "arm_bridge",
      text: "Arm B isn't linking to arm A yet — take 3–5 photos while walking back toward your first position." }];
  }
  if (currentArm === "T") {
    return [{ level: "info", id: "arm_bridge_hint",
      text: "Keep shooting every few steps while walking to arm B, so the two arms register into one model." }];
  }
  return [];
}

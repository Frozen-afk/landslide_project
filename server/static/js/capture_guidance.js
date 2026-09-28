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

/** §10 resume ("return to the same arm and seq"): the next 1-based seq for
 * a new photo, given the server manifest plus any not-yet-acked client
 * queue entries — so a still-retrying photo's seq is never reused by a
 * fresh one taken right after reload. `pendingMeta` is the `meta` object
 * (`{seq, arm, client}`) of each `capture_store.js` queue entry. */
export function nextCaptureSeq(manifestPhotos, pendingMeta) {
  const seqs = [-1, ...manifestPhotos.map((p) => p.seq), ...pendingMeta.map((m) => m.seq)];
  return Math.max(...seqs) + 1;
}

/** §10 resume: the arm of whichever photo (kept or still-queued) has the
 * highest seq, so a reload lands back on the arm the user was shooting.
 * "A" for a session with nothing yet. */
export function lastCaptureArm(manifestPhotos, pendingMeta) {
  const all = [...manifestPhotos.map((p) => ({ seq: p.seq, arm: p.arm })),
               ...pendingMeta.map((m) => ({ seq: m.seq, arm: m.arm }))];
  if (!all.length) return "A";
  return all.reduce((best, p) => (p.seq > best.seq ? p : best)).arm;
}

/** §10 "retries with exponential backoff (2s -> 60s cap)": the next delay
 * given the current one and whether the last drain of the queue fully
 * succeeded (empties it) or stopped partway (still offline/server down). */
export function nextBackoffMs(currentMs, ok) {
  return ok ? 2000 : Math.min(60000, currentMs * 2);
}

/** §10 "re-queue any IndexedDB entries whose uuid is missing from the
 * manifest": uuids the client still has queued that the server has NOT
 * acked. The rest were accepted before the crash/refresh and only need a
 * local ack, never a re-send — this is what keeps a retried queue from
 * ever duplicating a photo the server already has. */
export function pendingNotYetOnServer(manifestPhotos, pendingUuids) {
  const known = new Set(manifestPhotos.map((p) => p.uuid));
  return pendingUuids.filter((u) => !known.has(u));
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

// ---------------------------------------------------------------------------
// S5C (GUIDED_CAPTURE_PLAN.md §12c.9): coverage/ROI display helpers. Pure —
// the `coverage` object here is `preview_report(...)["coverage"]` (§12c.8)
// as returned by `GET .../capture` / `POST .../capture/preview`.
// ---------------------------------------------------------------------------

/** §12c.9: one-line coverage summary — "Predicted: <class>" once
 * calibration has fixed a class, otherwise a bare percentage, flagged
 * lower-confidence when the ROI came from the §5.1 default (no outline
 * drawn) rather than a tap. */
export function coverageSummaryText(coverage) {
  if (!coverage || coverage.unavailable) {
    return `Coverage estimate unavailable${coverage && coverage.unavailable ? ` (${coverage.unavailable})` : ""}`;
  }
  if (coverage.predicted_g6) return `Predicted: ${coverage.predicted_g6}`;
  const pct = Math.round((coverage.preview_coverage || 0) * 100);
  const note = coverage.roi_source === "tap" ? "" : " (lower-confidence: no outline drawn)";
  return `Coverage estimate ${pct}%${note}`;
}

/** §12c.9: `[{k, n, done}]` per instruction — the count of kept photos on
 * the instruction's own arm taken after the report's `max_seq` (§12c.6:
 * "derived, not stored"). `higher`/rim instructions have no photo-count
 * progress of their own (they're a re-shoot-in-place, not a walk-and-count),
 * so they report `done: true` immediately. */
export function instructionProgress(instructions, manifestPhotos, maxSeq) {
  return (instructions || []).map((ins) => {
    if (ins.kind !== "extend_arm" && ins.kind !== "new_arm") {
      return { k: 0, n: 0, done: true };
    }
    const n = ins.n_photos || 12;
    const k = (manifestPhotos || [])
      .filter((p) => !p.excluded && p.arm === ins.arm && p.seq > (maxSeq ?? -1)).length;
    return { k, n, done: k >= n };
  });
}

/** §12c.9 top-down sketch layout: fits the ROI polygon, the observation-map
 * cell grid, and the camera dots into a `w`x`h` canvas with >= `margin` px
 * of border, plus a target wedge per directional instruction (an arc from
 * the ROI centroid out to `stand_m` at `target_az`) and a north arrow only
 * when `compass_offset_deg` is known. Returns null when there's nothing to
 * draw yet (`coverage` unavailable). All output is canvas px, y-down, with
 * ground +v mapped to "up" on screen. */
export function sketchLayout(coverage, w, h, margin = 8) {
  if (!coverage || coverage.unavailable || !coverage.grid) return null;
  const { u0, v0, cell, nx, ny, classes } = coverage.grid;
  const cams = coverage.cameras || [];
  const u1 = u0 + nx * cell, v1 = v0 + ny * cell;
  const us = [u0, u1, ...cams.map((c) => c.u)];
  const vs = [v0, v1, ...cams.map((c) => c.v)];
  const minU = Math.min(...us), maxU = Math.max(...us);
  const minV = Math.min(...vs), maxV = Math.max(...vs);
  const spanU = Math.max(maxU - minU, 1e-6), spanV = Math.max(maxV - minV, 1e-6);
  const availW = Math.max(w - 2 * margin, 1), availH = Math.max(h - 2 * margin, 1);
  const scale = Math.min(availW / spanU, availH / spanV);
  const toPx = (u, v) => ({
    x: margin + (u - minU) * scale,
    y: h - margin - (v - minV) * scale,
  });

  const roiPath = (coverage.roi_ground || []).map(([u, v]) => toPx(u, v));

  const rows = classes.split("\n");
  const cells = [];
  for (let iy = 0; iy < ny; iy++) {
    for (let ix = 0; ix < nx; ix++) {
      const cls = rows[iy][ix];
      if (cls === ".") continue;
      const p0 = toPx(u0 + ix * cell, v0 + iy * cell);
      const p1 = toPx(u0 + (ix + 1) * cell, v0 + (iy + 1) * cell);
      cells.push({ cls, x: Math.min(p0.x, p1.x), y: Math.min(p0.y, p1.y),
        w: Math.abs(p1.x - p0.x), h: Math.abs(p1.y - p0.y) });
    }
  }

  const camDots = cams.map((c) => ({ ...toPx(c.u, c.v), arm: c.arm, name: c.name }));

  const roiC = coverage.roi_centroid_uv || [0, 0];
  const from = toPx(roiC[0], roiC[1]);
  const targets = (coverage.instructions || [])
    .filter((ins) => ins.target_az != null && ins.stand_m != null)
    .map((ins) => {
      const rad = (ins.target_az * Math.PI) / 180;
      const to = toPx(roiC[0] + ins.stand_m * Math.cos(rad),
                      roiC[1] + ins.stand_m * Math.sin(rad));
      return { kind: ins.kind, arm: ins.arm, fromX: from.x, fromY: from.y, toX: to.x, toY: to.y };
    });

  const north = coverage.compass_offset_deg != null
    ? { angleDeg: -coverage.compass_offset_deg } : null;

  return { scale, marginPx: margin, roiPath, cells, camDots, targets, north };
}

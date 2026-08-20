"""Radiometric enhancement for degraded field photos + SfM ladder wiring."""
import sqlite3

import cv2
import numpy as np

from landslide.enhance import enhance_degraded_image, write_enhanced_dir
from landslide.sfm import LOW_CONTRAST_KP, _build_attempts, _median_keypoints


def _shadow_scene(w=240, h=180):
    """Bright half + deeply shadowed half, both carrying the same texture."""
    x = np.arange(w, dtype=np.float32)
    texture = 18.0 * np.sin(x / 3.5) + 9.0 * np.sin(x / 1.3 + 1.0)
    tex = np.tile(texture, (h, 1))
    img = np.dstack([tex + 150, tex + 145, tex + 140])     # BGR, bright
    img[:, w // 2:] -= 105.0                               # deep shadow half
    return np.clip(img, 0, 255).astype(np.uint8)


def test_enhance_is_radiometric_only():
    img = _shadow_scene()
    out = enhance_degraded_image(img)
    assert out.shape == img.shape and out.dtype == np.uint8
    assert not (out == img).all(), "enhancement changed nothing"
    assert np.isfinite(out).all()


def test_enhance_recovers_shadow_texture():
    img = _shadow_scene()
    out = enhance_degraded_image(img)
    w = img.shape[1]
    # texture amplitude (local std) inside the shadow must grow: SIFT needs
    # those gradients to find keypoints there at all
    g_in = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)[:, w // 2 + 20:]
    g_out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)[:, w // 2 + 20:]
    std_in = g_in.astype(float).std()
    std_out = g_out.astype(float).std()
    assert std_out > 1.5 * std_in, f"shadow contrast {std_in:.1f} -> {std_out:.1f}"
    # and the bright half must not blow out to flat white
    bright_out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)[:, :w // 2 - 20]
    assert bright_out.std() > 2.0


def test_write_enhanced_dir_names_and_cache(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    for i in range(3):
        cv2.imwrite(str(photos / f"p{i}.jpg"), _shadow_scene(120, 90))
    logs = []
    dest = write_enhanced_dir(photos, tmp_path / "enh", log=logs.append)
    assert sorted(p.name for p in dest.iterdir()) == ["p0.jpg", "p1.jpg", "p2.jpg"]
    assert any("3 enhanced" in l for l in logs)
    out = cv2.imread(str(dest / "p0.jpg"))
    assert out.shape == (90, 120, 3)
    # unchanged sources -> second call writes nothing (cache)
    logs.clear()
    write_enhanced_dir(photos, dest, log=logs.append)
    assert not logs, f"cache miss rewrote: {logs}"


def _make_db(path, rows, with_table_id):
    con = sqlite3.connect(str(path))
    if with_table_id:
        con.execute("CREATE TABLE keypoints (image_id, table_id, rows, cols, data)")
        con.executemany(
            "INSERT INTO keypoints VALUES (?, 0, ?, 4, x'')",
            [(i + 1, r) for i, r in enumerate(rows)])
    else:
        con.execute("CREATE TABLE keypoints (image_id, rows, cols, data)")
        con.executemany(
            "INSERT INTO keypoints VALUES (?, ?, 4, x'')",
            [(i + 1, r) for i, r in enumerate(rows)])
    con.commit()
    con.close()


def test_median_keypoints_both_schemas(tmp_path):
    from pathlib import Path
    rows = [1000, 2000, 3000, 4000]        # median 2500
    for tid in (False, True):
        db = tmp_path / f"db_{tid}.db"
        _make_db(db, rows, tid)
        assert _median_keypoints(Path(db)) == 2500.0
    assert _median_keypoints(tmp_path / "missing.db") == float("inf")


def test_build_attempts_ladder_order():
    a = _build_attempts(21, 2400)
    labels = [x["label"] for x in a]
    assert labels[:2] == ["default", "dense sequential @3200px"]
    assert "enhanced low-contrast" in labels
    assert labels[-1] == "shared intrinsics"
    enhanced = next(x for x in a if x["label"] == "enhanced low-contrast")
    assert enhanced["enhanced"] is True
    assert enhanced["peak"] < 0.0067 and enhanced["edge"] > 10.0
    # the low-contrast attempt only makes sense when features are scarce
    assert LOW_CONTRAST_KP == 2500


def test_attempt_score_prefers_usable_geometry():
    from landslide.sfm import MIN_SPARSE_PER_IMG, _attempt_score
    # the field failure that motivated the floor: 19 images, 63 points
    starved = _attempt_score(19, 63)
    assert starved[0] == 0, "19 images on 63 points must not count as usable"
    # fewer images but healthy triangulation beats a starved registration
    healthy = _attempt_score(17, 17 * MIN_SPARSE_PER_IMG)
    assert healthy > starved
    # among usable attempts more images wins, ties break on points
    assert _attempt_score(20, 20 * MIN_SPARSE_PER_IMG) > healthy
    bigger = _attempt_score(20, 20 * MIN_SPARSE_PER_IMG + 500)
    assert bigger > _attempt_score(20, 20 * MIN_SPARSE_PER_IMG)
    # a single failed-but-registered image still scores sanely
    assert _attempt_score(1, 0)[0] == 0

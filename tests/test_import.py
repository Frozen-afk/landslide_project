"""Pre-SfM culling of blurry frames and near-duplicate photos."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageFilter

from landslide.pipeline import import_photos


def _sharp(seed):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 255, (480, 640, 3), dtype=np.uint8))


def _blurry(seed):
    rng = np.random.default_rng(seed)
    grad = np.tile(np.linspace(0, 255, 640, dtype=np.uint8), (480, 1))
    img = Image.fromarray(np.stack([grad] * 3, -1))
    return img.filter(ImageFilter.GaussianBlur(12))


def test_cull_drops_blurry_and_duplicates(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    a, b, c = _sharp(1), _sharp(2), _sharp(3)
    files = [src / "a.jpg", src / "blur1.jpg", src / "a_copy.jpg",
             src / "b.jpg", src / "blur2.jpg", src / "c.jpg"]
    a.save(files[0]); _blurry(1).save(files[1]); a.save(files[2])
    b.save(files[3]); _blurry(2).save(files[4]); c.save(files[5])

    dropped = []
    names = import_photos(files, tmp_path / "photos",
                          log=lambda m: dropped.append(m)
                          if "dropping" in m else None)
    kept_stems = {Path(n).stem.split("_", 1)[1] for n in names}
    assert "blur1" not in kept_stems and "blur2" not in kept_stems
    assert "a_copy" not in kept_stems          # duplicate of a.jpg
    assert {"a", "b", "c"} <= kept_stems       # sharp photos survive
    assert len(names) == 3
    for n in names:
        assert (tmp_path / "photos" / n).exists()


def test_cull_never_takes_too_much(tmp_path):
    # 8 sharp photos + 1 blurry: even though many are similar-ish, only the
    # clear outlier goes
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(8):
        p = src / f"s{i}.jpg"
        _sharp(10 + i).save(p)
        files.append(p)
    p = src / "blur.jpg"
    _blurry(9).save(p)
    files.append(p)
    names = import_photos(files, tmp_path / "photos", log=lambda *_: None)
    assert len(names) >= 8
    assert all("blur" not in n for n in names)


def _blown(seed, hi=True):
    """Textured frame with most of the histogram pinned at one rail."""
    rng = np.random.default_rng(seed)
    if hi:
        # base 251 + noise 0..4 -> ~60% of pixels at 253..255 (clipped)
        noise = rng.integers(0, 5, (480, 640, 3))
        arr = np.clip(251 + noise, 0, 255).astype(np.uint8)
    else:
        arr = rng.integers(0, 3, (480, 640, 3)).astype(np.uint8)
    return Image.fromarray(arr)


def test_cull_drops_exposed_frames_with_reason(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(4):
        p = src / f"s{i}.jpg"
        _sharp(20 + i).save(p)
        files.append(p)
    _blown(5).save(src / "blown.jpg")
    _blown(6, hi=False).save(src / "black.jpg")
    files += [src / "blown.jpg", src / "black.jpg"]

    lines = []
    names = import_photos(files, tmp_path / "photos", log=lines.append)
    kept_stems = {Path(n).stem.split("_", 1)[1] for n in names}
    assert "blown" not in kept_stems and "black" not in kept_stems
    assert len(names) == 4                          # sharp photos untouched
    reasons = [l for l in lines if "dropping" in l]
    assert sum("over/under-exposed" in l for l in reasons) == 2
    assert any("quality gate" in l and "2 over/under-exposed" in l
               for l in lines), lines               # explained summary


def test_cull_keeps_three_survivors_even_if_most_exposed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(3):
        p = src / f"s{i}.jpg"
        _sharp(30 + i).save(p)
        files.append(p)
    for i in range(5):
        p = src / f"e{i}.jpg"
        _blown(40 + i).save(p)
        files.append(p)
    names = import_photos(files, tmp_path / "photos", log=lambda *_: None)
    kept_stems = {Path(n).stem.split("_", 1)[1] for n in names}
    assert {"s0", "s1", "s2"} <= kept_stems          # the 3 sharp ones survive
    assert not any(n.startswith(("003", "004", "005", "006", "007"))
                   for n in names)

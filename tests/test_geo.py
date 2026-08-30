"""EXIF-GPS georeferencing (annotation-only, never scale)."""
import numpy as np

from landslide.geo import enu, read_gps, umeyama


def test_enu_local_frame():
    gps = {"a.jpg": [50.0, 8.0, 100.0],
           "b.jpg": [50.001, 8.0, 100.0],          # ~111 m north
           "c.jpg": [50.0, 8.0015, 101.0]}         # ~107 m east, +1 m up
    pts, names, origin = enu(gps)
    assert names == ["a.jpg", "b.jpg", "c.jpg"]
    assert origin == [50.0, 8.0, 100.0]
    b = pts[names.index("b.jpg")]
    c = pts[names.index("c.jpg")]
    assert abs(b[1] - 111.2) < 1.0 and abs(b[0]) < 0.01
    assert abs(c[0] - 107.3) < 1.0 and abs(c[2] - 1.0) < 0.01


def test_umeyama_fixed_and_free_scale():
    rng = np.random.default_rng(0)
    P = rng.uniform(-10, 10, (50, 3))
    R = np.linalg.qr(rng.standard_normal((3, 3)))[0]
    s_true, t = 2.7, np.array([5.0, -3.0, 12.0])
    Q = s_true * P @ R.T + t
    s, Rf, tf = umeyama(P, Q)
    assert abs(s - s_true) < 1e-9
    assert np.abs(Q - (s * P @ Rf.T + tf)).max() < 1e-6
    # fixed-scale variant: perfect rigid recovery at s=1
    Qr = P @ R.T + t
    s1, R1, t1 = umeyama(P, Qr, fixed_scale=1.0)
    assert s1 == 1.0
    assert np.abs(Qr - (P @ R1.T + t1)).max() < 1e-6


class _FakeImg:
    """PIL surface duck-type: getexif() -> .get_ifd(0x8825) -> GPS dict."""

    def __init__(self, gps):
        self._gps = gps

    def getexif(self):
        outer = self

        class E:
            def get_ifd(self, tag):
                return outer._gps if tag == 0x8825 else None
        return E()


def test_read_gps_parses_dms_and_refs():
    gps = {1: b"N", 2: ((50, 1), (0, 1), (0, 1)),
           3: b"E", 4: ((8, 1), (30, 1), (0, 1)),
           6: (12345, 100)}                  # altitude is ONE rational
    g = read_gps(_FakeImg(gps))
    assert g is not None
    assert abs(g[0] - 50.0) < 1e-9 and abs(g[1] - 8.5) < 1e-9
    assert abs(g[2] - 123.45) < 0.01
    # southern / western hemispheres negate
    g2 = read_gps(_FakeImg({1: b"S", 2: ((10, 1), (30, 1), (0, 1)),
                            3: b"W", 4: ((70, 1), (0, 1), (0, 1))}))
    assert g2[0] < 0 and g2[1] < 0
    # missing IFD / garbage -> None, never raises
    assert read_gps(_FakeImg({})) is None
    assert read_gps(_FakeImg({2: "junk"})) is None


def test_read_gps_on_real_photo():
    """Live check against the user's actual phone JPEGs (no GPS -> None)."""
    from PIL import Image
    p = list(__import__("pathlib").Path(
        "/home/frozen/Documents/landslide-volume/data/jobs/"
        "20260816-150959-37d845/photos").glob("*.jpg"))
    if not p:
        return
    for f in p[:5]:
        with Image.open(f) as im:
            g = read_gps(im)          # must not raise, whatever it holds
            if g is not None:
                assert -90 <= g[0] <= 90 and -180 <= g[1] <= 180

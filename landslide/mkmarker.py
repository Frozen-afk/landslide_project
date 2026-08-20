"""Generate a printable ArUco reference marker (PNG + exact-size HTML)."""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def generate(out_png, out_html=None, side_m: float = 0.25,
             dict_name: str = "DICT_6X6_250", marker_id: int = 0,
             border_m: float = 0.03):
    ar = cv2.aruco
    dictionary = ar.getPredefinedDictionary(getattr(ar, dict_name))
    inner = ar.generateImageMarker(dictionary, marker_id, 1500, borderBits=1)

    # white margin so the printer never clips the quiet zone
    pad = int(round(1500 * border_m / side_m))
    canvas = np.full((inner.shape[0] + 2 * pad,) * 2, 255, np.uint8)
    canvas[pad:pad + inner.shape[0], pad:pad + inner.shape[1]] = inner
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_png), canvas)

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ArUco marker {marker_id}</title>
<style>
  body {{ font-family: sans-serif; margin: 30px; }}
  img.marker {{ width: {side_m * 100:.1f}cm; height: {side_m * 100:.1f}cm; }}
  @page {{ size: A4; margin: 10mm; }}
  @media print {{ .no-print {{ display: none; }} }}
</style></head>
<body>
<h2>ArUco reference marker — {dict_name}, id {marker_id}</h2>
<p class="no-print">Print this page at <b>100% scale</b> (no "fit to page"),
verify the black square below measures exactly <b>{side_m * 100:.0f} cm</b>
across (inner black square, excluding white border) with a ruler, then glue it
to something rigid. When photographing, keep it flat, unobstructed, and in at
least 2–3 photos. In the app, enter {side_m * 100:.0f} cm as the marker side.</p>
<img class="marker" src="{Path(out_png).name}">
</body></html>
"""
    out_html = Path(out_html or out_png.with_suffix(".html"))
    out_html.write_text(html)
    return out_png, out_html


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--side", type=float, default=0.25, help="side length in meters")
    p.add_argument("--dict", dest="dict_name", default="DICT_6X6_250")
    p.add_argument("--id", type=int, default=0)
    p.add_argument("--out", default="aruco_marker.png")
    a = p.parse_args()
    png, html = generate(a.out, side_m=a.side, dict_name=a.dict_name, marker_id=a.id)
    print(f"wrote {png} and {html}")


if __name__ == "__main__":
    main()

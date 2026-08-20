"""Command-line interface.

Examples:
  python -m landslide.cli marker --side 0.25 --out aruco_marker.png
  python -m landslide.cli run spec.json          # full pipeline from a spec
  python -m landslide.cli spec-template          # print an example spec
"""
from __future__ import annotations

import argparse
import json
import sys


def _cmd_run(args):
    from .pipeline import run_spec
    with open(args.spec) as f:
        spec = json.load(f)
    res = run_spec(spec, log=print)
    print(json.dumps({k: v for k, v in res.items() if k != "polygon_px"},
                     indent=2))


def _cmd_marker(args):
    from .mkmarker import generate
    png, html = generate(args.out, side_m=args.side, dict_name=args.dict_name,
                         marker_id=args.id)
    print(f"wrote {png}\nprint {html} at 100% scale")


TEMPLATE = {
    "photos_dir": "/path/to/photos",
    "out_dir": "/path/to/output",
    "save_cloud": True,
    "scale": {
        "method": "aruco",            # or "manual"
        "side_m": 0.25,
        "dict": "DICT_6X6_250",
        "id": 0,
        # --- manual alternative ---
        # "method": "manual", "length_m": 2.0,
        # "a": {"image": "000_IMG_1.jpg", "p1": [x, y], "p2": [x, y]},
        # "b": {"image": "005_IMG_6.jpg", "p1": [x, y], "p2": [x, y]},
    },
    "region": {
        # "mode": "photo" (trace on a photo, perspective) or "ortho"
        # (trace on the auto-generated top-down orthophoto — no parallax;
        # with "ortho", "image" is ignored and "polygon" is in ortho pixels)
        "mode": "photo",
        "image": "010_IMG_11.jpg",
        "polygon": [[100, 200], [400, 180], [420, 500], [120, 520]],
        "dense": True,
        "rim_px": 12,
        # "rim_inner_px": 6,     # start the rim band this far outside the line
    },
}


def main(argv=None):
    p = argparse.ArgumentParser(prog="landslide",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run the full pipeline from a spec JSON")
    pr.add_argument("spec")
    pr.set_defaults(fn=_cmd_run)

    pm = sub.add_parser("marker", help="generate a printable ArUco marker")
    pm.add_argument("--side", type=float, default=0.25)
    pm.add_argument("--dict", dest="dict_name", default="DICT_6X6_250")
    pm.add_argument("--id", type=int, default=0)
    pm.add_argument("--out", default="aruco_marker.png")
    pm.set_defaults(fn=_cmd_marker)

    pt = sub.add_parser("spec-template", help="print an example spec")
    pt.set_defaults(fn=lambda a: print(json.dumps(TEMPLATE, indent=2)))

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

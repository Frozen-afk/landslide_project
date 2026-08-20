"""Automatic landslide-region detection via a hosted segmentation model.

Sends one image to Roboflow's Serverless REST API (SegFormer landslide
segmentation) and returns detected boundaries as polygons in the coordinate
frame of the image that was sent — stored-photo pixels for photos, ortho
pixels for the orthophoto — so the result drops straight into the existing
manual-marking polygon state and measure().

Talks to the REST API directly with `requests` instead of `inference-sdk`:
every published SDK version requires Python <3.13 and this project runs on
3.14. The wire format is identical (base64 image in, predictions JSON out).

The API key never lives in source or client code: it is read from the
ROBOFLOW_API_KEY environment variable, falling back to a `.env` file at the
project root (which must stay out of version control).
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import cv2
import numpy as np

API_URL = "https://serverless.roboflow.com"
MODEL_ID = "segformer-landslide-detection/2"

# the SegFormer head works at fixed tile sizes (~512-1024 px); sending more
# only inflates the upload payload
MAX_UPLOAD_SIDE = 1600
MIN_CONFIDENCE = 0.35
MAX_POLY_POINTS = 120
TIMEOUT_S = 60

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_api_key(env_file: Path | None = None) -> str | None:
    """ROBOFLOW_API_KEY from the environment, else the first match in .env."""
    key = os.environ.get("ROBOFLOW_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ROBOFLOW_API_KEY") and "=" in line:
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _simplify(poly: np.ndarray, max_points: int) -> np.ndarray:
    """Douglas-Peucker the closed contour down to at most `max_points`."""
    contour = poly.reshape(-1, 1, 2).astype(np.float32)
    peri = max(cv2.arcLength(contour, True), 1.0)
    eps = 0.002 * peri
    for _ in range(12):
        out = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
        if len(out) <= max_points:
            break
        eps *= 1.6
    return np.asarray(out, np.float64) if len(out) >= 3 else poly


def _landslide_class_id(class_map: dict) -> int:
    """Class id the model assigns to 'landslide' (usually 1)."""
    for cid, name in (class_map or {}).items():
        if "landslide" in str(name).lower():
            return int(cid)
    return 1


def _regions_from_mask(preds: dict, scale, min_confidence: float,
                       max_points: int) -> list[dict]:
    """Instance regions from the model's class-mask output.

    The SegFormer endpoint returns predictions.segmentation_mask: a PNG
    (base64) of per-pixel class ids at the uploaded resolution, plus an
    optional confidence_mask of the same geometry. Extract external
    contours of the landslide class, then simplify each to a polygon.
    """
    raw = np.frombuffer(base64.b64decode(preds["segmentation_mask"]),
                        np.uint8)
    mask = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        return []
    cid = _landslide_class_id(preds.get("class_map"))
    binmask = (mask == cid).astype(np.uint8)
    contours, _ = cv2.findContours(binmask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    conf_img = None
    if preds.get("confidence_mask"):
        try:
            conf_img = cv2.imdecode(np.frombuffer(
                base64.b64decode(preds["confidence_mask"]), np.uint8),
                cv2.IMREAD_GRAYSCALE)
        except Exception:
            conf_img = None

    sx, sy = float(scale[0]), float(scale[1])
    min_area = max(100.0, 5e-4 * mask.size)   # drop specks / jitter
    out = []
    for c in contours:
        area = float(cv2.contourArea(c))
        if area < min_area:
            continue
        if conf_img is not None:
            m = np.zeros(mask.shape, np.uint8)
            cv2.drawContours(m, [c], -1, 255, -1)
            conf = float(cv2.mean(conf_img, m)[0]) / 255.0
        else:
            conf = 1.0
        if conf < min_confidence:
            continue
        poly = c.reshape(-1, 2).astype(np.float64) * [sx, sy]
        poly = _simplify(poly, max_points)
        out.append({"polygon": poly.tolist(),
                    "confidence": round(conf, 3),
                    "class": "landslide",
                    "area_px": round(area * sx * sy, 1)})
    out.sort(key=lambda r: -r["area_px"])
    return out


def regions_from_response(resp, scale=(1.0, 1.0), min_confidence=MIN_CONFIDENCE,
                          max_points=MAX_POLY_POINTS) -> list[dict]:
    """Parse a Roboflow segmentation response into usable polygons.

    Handles both output styles the platform uses: class-mask PNGs
    (SegFormer — decoded to external contours) and polygon-point lists.
    Keeps regions above `min_confidence`, simplifies each boundary, scales
    points from the uploaded image back to the source frame, and returns
    them largest-area first:
        [{"polygon": [[x, y], ...], "confidence": 0.87, "class": "landslide"}]
    """
    if not isinstance(resp, dict):
        return []
    preds = resp.get("predictions")
    if isinstance(preds, dict) and "segmentation_mask" in preds:
        return _regions_from_mask(preds, scale, min_confidence, max_points)
    if not isinstance(preds, list):
        return []
    out = []
    sx, sy = float(scale[0]), float(scale[1])
    for pred in preds:
        try:
            conf = float(pred.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf < min_confidence:
            continue
        pts = pred.get("points") or []
        if len(pts) < 3:
            continue
        poly = np.array([[float(p["x"]) * sx, float(p["y"]) * sy]
                         for p in pts], np.float64)
        x, y = poly[:, 0], poly[:, 1]
        area = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
        if area < 100:            # specks: artefacts, not regions
            continue
        poly = _simplify(poly, max_points)
        out.append({"polygon": poly.tolist(),
                    "confidence": round(conf, 3),
                    "class": pred.get("class", "landslide"),
                    "area_px": round(area, 1)})
    out.sort(key=lambda r: -r["area_px"])
    return out


def detect_landslide(image_path, api_key: str | None = None,
                     model_id: str = MODEL_ID, log=print) -> list[dict]:
    """Run the hosted landslide segmentation on one local image.

    Returns regions_from_response() output in the source image's pixel
    frame. Raises RuntimeError with a readable message on any transport,
    auth, or decode failure.
    """
    import requests

    api_key = api_key or load_api_key()
    if not api_key:
        raise RuntimeError(
            "no Roboflow API key — set ROBOFLOW_API_KEY or put it in .env")

    path = Path(image_path)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot decode image {path.name}")
    sent = img
    scale = (1.0, 1.0)
    if max(img.shape[:2]) > MAX_UPLOAD_SIDE:
        s = MAX_UPLOAD_SIDE / max(img.shape[:2])
        sent = cv2.resize(img, (round(img.shape[1] * s), round(img.shape[0] * s)),
                          interpolation=cv2.INTER_AREA)
        scale = (img.shape[1] / sent.shape[1], img.shape[0] / sent.shape[0])
    ok, buf = cv2.imencode(".jpg", sent, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("JPEG re-encode failed")

    url = f"{API_URL}/{model_id}"
    log(f"[segment] querying {model_id} with {path.name} "
        f"({sent.shape[1]}x{sent.shape[0]} px)…")
    # documented raw-REST format: the bare base64 string as a urlencoded
    # body (JSON-wrapping it is rejected with "malformed base64")
    r = requests.post(url, params={"api_key": api_key},
                      data=base64.b64encode(buf).decode(),
                      headers={"Content-Type":
                               "application/x-www-form-urlencoded"},
                      timeout=TIMEOUT_S)
    if r.status_code in (401, 403):
        raise RuntimeError("Roboflow rejected the API key")
    if r.status_code == 404:
        raise RuntimeError(f"model {model_id} not found on Roboflow")
    if r.status_code == 429:
        raise RuntimeError("Roboflow rate limit hit — retry in a moment")
    if not r.ok:
        raise RuntimeError(f"Roboflow error {r.status_code}: "
                           f"{r.text[:200]}")
    try:
        resp = r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON response from Roboflow: {r.text[:200]}")

    regions = regions_from_response(resp, scale=scale)
    log(f"[segment] {len(regions)} region(s) detected"
        + (f", best confidence {regions[0]['confidence']}"
           if regions else ""))
    return regions

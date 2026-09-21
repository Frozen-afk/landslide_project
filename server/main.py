"""FastAPI app entrypoint: `uvicorn server.main:app`.

Thin by design (T3.1 split) — job/state bookkeeping lives in `server/jobs.py`,
process-isolated heavy-stage execution in `server/executor.py`/`worker.py`,
and all HTTP routes in `server/routes.py`.
"""
from __future__ import annotations

import os

# bound glibc per-thread arenas before cv2/pycolmap load (see landslide/sfm.py)
os.environ.setdefault("MALLOC_ARENA_MAX", "4")

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.jobs import load_persisted_jobs
from server.routes import router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_persisted_jobs()
    yield


app = FastAPI(title="Landslide Volume from Phone Photos", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    """The page and its JS/CSS must never be served from browser cache —
    a stale app.js against a fresh index.html (or vice versa) breaks the
    upload wiring with no visible error."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

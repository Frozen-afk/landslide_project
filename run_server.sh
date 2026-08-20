#!/usr/bin/env bash
# Start the landslide volume web app at http://localhost:8000
cd "$(dirname "$0")"
source .venv/bin/activate
# keep glibc from spawning a malloc arena per core inside pycolmap's threads
export MALLOC_ARENA_MAX=4
exec python -m uvicorn server.main:app --host 0.0.0.0 --port 8000

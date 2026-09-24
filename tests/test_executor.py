"""Regression coverage for `server/executor.py`'s worker memory sizing (B4).

`_worker_init` must cap BLAS/OMP thread count before any worker function's
lazy heavy imports (numpy/cv2/scipy/pycolmap), so import-time virtual memory
stays small on many-core hosts and fits under `_worker_mem_limit_bytes()` on
a small-RAM host. Runs the real `_worker_init` and real imports in a real
worker process (matching production's `forkserver` start method), so it
exercises the exact ordering bug: setting the env vars after import is too
late — the BLAS thread pool is already sized.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

from server import executor

# Matches the audit's "<4 GB machine, 1 worker" figure (§4.5): small enough
# that unconstrained import-time VM (1.9-3.9 GB on a many-core host) blows
# past it before any real work starts.
_SMALL_HOST_MEM_MB = "1600"


def _import_heavy_deps_and_report_env() -> dict:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    import pycolmap  # noqa: F401
    import scipy  # noqa: F401
    return {var: os.environ.get(var) for var in
            ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS")}


def test_worker_init_caps_blas_threads_before_heavy_import():
    # Kept set for the pool's whole life: workers spawn lazily on first
    # `submit`, not at construction, so `_worker_mem_limit_bytes()` (read
    # inside the real `_worker_init`, in the child) must still see it then.
    os.environ["SLOPELENS_WORKER_MEM_MB"] = _SMALL_HOST_MEM_MB
    try:
        with ProcessPoolExecutor(max_workers=1, initializer=executor._worker_init) as pool:
            env_seen = pool.submit(_import_heavy_deps_and_report_env).result(timeout=60)
    finally:
        del os.environ["SLOPELENS_WORKER_MEM_MB"]
    assert env_seen == {
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def test_heavy_import_fits_small_host_rlimit_with_thread_cap():
    """B4 repro: on a small/many-core host, `_worker_init`'s real thread cap
    plus RLIMIT_AS, in the real order, must not crash the worker process
    importing numpy/cv2/pycolmap/scipy."""
    os.environ["SLOPELENS_WORKER_MEM_MB"] = _SMALL_HOST_MEM_MB
    try:
        with ProcessPoolExecutor(max_workers=1, initializer=executor._worker_init) as pool:
            fut = pool.submit(_import_heavy_deps_and_report_env)
            fut.result(timeout=60)  # raises BrokenProcessPool if the worker crashed
    finally:
        del os.environ["SLOPELENS_WORKER_MEM_MB"]

"""Payload cache — builds the jobs payload once and holds it in memory.

Invalidation is mtime-based: the cache is considered stale whenever
work/jobs.parquet or work/interactions.jsonl has been modified since the
last build.  A threading.Lock ensures only one build runs at a time even
under concurrent requests.

Usage::

    from app.cache import get_payload

    payload = await asyncio.to_thread(get_payload)
"""
import threading
import time
from pathlib import Path

from app.config import WORK

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_lock = threading.Lock()

_cache: dict | None = None          # last built payload
_built_at: float = 0.0              # wall-clock time of last build
_parquet_mtime: float = 0.0         # mtime of jobs.parquet at build time
_interactions_mtime: float = 0.0    # mtime of interactions.jsonl at build time

_PARQUET = WORK / "jobs.parquet"
_INTERACTIONS = WORK / "interactions.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mtime(p: Path) -> float:
    """Return the file's mtime, or 0.0 if it does not exist."""
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _is_stale() -> bool:
    """True if either source file is newer than the cached payload."""
    return (
        _cache is None
        or _mtime(_PARQUET) > _parquet_mtime
        or _mtime(_INTERACTIONS) > _interactions_mtime
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def invalidate() -> None:
    """Force the next call to get_payload() to rebuild from scratch.

    Call this after writing new data to jobs.parquet or interactions.jsonl
    so that the next page load picks up the changes immediately rather than
    waiting for an mtime bump to be detected.
    """
    global _cache
    with _lock:
        _cache = None


def get_payload(jd_chars: int = 4000) -> dict:
    """Return the cached payload, rebuilding if the source files have changed.

    This function is synchronous and CPU/IO-bound; callers on the async
    event loop must wrap it with ``asyncio.to_thread(get_payload)``.
    """
    global _cache, _built_at, _parquet_mtime, _interactions_mtime

    with _lock:
        if not _is_stale():
            return _cache  # type: ignore[return-value]

        # Snapshot mtimes *before* the build so a concurrent write that
        # lands mid-build will be detected on the next call.
        snap_pq = _mtime(_PARQUET)
        snap_ia = _mtime(_INTERACTIONS)

        from app.services.data import build_jobs_payload

        t0 = time.monotonic()
        payload = build_jobs_payload(jd_chars=jd_chars)
        elapsed = time.monotonic() - t0

        _cache = payload
        _built_at = time.time()
        _parquet_mtime = snap_pq
        _interactions_mtime = snap_ia

        n = len(payload.get("jobs", []))
        print(
            f"[cache] payload built: {n} jobs in {elapsed:.1f}s",
            flush=True,
        )
        return _cache

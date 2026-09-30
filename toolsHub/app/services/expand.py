"""Background expansion — triggered on yes-labels.

When the user labels a job as "yes", this module fetches the 12 nearest
corpus groups that are not yet in the slice, unions them into
work/jobs.parquet, then invalidates the payload cache so the next page
load picks up the new jobs.

Only one expansion runs at a time (exp_lock).  Keys that have already
been expanded are tracked in exp_done so a repeated label on the same
job does not trigger a redundant fetch.

Mirrors tools/jobs.py _expand_around() but calls the service layer
directly instead of shelling out to a subprocess.
"""
import base64
import threading

import numpy as np

from app.config import WORK

# Module-level state shared across all requests in the serve process
exp_done: set = set()
exp_lock = threading.Lock()


def expand_around(key: str) -> None:
    """Fetch nearby groups for *key* and invalidate the cache.

    Safe to call from any thread.  Returns immediately if another
    expansion is already running or *key* has already been expanded.
    """
    if not exp_lock.acquire(blocking=False):
        print(
            f"[expand] busy; skipping {key} (label again later to retry)",
            flush=True,
        )
        return
    try:
        if key in exp_done:
            return
        exp_done.add(key)

        import duckdb

        from app.cache import invalidate
        from app.services.data import manifest, nearest
        from app.services.fetch import fetch_groups  # implemented in Step 11

        pq = WORK / "jobs.parquet"
        if not pq.exists():
            print(f"[expand] {key}: jobs.parquet absent, skipped", flush=True)
            return

        row = (
            duckdb.connect()
            .execute(
                f"SELECT vec_b64 FROM read_parquet('{pq}') "
                f"WHERE ats || '/' || slug || '#' || id = ?",
                [key],
            )
            .fetchone()
        )
        if not row:
            print(f"[expand] {key}: not in slice, skipped", flush=True)
            return

        vec = np.frombuffer(base64.b64decode(row[0]), dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) + 1e-9)

        m, C = manifest()
        near = [n["id"] for n, _ in nearest(m, C, vec, 12)]

        have = {
            r[0]
            for r in duckdb.connect()
            .execute(f"SELECT DISTINCT leaf FROM read_parquet('{pq}')")
            .fetchall()
        }
        new = [i for i in near if i not in have]

        if not new:
            print(
                f"[expand] yes on {key}: all 12 nearest groups already in the slice",
                flush=True,
            )
            return

        print(
            f"[expand] yes on {key}: fetching {len(new)} new nearby groups {new} ...",
            flush=True,
        )

        added = fetch_groups(new)
        invalidate()

        print(
            f"[expand] done: {added} jobs added -> refresh the page",
            flush=True,
        )
    except Exception as exc:
        print(f"[expand] error for {key}: {exc}", flush=True)
    finally:
        if exp_lock.locked():
            exp_lock.release()


def trigger(key: str) -> None:
    """Spawn a daemon thread to run expand_around(key) without blocking the caller."""
    threading.Thread(target=expand_around, args=(key,), daemon=True).start()

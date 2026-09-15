"""Core data access — manifest, ideal JD, job slice loading, deduplication.

Part 1: foundational functions that every other service depends on.
Part 2 (same file): subgroups and build_jobs_payload — added in Step 7.
"""
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from app.config import WORK
from app.services.models import get

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def nice_company(c: str | None) -> str:
    """Return an empty string for UUID-style company slugs, otherwise the name."""
    return "" if re.fullmatch(r"[0-9a-fA-F-]{20,}", c or "") else (c or "")


def node_label(n: dict) -> str:
    """Human-readable label for a tree node: topic + up to 4 exemplar titles."""
    ex = "; ".join(
        f"{e['title']}" + (f" @ {nice_company(e['company'])}" if nice_company(e["company"]) else "")
        for e in n["exemplars"][:4]
    )
    return f"{n['label']}  [{ex}]"


# ---------------------------------------------------------------------------
# Manifest and centroids (cached 6 hours)
# ---------------------------------------------------------------------------

_SIX_HOURS = 6 * 3600


def manifest() -> tuple[dict, np.ndarray]:
    """Return (manifest_dict, centroids_float32[nodes x dims]).

    Downloads and caches work/manifest.json and work/centroids.bin;
    refreshes if either is absent or older than 6 hours.
    """
    mp = WORK / "manifest.json"
    cp = WORK / "centroids.bin"
    stale = (
        not mp.exists()
        or not cp.exists()
        or time.time() - mp.stat().st_mtime > _SIX_HOURS
    )
    if stale:
        m = get("/data/manifest.json")
        mp.write_text(json.dumps(m), encoding="utf-8")
        cp.write_bytes(get("/data/centroids.bin", binary=True))
    m = json.loads(mp.read_text(encoding="utf-8"))
    C = np.fromfile(cp, dtype=np.float16).astype(np.float32).reshape(-1, m["dims"])
    return m, C


# ---------------------------------------------------------------------------
# Ideal JD
# ---------------------------------------------------------------------------

def ideal() -> tuple[dict, np.ndarray]:
    """Return (ideal_dict, unit_vector).

    Raises FileNotFoundError if work/ideal.json is absent (run embed first).
    """
    p = WORK / "ideal.json"
    if not p.exists():
        raise FileNotFoundError(
            "work/ideal.json not found — run `embed --file work/ideal-jd.md` first"
        )
    d = json.loads(p.read_text(encoding="utf-8"))
    v = np.asarray(d["vector"], dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-9
    return d, v


# ---------------------------------------------------------------------------
# Nearest-neighbour search over leaves
# ---------------------------------------------------------------------------

def nearest(m: dict, C: np.ndarray, v: np.ndarray, k: int, min_sim: float = 0.0) -> list:
    """Rank every leaf node by cosine similarity to *v*.

    All centroids are loaded locally so we score all of them — ball bounds
    are too loose in 1536-d to prune. Returns [(node, sim)] best first.
    """
    T = m["tree"]
    sims = C @ v
    leaves = [n for n in T if not n["children"]]
    leaves.sort(key=lambda n: -sims[n["id"]])
    return [(n, float(sims[n["id"]])) for n in leaves if sims[n["id"]] >= min_sim][:k]


def leaves_under(m: dict, n: dict) -> list:
    """Return all leaf descendants of tree node *n* (depth-first)."""
    T = m["tree"]
    out: list = []
    stack = [n]
    while stack:
        x = stack.pop()
        if not x["children"]:
            out.append(x)
        else:
            stack.extend(T[c] for c in x["children"])
    return out


# ---------------------------------------------------------------------------
# Job slice
# ---------------------------------------------------------------------------

def dedup_rows(rows: list) -> list:
    """Collapse identical (company, title) postings.

    Rows are expected to arrive sorted by sim DESC so the highest-similarity
    copy of each (company, title) pair is kept. Mirrors tools/jobs.py exactly.
    """
    def _n(x):
        return re.sub(r"\W+", " ", (x or "").lower()).strip()

    seen: set = set()
    uniq: list = []
    for r in rows:
        key = (_n(r[4]), _n(r[3]))  # (company, title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    dropped = len(rows) - len(uniq)
    if dropped:
        print(f"deduped {dropped} repeated (company, title) postings", file=sys.stderr)
    return uniq


def load_jobs() -> list:
    """Load work/jobs.parquet ordered by sim DESC, deduped by (company, title).

    Raises FileNotFoundError if the parquet file is absent (run fetch first).
    """
    import duckdb

    p = WORK / "jobs.parquet"
    if not p.exists():
        raise FileNotFoundError(
            "work/jobs.parquet not found — run `fetch` first"
        )
    rows = (
        duckdb.connect()
        .execute(f"SELECT * FROM read_parquet('{p}') ORDER BY sim DESC")
        .fetchall()
    )
    return dedup_rows(rows)

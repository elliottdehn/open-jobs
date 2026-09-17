"""Fetch command — download job groups and write work/jobs.parquet.

Exposes two entry points:

* run(groups, top, replace)  — full CLI equivalent of tools/jobs.py cmd_fetch()
* fetch_groups(ids)          — thin wrapper used by expand.py to union new
                               leaf groups into an existing slice without
                               touching the ideal vector or manifest cache.

Both raise on hard errors instead of calling sys.exit().
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

from app.config import WORK
from app.services.data import ideal, leaves_under, manifest, nearest
from app.services.labels import (
    LABEL_STORE,
    capture_labelled,
    load_interaction_labels,
    store_row,
)
from app.services.models import get


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _download_leaf(leaf: dict, gdir: Path, groups_prefix: str) -> list:
    """Return the cached group JSON for *leaf*, downloading if absent."""
    p = gdir / f"{leaf['id']}.json"
    if not p.exists():
        p.write_bytes(
            get(f"/data/{groups_prefix}{leaf['id']}.json", binary=True)
        )
    return json.loads(p.read_text(encoding="utf-8"))


def _build_rows(leaves: list, v: np.ndarray, gdir: Path, groups_prefix: str) -> list:
    """Download and assemble parquet rows for *leaves*, ranked by sim to *v*."""
    import base64

    rows = []
    total = 0
    for li, leaf in enumerate(leaves):
        g = _download_leaf(leaf, gdir, groups_prefix)
        for j in g["jobs"]:
            vec = np.frombuffer(
                base64.b64decode(j["v"]), dtype=np.float32
            )
            rows.append((
                j["ats"], j["slug"], j["id"],
                j["title"], j["company"], j["location"],
                j["url"], j.get("seen") or 0, j.get("jd") or "",
                leaf["id"], float(vec @ v), j["v"],
                j.get("pub") or None,
            ))
        total += len(g["jobs"])
        print(
            f"\r{li + 1}/{len(leaves)} groups, {total:,} jobs",
            end="", file=sys.stderr,
        )
    print(file=sys.stderr)
    return rows


def _write_parquet(rows: list) -> None:
    """Write *rows* to work/jobs.parquet (and work/jobs.duckdb)."""
    import duckdb

    con = duckdb.connect(str(WORK / "jobs.duckdb"))
    con.execute(
        "CREATE OR REPLACE TABLE jobs ("
        "ats VARCHAR, slug VARCHAR, id VARCHAR, title VARCHAR, "
        "company VARCHAR, location VARCHAR, url VARCHAR, "
        "seen_ms BIGINT, jd VARCHAR, leaf INTEGER, sim DOUBLE, "
        "vec_b64 VARCHAR, pub_ms BIGINT)"
    )
    con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    pq = WORK / "jobs.parquet"
    con.execute(f"COPY (SELECT * FROM jobs) TO '{pq}' (FORMAT PARQUET)")


def _inject_labelled(rows: list, v: np.ndarray) -> tuple[list, int]:
    """Persist labelled jobs and re-inject any that fell outside the slice."""
    labels = load_interaction_labels()
    store = capture_labelled(rows, labels)
    have = {f"{r[0]}/{r[1]}#{r[2]}" for r in rows}
    inj = 0
    for key, rec in store.items():
        if key in have or not rec.get("vec_b64"):
            continue
        rows.append(store_row(rec, v))
        inj += 1
    if inj:
        print(
            f"re-injected {inj} labelled job(s) that fell outside the new slice "
            f"(from work/{LABEL_STORE})",
            file=sys.stderr,
        )
    return rows, inj


def _union_previous(rows: list, replace: bool) -> list:
    """Union *rows* with the existing jobs.parquet unless *replace* is True."""
    import duckdb

    pq = WORK / "jobs.parquet"
    if replace or not pq.exists():
        return rows
    have = {f"{r[0]}/{r[1]}#{r[2]}" for r in rows}
    prev = duckdb.connect().execute(
        f"SELECT * FROM read_parquet('{pq}')"
    ).fetchall()
    # Older slices predate the pub_ms column — pad to 13 columns
    prev = [tuple(r) + (None,) * (13 - len(r)) for r in prev]
    kept = [r for r in prev if f"{r[0]}/{r[1]}#{r[2]}" not in have]
    print(
        f"unioned with the previous slice: kept {len(kept):,} prior job(s) "
        f"not in this fetch (total {len(rows) + len(kept):,}). "
        f"Pass --replace to start fresh.",
        file=sys.stderr,
    )
    return rows + kept


def _report_dropouts(rows: list, top: int) -> None:
    """Warn if previously top-ranked jobs are missing from the new slice."""
    new_keys = {f"{r[0]}/{r[1]}#{r[2]}" for r in rows}
    for rf in ("llm-ranked.csv", "ranked.csv"):
        rp = WORK / rf
        if not rp.exists():
            continue
        prev = []
        with rp.open(encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                if i >= 50:
                    break
                if row.get("key"):
                    prev.append((row["key"], row.get("title") or "", row.get("url") or ""))
        dropped = [p for p in prev if p[0] not in new_keys]
        if dropped:
            print(
                f"\n! {len(dropped)} of your previous top {len(prev)} ({rf}) are NOT "
                f"in this slice after the rebuild (moved into groups below --top {top}):",
                file=sys.stderr,
            )
            for k, t, u in dropped[:10]:
                print(f"    {(t or k)[:64]}  {u}", file=sys.stderr)
            print(
                "  probe <url> shows each one's new group; "
                "or re-fetch with a higher --top.",
                file=sys.stderr,
            )
        break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_groups(ids: list[int]) -> int:
    """Union the given leaf/node *ids* into the existing jobs.parquet.

    Used by expand.py after a yes-label.  Returns the number of net-new
    jobs added to the slice.
    """
    m, C = manifest()
    d, v = ideal()
    T = m["tree"]
    gdir = WORK / "groups"
    gdir.mkdir(parents=True, exist_ok=True)
    groups_prefix = m.get("groups", "groups/")

    leaves = []
    for i in ids:
        leaves.extend(leaves_under(m, T[i]))

    rows = _build_rows(leaves, v, gdir, groups_prefix)
    rows, _ = _inject_labelled(rows, v)
    rows = _union_previous(rows, replace=False)
    before = len(rows)
    _write_parquet(rows)
    return len(rows) - before  # net new (may be negative if dedup dropped some)


def run(
    groups: str | None = None,
    top: int = 30,
    replace: bool = False,
) -> None:
    """Download job groups and write work/jobs.parquet.

    Parameters
    ----------
    groups:
        Comma-separated node/leaf IDs to fetch, e.g. ``"12,45,301"``.
        If None, the *top* nearest groups to the ideal vector are used.
    top:
        Number of nearest groups to fetch when *groups* is None.
    replace:
        If True, overwrite jobs.parquet instead of unioning with it.

    Raises
    ------
    FileNotFoundError
        If work/ideal.json or work/manifest.json are absent.
    """
    m, C = manifest()
    d, v = ideal()
    T = m["tree"]
    gdir = WORK / "groups"
    gdir.mkdir(parents=True, exist_ok=True)
    groups_prefix = m.get("groups", "groups/")

    if groups:
        ids = [int(x) for x in groups.split(",")]
    else:
        ids = [n["id"] for n, _ in nearest(m, C, v, top)]

    leaves = []
    for i in ids:
        leaves.extend(leaves_under(m, T[i]))

    rows = _build_rows(leaves, v, gdir, groups_prefix)
    rows, _ = _inject_labelled(rows, v)
    rows = _union_previous(rows, replace)
    _write_parquet(rows)

    pq = WORK / "jobs.parquet"
    print(
        f"wrote {pq} and {WORK}/jobs.duckdb: {len(rows):,} jobs from "
        f"{len(leaves)} groups. Columns: ats, slug, id, title, company, "
        f"location, url, seen_ms, jd, leaf, sim (cosine to ideal JD), "
        f"vec_b64 (float32 LE base64)."
    )
    _report_dropouts(rows, top)

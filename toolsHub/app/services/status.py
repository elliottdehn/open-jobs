"""Status and SQL commands.

status: print a checklist of work/ files with size and mtime.
sql:    run a DuckDB query over the downloaded export.

Both mirror tools/jobs.py cmd_status() / cmd_sql() exactly.
"""
import glob
import json
import sys
import time
from collections import Counter

from app.config import WORK


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

_STATUS_FILES = (
    "ideal-jd.md",
    "ideal.json",
    "groups.json",
    "jobs.parquet",
    "search.html",
    "enrichment.json",
    "interactions.jsonl",
    "model.json",
    "ranked.csv",
)


def status() -> None:
    """Print a checklist of work/ data files with size and modification time."""
    for name in _STATUS_FILES:
        p = WORK / name
        if p.exists():
            mb = p.stat().st_size / 1e6
            hm = time.strftime("%H:%M", time.localtime(p.stat().st_mtime))
            print(f"✓ {name}  ({mb:.1f} MB, {hm})")
        else:
            print(f"· {name}")

    ip = WORK / "interactions.jsonl"
    if ip.exists():
        events = []
        for line in ip.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
        counts = dict(Counter(e.get("type") for e in events))
        yes = sum(1 for e in events if e.get("type") == "label" and e.get("value") == 1)
        no  = sum(1 for e in events if e.get("type") == "label" and e.get("value") == 0)
        print(f"interactions: {counts} | yes: {yes} no: {no}")


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

def sql(query: str | None = None, export_dir: str | None = None, limit: int = 0) -> None:
    """Run *query* over the downloaded export.

    Creates DuckDB views ``jobs`` and ``boards`` over
    ``work/export/<newest date>/jobs/*.parquet`` and
    ``work/export/<newest date>/boards/*.parquet``.

    Parameters
    ----------
    query:
        SQL string.  Reads from stdin when omitted.
    export_dir:
        Explicit export directory.  Defaults to the newest
        ``work/export/20*/`` directory.
    limit:
        Row limit passed to ``.limit()``; 0 means no limit.

    Raises
    ------
    RuntimeError
        If no export is found or the query is empty.
    """
    import duckdb

    root = export_dir or (
        sorted(glob.glob(str(WORK / "export" / "20*"))) or [None]
    )[-1]

    jobs_glob = f"{root}/jobs/*.parquet" if root else ""
    if not root or not glob.glob(jobs_glob):
        raise RuntimeError("no export in work/export/ — run `export` first")

    if not query:
        if sys.stdin.isatty():
            raise RuntimeError('usage: sql "SELECT ..."  (or pipe a query on stdin)')
        query = sys.stdin.read()

    if not query.strip():
        raise RuntimeError('usage: sql "SELECT ..."  (or pipe a query on stdin)')

    con = duckdb.connect()
    for view in ("jobs", "boards"):
        con.execute(
            f"CREATE VIEW {view} AS "
            f"SELECT * FROM read_parquet('{root}/{view}/*.parquet')"
        )

    result = con.sql(query)
    print(result.limit(limit) if limit else result)

"""Export command — download the full daily export (jobs + boards parquet per ATS).

Mirrors tools/jobs.py cmd_export() exactly.
Raises instead of sys.exit().
"""
import time
import urllib.request
from pathlib import Path

from app.config import DATA, UA, WORK
from app.services.models import get


def run(
    date: str | None = None,
    only: str | None = None,
    out: str | None = None,
) -> None:
    """Download the full export for *date* into *out*.

    Parameters
    ----------
    date:
        Export date in YYYY-MM-DD format.  Defaults to the most recent
        published export (resolved via the /data/exports/ index).
    only:
        Comma-separated ATS names to restrict the download, e.g.
        ``"greenhouse,lever"``.  Downloads all ATSes when omitted.
    out:
        Destination directory.  Defaults to work/export/<date>/.

    Raises
    ------
    RuntimeError
        If no export is published, a download fails after 5 retries, or
        a file is shorter than expected.
    """
    # Resolve the target date
    if not date:
        try:
            date = get("/data/diffs/index.json").get("head")
        except Exception:
            date = None
        dates = get("/data/exports/")["dirs"]
        if not dates:
            raise RuntimeError("no export published")
        if not date or f"{date}/" not in dates:
            date = sorted(dates)[-1].rstrip("/")

    dest_root = Path(out) if out else WORK / "export" / date
    only_set = {x.strip() for x in only.split(",")} if only else None

    # Build download plan
    plan: list[tuple[str, Path, int]] = []
    for sub in ("jobs", "boards"):
        for f in get(f"/data/exports/{date}/{sub}/")["files"]:
            if only_set and f["file"].rsplit(".", 1)[0] not in only_set:
                continue
            plan.append((
                f"/data/exports/{date}/{sub}/{f['file']}",
                dest_root / sub / f["file"],
                f["bytes"],
            ))

    total = sum(b for _, _, b in plan)
    print(f"export {date}: {len(plan)} files, {total / 1e9:.1f} GB -> {dest_root}")

    done = 0
    t0 = time.time()
    for path, dest, size in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = dest.stat().st_size if dest.exists() else 0
        if have == size:
            done += size
            continue
        if have > size:
            dest.unlink()
            have = 0

        for attempt in range(5):
            try:
                headers = {**UA}
                if have:
                    headers["range"] = f"bytes={have}-"
                req = urllib.request.Request(f"{DATA}{path}", headers=headers)
                with urllib.request.urlopen(req, timeout=300) as resp, \
                        dest.open("ab" if have else "wb") as w:
                    while True:
                        chunk = resp.read(8 << 20)
                        if not chunk:
                            break
                        w.write(chunk)
                        have += len(chunk)
                        done += len(chunk)
                        elapsed = time.time() - t0
                        print(
                            f"\r  {dest.relative_to(dest_root)}"
                            f"  {done / 1e9:.2f}/{total / 1e9:.1f} GB"
                            f"  {done / elapsed / 1e6:.0f} MB/s",
                            end="", flush=True,
                        )
                break
            except Exception as exc:
                if attempt == 4:
                    raise RuntimeError(f"\nfailed: {path}: {exc}") from exc
                time.sleep(2 * (attempt + 1))

        if have != size:
            raise RuntimeError(
                f"\nshort file: {dest} ({have} of {size} bytes)"
            )

    import duckdb

    n_jobs, n_boards = (
        duckdb.sql(
            f"SELECT count(*) FROM '{dest_root / sub / '*.parquet'}'"
        ).fetchone()[0]
        for sub in ("jobs", "boards")
    )
    elapsed = time.time() - t0
    print(
        f"\n\n  {n_jobs:,} postings from {n_boards:,} career sites, "
        f"{len(plan)} files, {total / 1e9:.1f} GB, {elapsed:.0f}s\n"
        f"  -> {dest_root}\n"
        f'  query it:  uv run toolsHub/main.py sql "SELECT title, company, location FROM jobs LIMIT 20"'
    )

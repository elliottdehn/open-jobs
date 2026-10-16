"""toolsHub — FastAPI reimplementation of the open-jobs tools workflow.

Usage:
  uv run toolsHub/main.py serve   [--port 8766] [--no-open]
  uv run toolsHub/main.py embed   --file work/ideal-jd.md --title "..." [--location "..."]
  uv run toolsHub/main.py groups  [--k 30] [--min-sim 0]
  uv run toolsHub/main.py fetch   --groups 12,45,301 | --top N [--replace]
  uv run toolsHub/main.py html    [--out work/search.html]
  uv run toolsHub/main.py enrich  [--top N | --all]
  uv run toolsHub/main.py rank    [--labels work/interactions.jsonl]
  uv run toolsHub/main.py top     [--n 50] [--notes work/top-notes.json]
  uv run toolsHub/main.py probe   <url>
  uv run toolsHub/main.py status
  uv run toolsHub/main.py export  [--date YYYY-MM-DD] [--only ats1,ats2] [--out DIR]
  uv run toolsHub/main.py sql     "SELECT ..."
"""
import argparse
import json
import sys
import time


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_serve(a):
    import uvicorn
    from app.server import app  # wired in Step 26
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")


def _cmd_embed(a):
    from app.services.embed import run
    run(file=a.file, title=a.title or "", location=a.location or "")


def _cmd_groups(a):
    from app.config import WORK
    from app.services.data import ideal, manifest, nearest, node_label

    m, C = manifest()
    d, v = ideal()
    if d["recipe"] != m["recipe"]:
        print(
            f"warning: ideal.json recipe {d['recipe']} != manifest {m['recipe']}; "
            "re-run embed",
            file=sys.stderr,
        )
    rows = nearest(m, C, v, a.k, a.min_sim)
    built = time.strftime("%Y-%m-%d", time.localtime(m["built_at"] / 1000))
    print(
        f"{m['jobs']:,} jobs in {m['leaves']:,} groups (built {built}). "
        f"Nearest to your ideal JD:\n"
    )
    print(f"{'id':>6} {'sim':>5} {'jobs':>6} {'titles':>6}  label  [exemplars]")
    for n, s in rows:
        print(
            f"{n['id']:>6} {s:5.2f} {n['size']:>6} "
            f"{n.get('distinct_titles', ''):>6}  {node_label(n)[:150]}"
        )
    out = WORK / "groups.json"
    out.write_text(
        json.dumps(
            [
                {
                    "id": n["id"], "sim": s, "size": n["size"],
                    "label": n["label"], "exemplars": n["exemplars"],
                }
                for n, s in rows
            ],
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"\nwrote {out}. "
        "Next: `fetch --groups <ids>` (maybe) — or `fetch --top N` for the N nearest."
    )


def _cmd_fetch(a):
    from app.services.fetch import run
    run(groups=a.groups, top=a.top, replace=a.replace)


def _cmd_html(a):
    from app.services.html import run
    run(out=a.out, jd_chars=a.jd_chars)


def _cmd_enrich(a):
    import json as _json

    from app.config import WORK
    from app.services.data import load_jobs
    from app.services.models import post

    rows = load_jobs()
    keys = [(r[0], r[1], r[2]) for r in rows]
    if not a.all:
        keys = keys[: a.top]

    p = WORK / "enrichment.json"
    store = (
        _json.loads(p.read_text(encoding="utf-8"))
        if p.exists()
        else {"jobs": {}, "boards": {}}
    )
    todo = [k for k in keys if f"{k[0]}/{k[1]}#{k[2]}" not in store["jobs"]]
    print(
        f"{len(keys)} jobs selected, {len(todo)} not yet enriched locally; "
        "sending in batches of 100"
    )
    spent = 0.0
    for i in range(0, len(todo), 100):
        batch = todo[i : i + 100]
        code, res = post(
            "/enrich",
            {"jobs": [{"ats": k[0], "slug": k[1], "id": k[2]} for k in batch]},
        )
        for name, b in (res.get("boards") or {}).items():
            store["boards"][name] = b
        for key, j in (res.get("jobs") or {}).items():
            if j.get("status") == "done":
                store["jobs"][key] = j["enrichment"]
        p.write_text(_json.dumps(store), encoding="utf-8")
        if code == 429:
            s = res.get("spent", {})
            print(
                f"rate limited: spent ${s.get('hourUsd', 0):.2f}/hr "
                f"${s.get('dayUsd', 0):.2f}/day; "
                f"retry in {res.get('retryAfterSeconds')}s. Saved what came back."
            )
            break
        if code != 200:
            print(f"batch failed: HTTP {code} {str(res)[:200]}")
            break
        c = res.get("cost", {})
        spent += c.get("thisCallUsd", 0)
        print(
            f"  {min(i + 100, len(todo))}/{len(todo)}: "
            f"${c.get('thisCallUsd', 0):.3f} this call, "
            f"${c.get('hourUsd', 0):.2f}/${c.get('hourLimit')} this hour, "
            f"${c.get('dayUsd', 0):.2f}/${c.get('dayLimit')} today",
            flush=True,
        )
    print(
        f"wrote {p}: {len(store['jobs'])} jobs, {len(store['boards'])} boards "
        f"enriched (this run ≈ ${spent:.2f}). Re-run `html` to use it."
    )


def _cmd_rank(a):
    from app.services.rank import run
    run(labels_path=a.labels)


def _cmd_top(a):
    from app.services.top import run
    run(n=a.n, notes_path=a.notes, freshness=a.freshness, out=a.out)


def _cmd_probe(a):
    from app.services.probe import run
    run(url=a.url, board=a.board)


def _cmd_status(_a):
    from app.services.status import status
    status()


def _cmd_export(a):
    from app.services.export import run
    run(date=a.date, only=a.only, out=a.out)


def _cmd_sql(a):
    from app.services.status import sql
    sql(query=a.query, export_dir=a.export, limit=a.limit)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    from app.config import WORK

    ap = argparse.ArgumentParser(prog="toolsHub")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # serve
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8766)
    s.add_argument("--no-open", action="store_true")

    # embed
    s = sub.add_parser("embed")
    s.add_argument("--file", required=True)
    s.add_argument("--title")
    s.add_argument("--location")

    # groups
    s = sub.add_parser("groups")
    s.add_argument("--k", type=int, default=30)
    s.add_argument("--min-sim", type=float, default=0.0, dest="min_sim")

    # fetch
    s = sub.add_parser("fetch")
    s.add_argument("--groups")
    s.add_argument("--top", type=int, default=12)
    s.add_argument(
        "--replace", action="store_true",
        help="overwrite the previous slice instead of unioning with it",
    )

    # html
    s = sub.add_parser("html")
    s.add_argument("--out")
    s.add_argument("--jd-chars", type=int, default=4000, dest="jd_chars")

    # enrich
    s = sub.add_parser("enrich")
    s.add_argument("--top", type=int, default=300)
    s.add_argument("--all", action="store_true")

    # rank
    s = sub.add_parser("rank")
    s.add_argument("--labels", default=str(WORK / "interactions.jsonl"))

    # top
    s = sub.add_parser(
        "top",
        help="static shortlist: top N eligible fresh matches by cosine",
    )
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--out")
    s.add_argument("--notes", help="JSON {key: why it fits}")
    s.add_argument(
        "--freshness", default="fresh",
        help="comma list of verdicts to keep: fresh,stale,re-stamped,ghost,unknown",
    )

    # probe
    s = sub.add_parser("probe", help="why isn't this posting in my list?")
    s.add_argument("url")
    s.add_argument(
        "--board",
        help="ats/slug when the URL doesn't name the board (workable, paylocity)",
    )

    # status
    sub.add_parser("status")

    # export
    s = sub.add_parser(
        "export",
        help="download today's full export (jobs + boards parquet per ATS), resumable",
    )
    s.add_argument("--date")
    s.add_argument("--only", help="comma-separated ATS names")
    s.add_argument("--out")

    # sql
    s = sub.add_parser(
        "sql",
        help="run SQL over the downloaded export: views `jobs` and `boards`",
    )
    s.add_argument("query", nargs="?")
    s.add_argument("--export", help="export dir (default: newest under work/export/)")
    s.add_argument("--limit", type=int, default=0)

    return ap


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_COMMANDS = {
    "serve":  _cmd_serve,
    "embed":  _cmd_embed,
    "groups": _cmd_groups,
    "fetch":  _cmd_fetch,
    "html":   _cmd_html,
    "enrich": _cmd_enrich,
    "rank":   _cmd_rank,
    "top":    _cmd_top,
    "probe":  _cmd_probe,
    "status": _cmd_status,
    "export": _cmd_export,
    "sql":    _cmd_sql,
}


def main():
    ap = _build_parser()
    args = ap.parse_args()
    try:
        _COMMANDS[args.cmd](args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

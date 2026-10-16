"""Probe command — explain why a URL is (or isn't) in the search results.

Mirrors tools/jobs.py cmd_probe() exactly.
Raises RuntimeError on hard backend errors instead of sys.exit().
"""
import datetime
import glob
import json
import re
import time
import urllib.parse

import numpy as np

from app.config import WORK
from app.services.data import ideal, manifest, nearest
from app.services.models import get


def run(url: str, board: str | None = None) -> None:
    """Print a diagnostic report for *url*.

    Parameters
    ----------
    url:
        Job posting URL to investigate.
    board:
        Optional ``ats/slug`` hint (skips the local parquet look-up).

    Raises
    ------
    RuntimeError
        If the backend /probe call returns an error field.
    """
    q: dict = {"url": url}
    if board:
        q["board"] = board

    # If the posting is already in the slice we know its board and canonical URL
    # (covers embedded boards like ?gh_jid=)
    jp = WORK / "jobs.parquet"
    if not board and jp.exists():
        import duckdb

        u = url.strip().rstrip("/")
        gh = re.search(r"gh_jid=(\d+)", u)
        gh_id = gh.group(1) if gh else ""
        row = (
            duckdb.connect()
            .execute(
                f"SELECT ats, slug, id, url FROM read_parquet('{jp}') "
                f"WHERE rtrim(url, '/') = ? "
                f"OR (? <> '' AND ats = 'greenhouse' AND id = ?) LIMIT 1",
                [u, gh_id, gh_id],
            )
            .fetchone()
        )
        if row:
            q["board"] = f"{row[0]}/{row[1]}"
            q["url"] = row[3]
            print(f"(known from work/jobs.parquet: {row[0]}/{row[1]} id {row[2]})")

    d = get("/probe?" + urllib.parse.urlencode(q))
    if d.get("error"):
        raise RuntimeError(f"probe failed: {d['error']}")

    r = d["resolved"]
    brd = d.get("board")
    job = d.get("job")
    now = time.time() * 1000

    def ago(ms):
        return f"{(now - ms) / 3600000:.1f}h ago" if ms else "never"

    def when(ms):
        return (
            datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
            if ms
            else "?"
        )

    print(f"url: {url}")

    if not r.get("ats"):
        raise RuntimeError(
            f"  ✗ {r.get('hint', 'unrecognized URL')} (not one of the 25 crawled ATSes)"
        )
    if not r.get("slug"):
        raise RuntimeError(
            f"  ✗ board unresolved: {r.get('hint')}\n"
            "    (re-run with --board <ats>/<slug> once you know it; "
            "boards are listed in backend/slugs.json)"
        )

    print(
        f"board: {r['ats']}/{r['slug']}"
        + (f"  (job id {r['id']})" if r.get("id") else "")
    )

    if not d.get("crawled"):
        raise RuntimeError(
            "  ✗ this board is not in slugs.json, so it has never been crawled. "
            "Add it (PR to slugs.json at the repo root, under `ats`) and it "
            "joins the daily fetch."
        )
    if not brd:
        raise RuntimeError(
            "  ✗ board is in slugs.json but has never completed a fetch "
            "(new or unreachable). It will appear after its first successful "
            "daily fetch."
        )

    err_suffix = (
        f" ({brd['lastError'][:80]})" if brd.get("lastError") else ""
    )
    print(
        f"  crawled: yes · last successful fetch {when(brd['lastOkAt'])}"
        f" ({ago(brd['lastOkAt'])}) · {brd['jobCount']} open jobs"
        f" · status {brd['lastStatus']}{err_suffix}"
        f" · next fetch {when(brd['nextFetchAt'])}"
    )

    m, C = manifest()
    built = m.get("built_at")
    T = m["tree"]

    if not job or not job.get("found"):
        print(
            f"  ✗ job NOT seen by the crawler as of {ago(brd['lastOkAt'])}.\n"
            "    → newer than this board's last crawl (it will be picked up at "
            "the next fetch), or the posting is not on the board's public listing API."
        )
        return

    st = job["status"]
    removed_note = (
        f" (removed {when(job['removedAt'])})" if st == "removed" else ""
    )
    print(
        f"job: {job['title']!r} · {job.get('location') or '?'} · {st}{removed_note}"
        f" · first seen {when(job['firstSeenAt'])} ({ago(job['firstSeenAt'])})"
        f" · embed {job['embedStatus']} · detail {job['detailStatus']}"
    )

    if st == "removed":
        print("  ✗ the board no longer lists it; removed jobs are not in the public snapshot.")
        return

    if job["firstSeenAt"] > (built or 0):
        print(
            f"  ✗ NEWER than the public snapshot (built {when(built)}). "
            "It will be in the next daily build; nothing you do locally can "
            "surface it before then."
        )
    elif job["embedStatus"] != "done":
        print(
            f"  ✗ not embedded yet (embed {job['embedStatus']}), so it was skipped "
            f"at build time ({when(built)}). Usually resolves in the next build."
        )
    else:
        print(
            f"  ✓ in the public snapshot (built {when(built)}, "
            f"job first seen {ago(job['firstSeenAt'])})"
        )

    emb = job.get("embedding")
    if not emb:
        return

    v = np.asarray(emb, dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-9

    ip = WORK / "ideal.json"
    if not ip.exists():
        print("  (no work/ideal.json, so no ranking context)")
        return

    _, ideal_v = ideal()
    sim = float(v @ ideal_v)

    local = None
    if jp.exists():
        import duckdb

        local = (
            duckdb.connect()
            .execute(
                f"SELECT leaf, sim, title, location FROM read_parquet('{jp}') "
                f"WHERE ats=? AND slug=? AND id=?",
                [r["ats"], r["slug"], job["id"]],
            )
            .fetchone()
        )

    # Authoritative membership check via downloaded leaf files
    key = f"{r['ats']}/{r['slug']}#{job['id']}"
    gdir = WORK / "groups"
    current: set = set()
    stale_files = 0
    found_in = None
    if gdir.is_dir():
        for gf in sorted(glob.glob(str(gdir / "*.json"))):
            try:
                g = json.loads(open(gf, encoding="utf-8").read())
                node = T[g["leaf"]] if 0 <= g.get("leaf", -1) < len(T) else None
            except Exception:
                continue
            if (
                not node
                or node["children"]
                or (g.get("lo"), g.get("hi"), len(g["jobs"]))
                != (node["lo"], node["hi"], node["size"])
            ):
                stale_files += 1
                continue
            current.add(g["leaf"])
            if found_in is None and any(
                f"{x['ats']}/{x['slug']}#{x['id']}" == key for x in g["jobs"]
            ):
                found_in = g["leaf"]

    # Tree-descent estimate of group (fallback when not found in downloaded files)
    n_node = T[0]
    while n_node["children"]:
        n_node = max(
            (T[c] for c in n_node["children"]),
            key=lambda c: float(C[c["id"]] @ v),
        )

    leaves = nearest(m, C, ideal_v, len(T))
    rank_of = {lf["id"]: i for i, (lf, _) in enumerate(leaves, 1)}

    print(f"  similarity to your ideal JD: {sim:.3f}")

    if found_in is not None:
        print(
            f"  ✓ group {found_in} ({T[found_in]['label']}): read from the downloaded "
            f"leaf file, rank {rank_of[found_in]} of {len(leaves)} for your JD; "
            f"that group IS in your slice ({len(current)} current group file(s))"
        )
    else:
        est = n_node["id"]
        print(
            f"  ~ group {est} ({n_node['label']}) by tree descent — an ESTIMATE "
            f"(the build assigns in PCA space; this can name a neighbouring leaf), "
            f"rank {rank_of[est]} of {len(leaves)} for your JD"
        )
        if current:
            if est in current:
                print(
                    f"    that group's file is in your slice ({len(current)} current "
                    f"group file(s)) and the job is not in it: the estimate is off by "
                    f"a leaf. `fetch --top {max(rank_of[est] + 5, len(current))}` "
                    "pulls the neighbourhood around it."
                )
            else:
                print(
                    f"    not among the {len(current)} group file(s) you have "
                    f"(it ranks {rank_of[est]}). "
                    f"`fetch --top {max(rank_of[est], len(current))}` "
                    f"(or `--groups {est}`) would include it, if the estimate is right."
                )
        else:
            print(
                "    no current group files in work/groups/ "
                "(nothing fetched yet, or all predate today's manifest)"
            )

    if stale_files:
        print(
            f"    ({stale_files} file(s) in work/groups/ predate today's manifest "
            "and were ignored; `fetch` refreshes them)"
        )

    if jp.exists():
        import duckdb

        total = (
            duckdb.connect()
            .execute(
                f"SELECT count(*), sum(sim > ?) FROM read_parquet('{jp}')",
                [sim],
            )
            .fetchone()
        )
        if local:
            print(
                f"  ✓ it IS in work/jobs.parquet at rank "
                f"{int(total[1] or 0) + 1} of {total[0]} by similarity."
            )
            pref = (
                json.loads(ip.read_text(encoding="utf-8")).get("location") or ""
            ).strip()
            if pref:
                from locparse import eligibility as loc_eligibility

                el, why = loc_eligibility(pref, local[3] or "", "", local[2] or "")
                if el is False:
                    print(
                        f"    but eligibility for {pref!r} hides it: {why} "
                        "(toggle the ✓ eligible filter, or it's a parser miss "
                        "worth a bug report)"
                    )

            hidden: set = set()
            ipath = WORK / "interactions.jsonl"
            if ipath.exists():
                for line in ipath.open(encoding="utf-8"):
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") == "hide_company" and ev.get("board"):
                        if ev.get("on", True):
                            hidden.add(ev["board"])
                        else:
                            hidden.discard(ev["board"])

            if f"{r['ats']}/{r['slug']}" in hidden:
                print(
                    '    and this company is hidden ("never show again"); '
                    "un-hide it in the page."
                )
            elif el is not False:
                print(
                    "    so if it isn't on the page, check the active facets, "
                    "bans, and search box: it is in the data."
                )
        else:
            print(
                f"  ✗ not in work/jobs.parquet; by similarity it would sit at rank "
                f"{int(total[1] or 0) + 1} of {total[0]} in your slice."
            )

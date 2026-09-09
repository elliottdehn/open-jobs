"""Rebuild ../slugs.json from the fleet itself, so the committed slug list is exactly the set of boards the
Worker runs, classified by what the crawler last saw. Run after a consolidation (the ledger export carries every
board's meta):

  uv run scripts/build-slugs.py [--ledger export/<date>/ledger-raw] [--boards src/boards.json]

Schema (v2; the old file was a Common Crawl mining artifact with crawl metadata and 25 providers):
  {"schema": 2, "generated_at": ISO date, "source": "...", "counts": {...},
   "ats":  {ats: [slug, ...]},   boards the crawler fetches: last status ok, error, or never fetched
   "gone": {ats: [slug, ...]}}   boards the crawler found dead (portal removed, 404, unmappable); still part of our
                                 fleet (they back off to weekly), a fresh deploy may leave them out
build-boards.mjs turns this into src/boards.json (ats + gone by default; --live-only for ats only).
"""
import argparse, collections, glob, json, os, time
ap = argparse.ArgumentParser()
ap.add_argument("--ledger", default=sorted(glob.glob("export/*/ledger-raw"))[-1] if glob.glob("export/*/ledger-raw") else None)
ap.add_argument("--boards", default="src/boards.json")
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "slugs.json"))
a = ap.parse_args()
if not a.ledger: raise SystemExit("no export/<date>/ledger-raw found; pass --ledger")
boards = json.load(open(a.boards))
meta = {}
for f in sorted(glob.glob(os.path.join(a.ledger, "*.ndjson"))):
    ats = os.path.basename(f)[:-len(".ndjson")]
    with open(f) as fh:
        for line in fh:
            try: d = json.loads(line)
            except Exception: continue
            if d.get("part"): continue
            meta[(ats, d["slug"])] = ((d.get("meta") or {}).get("lastStatus")) or "never"
date = os.path.basename(os.path.dirname(os.path.abspath(a.ledger)))
out = {"schema": 2, "generated_at": time.strftime("%Y-%m-%d"), "source": f"the fleet's own board metadata (ledger export of {date}); rebuild with backend/scripts/build-slugs.py",
       "counts": {}, "ats": {}, "gone": {}}
dist = collections.Counter()
for ats, slugs in sorted(boards.items()):
    live, gone = [], []
    for s in sorted(set(slugs)):
        st = meta.get((ats, s), "never"); dist[st] += 1
        (gone if st == "gone" else live).append(s)
    out["ats"][ats] = live
    if gone: out["gone"][ats] = gone
    out["counts"][ats] = {"live": len(live), "gone": len(gone)}
out["counts"]["total"] = {"live": sum(len(v) for v in out["ats"].values()), "gone": sum(len(v) for v in out["gone"].values()), "by_last_status": dict(dist)}
json.dump(out, open(a.out, "w"), indent=0, ensure_ascii=False); open(a.out, "a").write("\n")
print(f"wrote {os.path.relpath(a.out)}: {out['counts']['total']['live']:,} live + {out['counts']['total']['gone']:,} gone boards across {len(out['ats'])} providers; last status {dict(dist)}")

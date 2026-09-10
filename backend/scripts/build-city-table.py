# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "boto3"]
# ///
"""Bare city -> country table for tools/locparse.py, learned from the corpus: for every "City, <place that
resolves to one country>" segment, tally city -> country; keep cities with >= 25 postings where one country
has >= 90%. Hand-written aliases in locparse win. Writes tools/city_countries.json (~5k entries).
Run: EXPORT_DIR=export/latest uv run scripts/build-city-table.py"""
import duckdb, sys, os, collections, json, re
here = os.path.dirname(__file__); sys.path.insert(0, os.path.join(here, "..", "..", "tools"))
import locparse; from locparse import parse, _split
sys.path.insert(0, here)
from r2 import R2
_ed = os.environ.get("EXPORT_DIR", "export/latest"); _s3 = _ed.startswith("s3://")
root = _ed.rstrip("/") if _s3 else os.path.join(here, "..", _ed)
work = os.environ.get("WORK_DIR") or (os.path.join(here, "..", "work-" + _ed.rstrip("/").rsplit("/", 1)[-1]) if _s3 else root)
base = dict(locparse.CITY_COUNTRY)  # includes the previous table; rebuild from scratch below
_con = (R2().duckdb(duckdb.connect()) if _s3 else duckdb.connect()); _con.execute("SET memory_limit='4GB'"); _con.execute("SET threads=4")  # the default (80% of RAM) plus Python's heap exceeded the 16 GB VM (2026-09-10)
rows = _con.execute(f"SELECT location, count(*) FROM read_parquet('{root}/jobs/*.parquet') WHERE is_open AND location IS NOT NULL GROUP BY location").fetchall(); _con.close()
tally = collections.defaultdict(collections.Counter)
for loc, n in rows:
    for seg in _split(loc):
        toks = [t.strip() for t in seg.split(",") if t.strip()]
        if len(toks) < 2: continue
        city = toks[0].lower().strip(" .")
        if not city or len(city) > 30 or re.search(r"\d|remote|hybrid|office|campus|hq\b|anywhere", city): continue
        p = parse(seg, "", "")
        if len(p["countries"]) == 1: tally[city][p["countries"][0]] += n
table = {}
for city, c in tally.items():
    tot = sum(c.values()); cc, top = c.most_common(1)[0]
    if tot >= 25 and top / tot >= 0.9: table[city] = cc
out = os.path.join(here, "..", "..", "tools", "city_countries.json")
json.dump(dict(sorted(table.items())), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"{len(rows):,} distinct locations -> {len(table):,} unambiguous bare cities; wrote {out}")

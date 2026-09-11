# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "boto3"]
# ///
"""The ledger, derived: every posting that has ever appeared in the dataset, the day it appeared and the day it left.

  uv run scripts/derive-ledger.py --date D --export <exports/D root: local dir or s3://bucket/exports/D>
                                  --prev-ledger <ledger/<d> dir or s3 prefix> [--prev-export <exports/<d> root>] [--out export/ledger]

Rules: a row in today's export is open (first_seen_at from the previous ledger when it knew the row); a row the previous
ledger had open that is absent from today's export left the dataset today (removed_at = D 00:00 UTC, is_open false);
rows already removed are carried unchanged. With --prev-export (the first derived day after the pulled ledgers), open
rows of the previous ledger that were not in the previous export are dropped: postings the crawler held but never
published were never part of the dataset. Same schema and part layout as the pulled ledger (build-ledger.py).
Replaces the nightly status=all pull through the Worker (two hours, and a 12 GiB sort) with one DuckDB pass.
"""
import argparse, glob, os, sys, time
import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--date", required=True); ap.add_argument("--export", required=True); ap.add_argument("--prev-ledger", required=True)
ap.add_argument("--prev-export"); ap.add_argument("--out", default="export/ledger")
a = ap.parse_args()
t0 = time.time()
con = duckdb.connect(); con.execute("SET TimeZone='UTC'"); con.execute("SET preserve_insertion_order=false"); con.execute("SET threads=4")
con.execute(f"SET memory_limit='{os.environ.get('LEDGER_MEMORY', '6GB')}'")
tmp = os.path.join(a.out, ".tmp"); os.makedirs(tmp, exist_ok=True); con.execute(f"SET temp_directory='{tmp}'")
if any(p.startswith("s3://") for p in (a.export, a.prev_ledger, a.prev_export or "")):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from r2 import R2; R2().duckdb(con)
def glob_of(root, sub): return f"{root.rstrip('/')}/{sub}"
COLS = "ats, slug, id, title, location, url, published_at, content_hash, first_seen_at, last_seen_at, changed_at, detail_status, embed_status"
con.execute(f"CREATE VIEW today AS SELECT {COLS} FROM read_parquet('{glob_of(a.export, 'jobs/*.parquet')}', union_by_name=true)")
con.execute(f"CREATE VIEW prev_all AS SELECT * FROM read_parquet('{glob_of(a.prev_ledger, '*.parquet')}', union_by_name=true)")
if a.prev_export:
    con.execute(f"CREATE VIEW prevx AS SELECT ats, slug, id FROM read_parquet('{glob_of(a.prev_export, 'jobs/*.parquet')}', union_by_name=true)")
    con.execute("CREATE VIEW prev AS SELECT p.* FROM prev_all p WHERE NOT p.is_open OR EXISTS (SELECT 1 FROM prevx x WHERE x.ats = p.ats AND x.slug = p.slug AND x.id = p.id)")
else:
    con.execute("CREATE VIEW prev AS SELECT * FROM prev_all")
outd = os.path.join(a.out, a.date); os.makedirs(outd, exist_ok=True)
for f in glob.glob(os.path.join(outd, "*.parquet")): os.remove(f)
con.execute(f"""COPY (
  SELECT t.ats, t.slug, t.id, t.title, t.location, t.url, t.published_at, t.content_hash,
         coalesce(p.first_seen_at, t.first_seen_at) AS first_seen_at, t.last_seen_at, t.changed_at,
         NULL::TIMESTAMPTZ AS removed_at, true AS is_open, t.detail_status, t.embed_status
  FROM today t LEFT JOIN prev p ON p.ats = t.ats AND p.slug = t.slug AND p.id = t.id
  UNION ALL
  SELECT p.ats, p.slug, p.id, p.title, p.location, p.url, p.published_at, p.content_hash, p.first_seen_at, p.last_seen_at, p.changed_at,
         CASE WHEN p.is_open THEN TIMESTAMPTZ '{a.date} 00:00:00+00' ELSE p.removed_at END AS removed_at, false AS is_open, p.detail_status, p.embed_status
  FROM prev p ANTI JOIN today t ON t.ats = p.ats AND t.slug = p.slug AND t.id = p.id
) TO '{outd}' (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '200MB', ROW_GROUP_SIZE 20000)""")
parts = sorted(glob.glob(os.path.join(outd, "*.parquet"))); out_bytes = sum(os.path.getsize(f) for f in parts)
n, n_open, n_rem, n_left_today, boards = con.execute(f"SELECT count(*), count(*) FILTER (is_open), count(*) FILTER (NOT is_open), count(*) FILTER (removed_at = TIMESTAMPTZ '{a.date} 00:00:00+00'), count(DISTINCT ats||'/'||slug) FROM read_parquet('{outd}/*.parquet')").fetchone()
print(f"ledger {a.date} (derived): {n:,} postings ever published on {boards:,} boards; {n_open:,} open today, {n_rem:,} gone ({n_left_today:,} left today); {out_bytes/1e6:.0f} MB in {len(parts)} part(s), {time.time()-t0:.0f}s -> {outd}/", flush=True)

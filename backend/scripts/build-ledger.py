# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""The ledger: every job the crawler has ever recorded, open or removed, with its dates — no text, no vectors.

  EXPORT_DIR=export/<date> uv run scripts/build-ledger.py [--raw $EXPORT_DIR/ledger-raw] [--out export/ledger]

Input: slim /export ndjson (status=all) pulled by consolidate.sh step 2b into $EXPORT_DIR/ledger-raw/<ats>.ndjson.
Output: export/ledger/<date>/data_*.parquet (parts <= 200 MB, the wrangler upload cap), one row per (ats, slug, id): first_seen_at, last_seen_at, changed_at,
removed_at, is_open, plus title/location/url/published_at/content_hash for joining. This is the source of truth
for posting lifetimes (survival curves, re-stamp detection): the Board DOs keep removed jobs with removed_at,
so nothing here is inferred from snapshots.
"""
import argparse, glob, os, sys, time
import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--raw", default=os.path.join(os.environ.get("EXPORT_DIR", "export"), "ledger-raw"))
ap.add_argument("--out", default="export/ledger")
ap.add_argument("--date", default=os.path.basename(os.path.abspath(os.environ.get("EXPORT_DIR", "export"))))
a = ap.parse_args()
files = sorted(glob.glob(os.path.join(a.raw, "*.ndjson")))
if not files: sys.exit(f"no ndjson in {a.raw}")
os.makedirs(a.out, exist_ok=True)
t0 = time.time()
COLS = ("{ats:'VARCHAR', slug:'VARCHAR', jobs:'STRUCT(id VARCHAR, title VARCHAR, location VARCHAR, url VARCHAR, publishedAt VARCHAR, "
        "contentHash VARCHAR, firstSeenAt BIGINT, lastSeenAt BIGINT, changedAt BIGINT, removedAt BIGINT, detailStatus VARCHAR, embedStatus VARCHAR)[]'}")
con = duckdb.connect(); con.execute("SET preserve_insertion_order=false")
con.execute("SET TimeZone='UTC'")  # date-only posting dates cast to the session zone; the laptop (EDT) and the container (UTC) disagreed by 4 h on 2026-09-10
lst = ", ".join(f"'{f}'" for f in files)
outd = os.path.join(a.out, a.date); os.makedirs(outd, exist_ok=True)
for f in glob.glob(os.path.join(outd, "*.parquet")): os.remove(f)
con.execute(f"""COPY (
  SELECT ats, slug, j.id, j.title, j.location, j.url, try_cast(j.publishedAt AS TIMESTAMPTZ) AS published_at, j.contentHash AS content_hash,
         to_timestamp(j.firstSeenAt/1000) AS first_seen_at, to_timestamp(j.lastSeenAt/1000) AS last_seen_at,
         to_timestamp(j.changedAt/1000) AS changed_at, to_timestamp(j.removedAt/1000) AS removed_at, j.removedAt IS NULL AS is_open,
         j.detailStatus AS detail_status, j.embedStatus AS embed_status
  FROM read_ndjson([{lst}], maximum_object_size=67108864, columns={COLS}), unnest(jobs) AS t(j)
  QUALIFY row_number() OVER (PARTITION BY ats, slug, j.id ORDER BY j.changedAt DESC NULLS LAST) = 1
) TO '{outd}' (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '200MB', ROW_GROUP_SIZE 20000)""")
parts = sorted(glob.glob(os.path.join(outd, "*.parquet"))); out_bytes = sum(os.path.getsize(f) for f in parts)
n, n_open, n_rem, boards = con.execute(f"SELECT count(*), count(*) FILTER (is_open), count(*) FILTER (NOT is_open), count(DISTINCT ats||'/'||slug) FROM read_parquet('{outd}/*.parquet')").fetchone()
print(f"ledger {a.date}: {n:,} jobs on {boards:,} boards ({n_open:,} open, {n_rem:,} removed) from {len(files)} ATS files; {out_bytes/1e6:.0f} MB in {len(parts)} part(s), {time.time()-t0:.0f}s -> {outd}/")

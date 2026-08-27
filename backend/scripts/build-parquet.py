# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""Flatten export/*.ndjson (one line per board) into parquet, streaming one ATS at a time so
memory stays bounded (the full job set with content + raw is several GB):
  export/jobs/<ats>.parquet    one row per job      -> read_parquet('export/jobs/*.parquet')
  export/boards/<ats>.parquet  one row per board    -> read_parquet('export/boards/*.parquet')
Run: uv run scripts/build-parquet.py"""
import glob, os, sys
import duckdb

root = os.path.join(os.path.dirname(__file__), "..", os.environ.get("EXPORT_DIR", "export"))  # EXPORT_DIR=export-slim for the vector-only pull
files = sorted(glob.glob(os.path.join(root, "*.ndjson")))
if not files:
    sys.exit("no export/*.ndjson files; run scripts/export.mjs or scripts/pull-all.sh first")
for d in ("jobs", "boards"):
    os.makedirs(os.path.join(root, d), exist_ok=True)

con = duckdb.connect()
con.execute("SET preserve_insertion_order = false")
con.execute("SET threads = 2")
con.execute("SET memory_limit = '10GB'")
con.execute(f"SET temp_directory = '{os.path.join(root, '.duckdb_tmp')}'")

# Keys are read via JSON paths so files where a key never occurs (e.g. no resolved company yet,
# jobs predating the detail stage) don't fail the struct binder.
def js(expr, path, typ="VARCHAR"):
    return f"try_cast(json_extract_string({expr}, '{path}') AS {typ})"

def ts(expr, path):
    return f"to_timestamp({js(expr, path, 'BIGINT')}/1000)"

BOARDS_SQL = f"""
SELECT ats, slug,
       {js('m', '$.lastStatus')} AS last_status,
       {ts('m', '$.lastRunAt')} AS last_run_at,
       {ts('m', '$.lastOkAt')}  AS last_ok_at,
       {js('m', '$.lastError')} AS last_error,
       {js('m', '$.consecutiveFailures', 'INTEGER')} AS consecutive_failures,
       {js('m', '$.jobCount', 'INTEGER')} AS job_count,
       {js('m', '$.slotMs', 'BIGINT')} AS slot_ms,
       {js('l', '$.exported_jobs', 'INTEGER')} AS exported_jobs,
       {js('l', '$.error')} AS export_error,
       {js('m', '$.company.name')}           AS company_name,
       {js('m', '$.company.website')}        AS company_website,
       {js('m', '$.company.careers_url')}    AS company_careers_url,
       {js('m', '$.company.linkedin_url')}   AS company_linkedin_url,
       {js('m', '$.company.hq_location.city')}         AS company_hq_city,
       {js('m', '$.company.hq_location.region')}       AS company_hq_region,
       {js('m', '$.company.hq_location.country_code')} AS company_hq_country,
       {js('m', '$.company.industry')}       AS company_industry,
       {js('m', '$.company.description')}    AS company_description,
       {js('m', '$.company.is_staffing_agency', 'BOOLEAN')} AS company_is_staffing_agency,
       {js('m', '$.company.size_bucket')}    AS company_size_bucket,
       {js('m', '$.company.confidence', 'DOUBLE')} AS company_confidence,
       {ts('m', '$.company.resolved_at')}    AS company_resolved_at,
       {js('m', '$.companyError')}           AS company_error
FROM (SELECT ats, slug, to_json(meta) AS m, to_json(x) AS l FROM {{src}} AS x)
"""

JOB_COLUMNS = ("{id:'VARCHAR', title:'VARCHAR', location:'VARCHAR', url:'VARCHAR', departments:'VARCHAR[]', "
               "publishedAt:'VARCHAR', updatedAt:'VARCHAR', content:'VARCHAR', raw:'JSON', detailRaw:'JSON', "
               "detailStatus:'VARCHAR', contentHash:'VARCHAR', firstSeenAt:'BIGINT', lastSeenAt:'BIGINT', "
               "changedAt:'BIGINT', removedAt:'BIGINT', enrichStatus:'VARCHAR', enrichedAt:'BIGINT', "
               "enrichment:'JSON', embedStatus:'VARCHAR', embedModel:'VARCHAR', embedding:'FLOAT[]', ats:'VARCHAR', slug:'VARCHAR'}")

JOBS_SQL = """
SELECT ats, slug, id, title, location, url, departments,
       try_cast(publishedAt AS TIMESTAMPTZ) AS published_at,
       try_cast(updatedAt   AS TIMESTAMPTZ) AS updated_at,
       content,
       raw::VARCHAR       AS raw_json,
       detailRaw::VARCHAR AS detail_raw_json,
       detailStatus AS detail_status,
       contentHash  AS content_hash,
       to_timestamp(firstSeenAt/1000) AS first_seen_at,
       to_timestamp(lastSeenAt/1000)  AS last_seen_at,
       to_timestamp(changedAt/1000)   AS changed_at,
       to_timestamp(removedAt/1000)   AS removed_at,
       removedAt IS NULL AS is_open,
       enrichStatus AS enrich_status,
       to_timestamp(enrichedAt/1000)  AS enriched_at,
       enrichment::VARCHAR AS enrichment_json,
       embedStatus AS embed_status,
       embedModel  AS embed_model,
       embedding
FROM {src}
"""

import json

def split_ndjson(f, jobs_out, boards_out):
    """Stream one per-board NDJSON file into per-job and per-board JSONL (bounded memory:
    one board line at a time; boards with thousands of jobs are 100+ MB lines)."""
    with open(f) as src, open(jobs_out, "w") as jo, open(boards_out, "w") as bo:
        for line in src:
            if not line.strip():
                continue
            b = json.loads(line)
            jobs = b.get("jobs") or []
            if not b.get("part"):  # multi-part boards (vector exports) repeat meta on every part; emit the board once
                bo.write(json.dumps({"ats": b["ats"], "slug": b["slug"], "meta": b.get("meta"), "exported_jobs": len(jobs), "error": b.get("error")}) + "\n")
            for j in jobs:
                j["ats"] = b["ats"]; j["slug"] = b["slug"]
                jo.write(json.dumps(j) + "\n")

tmp = os.path.join(root, ".split")
os.makedirs(tmp, exist_ok=True)
force = "--force" in sys.argv
for f in files:
    ats = os.path.basename(f)[: -len(".ndjson")]
    outs = {k: os.path.join(root, k, f"{ats}.parquet") for k in ("jobs", "boards")}
    if not force and all(os.path.exists(o) and os.path.getmtime(o) > os.path.getmtime(f) for o in outs.values()):
        print(f"{ats:16} up to date", flush=True)
        continue
    jl, bl = os.path.join(tmp, f"{ats}.jobs.jsonl"), os.path.join(tmp, f"{ats}.boards.jsonl")
    split_ndjson(f, jl, bl)
    bsrc = f"read_ndjson('{bl}', maximum_object_size=67108864)"
    jsrc = f"read_ndjson('{jl}', maximum_object_size=67108864, columns={JOB_COLUMNS})"
    con.execute(f"COPY ({BOARDS_SQL.format(src=bsrc)}) TO '{outs['boards']}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute(f"COPY ({JOBS_SQL.format(src=jsrc)}) TO '{outs['jobs']}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)")
    os.remove(jl); os.remove(bl)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{outs['jobs']}')").fetchone()[0]
    print(f"{ats:16} {n:>9,} jobs", flush=True)

def show(sql):
    con.sql(sql).show(max_rows=50, max_width=200)

B = os.path.join(root, "boards", "*.parquet")
J = os.path.join(root, "jobs", "*.parquet")
show(f"SELECT count(*) AS boards, count(*) FILTER (last_status='ok') AS ok, count(*) FILTER (last_status='gone') AS gone, count(*) FILTER (last_status='error') AS error, count(*) FILTER (last_status IS NULL) AS unfetched FROM read_parquet('{B}')")
show(f"SELECT ats, count(*) AS jobs, count(*) FILTER (is_open) AS open, count(*) FILTER (length(content) > 800) AS with_body, count(DISTINCT slug) AS boards FROM read_parquet('{J}') GROUP BY ats ORDER BY jobs DESC")
show(f"SELECT count(*) AS total_jobs, count(*) FILTER (is_open) AS open_jobs, count(*) FILTER (is_open AND length(content) > 800) AS open_with_body FROM read_parquet('{J}')")
size = sum(os.path.getsize(p) for p in glob.glob(J))
print(f"wrote export/jobs/*.parquet ({size / 1e6:.0f} MB) and export/boards/*.parquet")

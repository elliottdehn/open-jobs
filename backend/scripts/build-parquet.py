# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "boto3"]
# ///
"""Flatten export/*.ndjson (one line per board) into parquet, streaming one ATS at a time so
memory stays bounded (the full job set with content + raw is several GB):
  export/jobs/<ats>.parquet    one row per job      -> read_parquet('export/jobs/*.parquet')
  export/boards/<ats>.parquet  one row per board    -> read_parquet('export/boards/*.parquet')
Run: uv run scripts/build-parquet.py"""
import time, glob, os, re, sys
import duckdb
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2

# SNAPSHOT_SOURCE=r2 reads snapshots/<ats>/*.parquet from the bucket in place (no local pull); default = local
# <EXPORT_DIR>/snapshots/. --publish uploads jobs/<ats>.parquet and boards/<ats>.parquet to exports/<date>/ in R2.
# --ats a,b restricts to those providers (tests). Local-only providers (ndjson via /export) are unchanged.
root = os.path.join(os.path.dirname(__file__), "..", os.environ.get("EXPORT_DIR", "export"))  # EXPORT_DIR=export-slim for the vector-only pull
date_name = os.path.basename(os.path.realpath(root))
argv = sys.argv[1:]
only = set(next((a.split("=", 1)[1] for a in argv if a.startswith("--ats=")), "").split(",")) - {""}
publish = "--publish" in argv
dedup_only = "--dedup-only" in argv  # rerun just the end-of-run aggregator dedup over the day's dark part files
from_r2 = os.environ.get("SNAPSHOT_SOURCE", "local") == "r2"
r2 = R2() if (from_r2 or publish) else None
files = sorted(glob.glob(os.path.join(root, "*.ndjson")))
if from_r2:
    import json as _json
    boards = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "src", "boards.json")))
    exclude = set(os.environ.get("SNAPSHOT_EXCLUDE", "jobscore,governmentjobs").split(","))
    snap_r2 = sorted(a for a in boards if a not in exclude)
    snap_dirs = []; snap_ats = set(snap_r2)
else:
    snap_r2 = []
    snap_dirs = sorted(d for d in glob.glob(os.path.join(root, "snapshots", "*")) if glob.glob(os.path.join(d, "*.parquet")))
    snap_ats = {os.path.basename(d) for d in snap_dirs}
if only:
    files = [f for f in files if os.path.basename(f)[:-len(".ndjson")] in only]; snap_dirs = [d for d in snap_dirs if os.path.basename(d) in only]; snap_r2 = [a for a in snap_r2 if a in only]
if not dedup_only and not files and not snap_dirs and not snap_r2:
    sys.exit("no export/*.ndjson files or snapshots/; run scripts/pull-snapshots.mjs or scripts/export.mjs first")
for d in ("jobs", "boards"):
    os.makedirs(os.path.join(root, d), exist_ok=True)

con = duckdb.connect()
con.execute("SET TimeZone='UTC'")  # date-only posting dates cast to the session zone; the laptop (EDT) and the container (UTC) disagreed by 4 h on 2026-09-10
if from_r2: r2.duckdb(con)
con.execute("SET preserve_insertion_order = false")
con.execute("SET threads = 2")
con.execute(f"SET memory_limit = '{os.environ.get('PARQUET_MEMORY', '10GB')}'")  # the image sets PARQUET_MEMORY for a 12 GiB instance
con.execute(f"SET temp_directory = '{os.path.join(root, '.duckdb_tmp')}'")

# Keys are read via JSON paths so files where a key never occurs (e.g. no resolved company yet,
# jobs predating the detail stage) don't fail the struct binder.
def js(expr, path, typ="VARCHAR"):
    return f"try_cast(json_extract_string({expr}, '{path}') AS {typ})"

def ts(expr, path):
    return f"to_timestamp({js(expr, path, 'BIGINT')}/1000)"

BOARDS_SQL = f"""
SELECT ats::VARCHAR AS ats, slug::VARCHAR AS slug,  -- read_ndjson types a column of GUID-shaped slugs (paylocity) as UUID; every reader unions these files by name
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
               "enrichment:'JSON', embedStatus:'VARCHAR', embedModel:'VARCHAR', embedding:'FLOAT[]', org:'VARCHAR', ats:'VARCHAR', slug:'VARCHAR'}")

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
       org,
       embedding
FROM {src}
-- Contentless jobs are noise: a discovered URL whose detail fetch never yielded a JD (dark links,
-- bot-walled detail pages) can't be usefully searched, ranked, or embedded — drop them here.
WHERE content IS NOT NULL AND length(trim(content)) > 0
-- Collapse duplicate (ats, slug, id) rows: a resumed pull can re-fetch a seam board, so the same
-- posting can appear twice in the ndjson. Keep the freshest / most-complete copy.
QUALIFY row_number() OVER (PARTITION BY ats, slug, id ORDER BY changedAt DESC NULLS LAST, length(content) DESC NULLS LAST) = 1
"""

import json

# Jobs from per-board R2 parquet snapshots (src/snapshot.ts): already columnar, open jobs only.
# Mapped to the exact schema JOBS_SQL produces so everything downstream is source-agnostic.
# raw/detailRaw are not carried in snapshots (debug-only weight) -> NULL columns.
SNAPSHOT_JOBS_SQL = """
SELECT ats, slug, id, title, location, url,
       coalesce(from_json(departments_json, '["VARCHAR"]'), []) AS departments,
       try_cast(published_at AS TIMESTAMPTZ) AS published_at,
       try_cast(updated_at   AS TIMESTAMPTZ) AS updated_at,
       content,
       CAST(NULL AS VARCHAR) AS raw_json,
       CAST(NULL AS VARCHAR) AS detail_raw_json,
       detail_status,
       content_hash,
       to_timestamp(first_seen_ms/1000) AS first_seen_at,
       to_timestamp(last_seen_ms/1000)  AS last_seen_at,
       to_timestamp(changed_ms/1000)    AS changed_at,
       CAST(NULL AS TIMESTAMPTZ) AS removed_at,
       TRUE AS is_open,
       enrich_status,
       CAST(NULL AS TIMESTAMPTZ) AS enriched_at,
       enrichment_json,
       embed_status,
       embed_model,
       org,
       embedding
FROM {src}
WHERE content IS NOT NULL AND length(trim(content)) > 0
"""

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

def finalize(ats, outs, name=None):
    name = name or ats
    """Shared tail for both sources: the tier columns, the dark aggregator tier, count, optional publish.

    Every jobs row carries `tier` ('first_party' | 'aggregator'), `via` (the board a second-tier posting came
    through, else NULL) and `org` (the hiring organization a job board names, else NULL). A `dark` board whose
    open postings name >2 distinct organizations is a job board, not an employer: until 2026-09-10 its postings
    were dropped; now they are kept as the aggregator tier, provided they name an organization and a location,
    and dedup_aggregators() at the end of the run removes what first-party boards already carry."""
    if ats == "dark":
        agg = con.execute(f"""SELECT slug FROM (SELECT slug, count(DISTINCT lower(org)) AS o
                              FROM read_parquet('{outs['jobs']}') WHERE is_open AND org IS NOT NULL GROUP BY slug)
                              WHERE o > 2""").fetchall()
        aggs = [r[0] for r in agg]
        inlist = ",".join("?" * len(aggs)) or "''"
        con.execute(f"""COPY (SELECT *, CASE WHEN slug IN ({inlist}) THEN 'aggregator' ELSE 'first_party' END AS tier,
                               CASE WHEN slug IN ({inlist}) THEN slug END AS via
                        FROM read_parquet('{outs['jobs']}')
                        WHERE slug NOT IN ({inlist})
                           OR (org IS NOT NULL AND trim(org) != '' AND location IS NOT NULL AND trim(location) != ''))
                        TO '{outs['jobs']}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)""", aggs * 3)
        n_agg = con.execute(f"SELECT count(*) FROM read_parquet('{outs['jobs']}') WHERE tier = 'aggregator'").fetchone()[0]
        print(f"  dark: {len(aggs)} job boards kept as the aggregator tier ({n_agg:,} located postings naming an employer; deduped at the end of the run)", flush=True)
    else:
        con.execute(f"COPY (SELECT *, 'first_party' AS tier, CAST(NULL AS VARCHAR) AS via FROM read_parquet('{outs['jobs']}')) TO '{outs['jobs']}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{outs['jobs']}')").fetchone()[0]
    if publish:
        for k in ("jobs", "boards"): r2.put_file(f"exports/{date_name}/{k}/{name}.parquet", outs[k], "application/octet-stream")
        if os.environ.get("LOW_DISK") == "1":  # cloud container (20 GB disk): the bucket copy is the copy
            for k in ("jobs", "boards"): os.remove(outs[k])
    print(f"{ats:16} {n:>9,} jobs" + (f"  -> exports/{date_name}/" if publish else ""), flush=True)

tmp = os.path.join(root, ".split")
os.makedirs(tmp, exist_ok=True)
force = "--force" in sys.argv
for f in ([] if dedup_only else files):
    ats = os.path.basename(f)[: -len(".ndjson")]
    if ats in snap_ats:
        print(f"{ats:16} (ndjson skipped: R2 snapshots present)", flush=True)
        continue
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
    finalize(ats, outs)


# ---- per-board R2 snapshot parquets: local (scripts/pull-snapshots.mjs) or read from the bucket in place ----
import datetime as _dt
PART_ROWS = int(os.environ.get("PARQUET_PART_ROWS", "2000000"))
def _published_recently(name):
    """Same-day resume: both published objects exist and were written in the last 12 hours -> this run already did it."""
    if not (publish and from_r2 and not force): return False
    cut = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=12)
    for k in ("jobs", "boards"):
        h = r2.head(f"exports/{date_name}/{k}/{name}.parquet")
        if not h or not h.get("modified") or h["modified"] < cut: return False
    return True

for src in ([] if dedup_only else (snap_dirs or snap_r2)):
    if from_r2:
        ats = src; pq = r2.url(f"snapshots/{ats}/*.parquet")
        outs = {k: os.path.join(root, k, f"{ats}.parquet") for k in ("jobs", "boards")}
        if _published_recently(ats):
            print(f"{ats:16} published earlier this run; skipped (--force to redo)", flush=True); continue
        if not any(True for _ in r2.list(f"snapshots/{ats}/")):
            print(f"{ats:16} no snapshots in R2", flush=True); continue
    else:
        d = src; ats = os.path.basename(d)
        outs = {k: os.path.join(root, k, f"{ats}.parquet") for k in ("jobs", "boards")}
        parts = glob.glob(os.path.join(d, "*.parquet"))
        newest = max(os.path.getmtime(x) for x in parts)
        if not force and all(os.path.exists(o) and os.path.getmtime(o) > newest for o in outs.values()):
            print(f"{ats:16} up to date (snapshots)", flush=True)
            continue
        pq = os.path.join(d, "*.parquet")
    # boards.parquet: board meta rides in each snapshot's footer kv; rebuild the boards.jsonl shape
    # split_ndjson produces so BOARDS_SQL is reused unchanged. The bucket reads retry (r2.duckdb) and this
    # block retries on top: a transient storage error must not cost the night.
    for attempt in range(4):
        try:
            kv = con.execute(f"SELECT file_name, decode(value) FROM parquet_kv_metadata('{pq}') WHERE decode(key) = 'board_meta'").fetchall()
            counts = dict(con.execute(f"SELECT filename, count(*) FROM read_parquet('{pq}', filename=true) GROUP BY 1").fetchall())
            break
        except Exception as e:
            if attempt == 3: raise
            print(f"{ats:16} bucket read failed ({str(e)[:120]}); retrying in {15 * (attempt + 1)}s", flush=True); time.sleep(15 * (attempt + 1))
    # A source bigger than PARQUET_PART_ROWS is converted and published in parts (jobs/<ats>.p<n>.parquet), each a few
    # GB at most, so the local disk never holds the whole source (dark is ~17M rows; the cloud instance has 20 GB).
    # Files are packed by board (a board's own part files stay together), so per-board rules still hold per part.
    total_rows = sum(counts.values())
    if from_r2 and total_rows > PART_ROWS:
        by_slug = {}
        for fn in counts: by_slug.setdefault(re.sub(r"(\.p\d+)?\.parquet$", "", fn.rsplit("/", 1)[-1]), []).append(fn)
        packs, cur, n = [], [], 0
        for slug in sorted(by_slug):
            fns = by_slug[slug]; rows = sum(counts[f] for f in fns)
            if cur and n + rows > PART_ROWS: packs.append(cur); cur, n = [], 0
            cur += fns; n += rows
        if cur: packs.append(cur)
        print(f"{ats:16} {total_rows:,} rows -> {len(packs)} parts of <= {PART_ROWS:,}", flush=True)
    else: packs = [None]
    for pi, files in enumerate(packs):
        name = ats if files is None else f"{ats}.p{pi}"
        outs = {k: os.path.join(root, k, f"{name}.parquet") for k in ("jobs", "boards")}
        if files is not None and _published_recently(name):
            print(f"{name:16} published earlier this run; skipped", flush=True); continue
        fileset = None if files is None else set(files)
        bl = os.path.join(tmp, f"{name}.boards.jsonl")
        # a big board is written as parts (<slug>.parquet, <slug>.p1.parquet, ...), each with the same board_meta in its
        # footer: one boards row per slug, exported_jobs summed over its parts
        per_slug = {}
        for fn, meta_json in kv:
            if fileset is not None and fn not in fileset: continue
            try: meta = json.loads(meta_json)
            except Exception: meta = None
            if not isinstance(meta, dict): continue
            row = per_slug.setdefault(meta.get("slug"), {"ats": ats, "slug": meta.get("slug"), "meta": meta, "exported_jobs": 0, "error": None})
            row["exported_jobs"] += counts.get(fn, 0)
        with open(bl, "w") as bo:
            for row in per_slug.values(): bo.write(json.dumps(row) + "\n")
        bsrc = f"read_ndjson('{bl}', maximum_object_size=67108864)"
        # union_by_name: snapshot files are written over months and a board whose snapshot predates a schema change has
        # its columns in another order; a positional read then decodes a binary column as text (dark, 2026-09-09).
        jsrc = f"read_parquet('{pq}', union_by_name=true)" if files is None else f"read_parquet({files!r}, union_by_name=true)"
        con.execute(f"COPY ({BOARDS_SQL.format(src=bsrc)}) TO '{outs['boards']}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        os.remove(bl)
        try:
            # a transient storage error on the big read is retried here; only a repeatable failure goes to the per-file scan
            for attempt in range(4):
                try:
                    con.execute(f"COPY ({SNAPSHOT_JOBS_SQL.format(src=jsrc)}) TO '{outs['jobs']}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)"); break
                except Exception as e:
                    if attempt == 3 or "HTTP" not in str(e): raise
                    print(f"{name:16} bucket read failed ({str(e)[:100]}); retrying the scan in {20 * (attempt + 1)}s", flush=True); time.sleep(20 * (attempt + 1))
        except BaseException as e:
            # One bad snapshot (invalid UTF-8, a truncated upload) must not stop the night: find it, quarantine it, go on
            # without it, and say so. The board's next snapshot replaces the bad file; nothing here is permanent.
            print(f"{name:16} snapshot scan failed ({type(e).__name__}: {e!r}"[:200] + "); testing each file", flush=True)
            cand = files if files is not None else ([k for k, _, _ in r2.list(f"snapshots/{ats}/")] if from_r2 else sorted(glob.glob(pq)))
            good, bad = [], []
            for k in cand:
                one = k if (files is not None or not from_r2) else r2.url(k)
                try: con.execute(f"SELECT count(*) FROM ({SNAPSHOT_JOBS_SQL.format(src=f'read_parquet({one!r})')}) WHERE length(content) >= 0 AND length(title) >= 0 AND length(location) >= 0")
                except BaseException as e2: bad.append({"file": k, "error": f"{type(e2).__name__}: {e2!r}"[:200]}); print(f"  QUARANTINED {k}: {bad[-1]['error']}", flush=True); continue
                good.append(one)
            if not good: raise
            con.execute(f"COPY ({SNAPSHOT_JOBS_SQL.format(src=f'read_parquet({good!r}, union_by_name=true)')}) TO '{outs['jobs']}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)")
            qf = os.path.join(root, "quarantine.json"); q = json.load(open(qf)) if os.path.exists(qf) else []
            q += [{"ats": ats, **b} for b in bad]; json.dump(q, open(qf, "w"), indent=1)
            print(f"{name:16} WARNING: {len(bad)} snapshot file(s) skipped (see {qf}); the board's next snapshot replaces them", flush=True)
        finalize(ats, outs, name)

SUFFIX = r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|sas|sarl|srl|bv|nv|oy|ab|as|plc|co|corp|corporation|company|group|holding|holdings|kg|mbh|e\.?v\.?|se|s\.?p\.?a\.?|kk|k\.k\.)\b"
def dedup_aggregators():
    """Second-tier dedup, once every ATS is written: an aggregator posting whose (employer, title, location) a
    first-party board already carries is the same job seen through a job board; and the same job seen through
    several boards is kept once (earliest first seen). Employers of first-party rows come from boards/ (company
    name, else slug). Works over dark's part files: pass A picks the winners from every part's keys, pass B
    rewrites each part in place (pulling and pushing one part at a time under LOW_DISK) and re-publishes it."""
    low = os.environ.get("LOW_DISK") == "1" and publish and from_r2
    if low: names = sorted(k.rsplit("/", 1)[-1] for k, _, _ in r2.list(f"exports/{date_name}/jobs/") if re.match(r"dark(\.p\d+)?\.parquet$", k.rsplit("/", 1)[-1]))
    else: names = sorted(os.path.basename(f) for f in glob.glob(os.path.join(root, "jobs", "dark*.parquet")))
    if not names: return
    src = lambda n: r2.url(f"exports/{date_name}/jobs/{n}") if low else os.path.join(root, "jobs", n)
    con.execute(f"""CREATE OR REPLACE MACRO norm(s) AS trim(regexp_replace(regexp_replace(regexp_replace(lower(coalesce(s, '')), '[^a-z0-9 ]+', ' ', 'g'), '{SUFFIX}', ' ', 'g'), ' +', ' ', 'g'))""")
    con.execute("""CREATE OR REPLACE MACRO ntitle(s) AS trim(regexp_replace(regexp_replace(lower(coalesce(s, '')), '\\(.*?\\)|\\[.*?\\]|[^a-z0-9 ]+', ' ', 'g'), ' +', ' ', 'g'))""")
    allsrc = "[" + ", ".join(repr(src(n)) for n in names) + "]"
    before = con.execute(f"SELECT count(*) FILTER (tier = 'aggregator'), count(*) FROM read_parquet({allsrc}, union_by_name=true)").fetchone()
    if not before[0]: return
    J_ = r2.url(f"exports/{date_name}/jobs/*.parquet") if low else J; B_ = r2.url(f"exports/{date_name}/boards/*.parquet") if low else B
    con.execute(f"""CREATE OR REPLACE TABLE fp_keys AS
        SELECT DISTINCT norm(coalesce(b.company_name::VARCHAR, b.slug::VARCHAR)) AS org, ntitle(j.title) AS t, norm(j.location) AS loc
        FROM read_parquet('{J_}', union_by_name=true) j JOIN read_parquet('{B_}', union_by_name=true) b ON j.ats = b.ats::VARCHAR AND j.slug = b.slug::VARCHAR
        WHERE j.is_open AND coalesce(j.tier, 'first_party') = 'first_party'""")
    # pass A: one winner per (employer, title, location) across every part, earliest first seen; index-marked copies never win
    con.execute(f"""CREATE OR REPLACE TABLE winners AS
        SELECT ats, slug, id FROM (
          SELECT ats, slug, id, first_seen_at, norm(org) AS k_org, ntitle(title) AS k_t, norm(location) AS k_loc
          FROM read_parquet({allsrc}, union_by_name=true) WHERE tier = 'aggregator' AND coalesce(embed_status, '') != 'dup')
        WHERE NOT EXISTS (SELECT 1 FROM fp_keys f WHERE f.org = k_org AND f.t = k_t AND f.loc = k_loc)
        QUALIFY row_number() OVER (PARTITION BY k_org, k_t, k_loc ORDER BY first_seen_at, slug, id) = 1""")
    # pass B: rewrite each part keeping first-party rows and winning aggregator rows
    kept = 0
    for n in names:
        local = os.path.join(root, "jobs", n)
        if low and not os.path.exists(local): os.makedirs(os.path.dirname(local), exist_ok=True); r2.get_file(f"exports/{date_name}/jobs/{n}", local)
        tmpf = local + ".dedup"
        con.execute(f"""COPY (SELECT d.* FROM read_parquet('{local}') d
                        WHERE d.tier = 'first_party' OR EXISTS (SELECT 1 FROM winners w WHERE w.ats = d.ats AND w.slug = d.slug AND w.id = d.id))
                        TO '{tmpf}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 20000)""")
        os.replace(tmpf, local)
        kept += con.execute(f"SELECT count(*) FILTER (tier = 'aggregator') FROM read_parquet('{local}')").fetchone()[0]
        if publish: r2.put_file(f"exports/{date_name}/jobs/{n}", local, "application/octet-stream")
        if low: os.remove(local)
    print(f"aggregator tier: {before[0]:,} -> {kept:,} postings after dedup against first-party boards and across job boards ({len(names)} part file(s){'; re-published' if publish else ''})", flush=True)

def show(sql):
    con.sql(sql).show(max_rows=50, max_width=200)

B = os.path.join(root, "boards", "*.parquet")
J = os.path.join(root, "jobs", "*.parquet")
dedup_aggregators()
show(f"SELECT count(*) AS boards, count(*) FILTER (last_status='ok') AS ok, count(*) FILTER (last_status='gone') AS gone, count(*) FILTER (last_status='error') AS error, count(*) FILTER (last_status IS NULL) AS unfetched FROM read_parquet('{B}')")
show(f"SELECT ats, count(*) AS jobs, count(*) FILTER (is_open) AS open, count(*) FILTER (length(content) > 800) AS with_body, count(DISTINCT slug) AS boards FROM read_parquet('{J}') GROUP BY ats ORDER BY jobs DESC")
show(f"SELECT count(*) AS total_jobs, count(*) FILTER (is_open) AS open_jobs, count(*) FILTER (is_open AND length(content) > 800) AS open_with_body FROM read_parquet('{J}')")
size = sum(os.path.getsize(p) for p in glob.glob(J))
print(f"wrote export/jobs/*.parquet ({size / 1e6:.0f} MB) and export/boards/*.parquet")

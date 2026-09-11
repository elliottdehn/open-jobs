# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "boto3"]
# ///
"""Daily diff between the previous full export and today's, lossless: latest + diffs reconstructs any day.

  EXPORT_DIR=export/<date> uv run scripts/build-diff.py --prev export/<prev-date> [--base <worker>] [--no-verify] [--no-carry]

Writes export/diffs/<prev>__<date>/data_*.parquet (parts <= 200 MB: wrangler uploads are capped at 300 MiB), one row per
event with the full job record plus `op`:
  added         the new row (first day in the corpus)
  removed       the old row, in full (so a removed posting's text is never lost), with `removal` = closed
                (the crawler set removed_at; `removed_at_crawler` says when) | left_dataset (still open in the
                crawler: eligibility, dedup, or a filter rule moved it out of the export) | unknown
  changed       the new row of a posting whose title, location, url, text, embed_status, or published_at moved (not the crawler's
                content_hash, which also covers the raw provider payload and churns for ~370k Workday rows a day).
                The embed_status flip to done is the moment a job enters the public group files.
  changed_prev  its previous row
  carried       an old row carried forward into today's export because its whole board vanished from the pull
                without the crawler saying so (see below); not a removal
plus <prev>__<date>/lite/data_*.parquet: the same rows without embedding, raw_json, detail_raw_json, enrichment_json;
content is kept on added/changed rows only — everything a mirror needs to show a job, and none of the vector weight.
and export/diffs/<prev>__<date>.json with the counts, the boards involved, and `ok_to_prune`.

Boards that vanish: a board with rows yesterday and none today is either really empty now or missing from
today's pull. The crawler knows which: GET /boards/:ats/:slug (admin) reports jobCount. jobCount > 0, or
unreachable, means our pull lost it -> its rows are `carried` (appended to today's jobs/<ats>.parquet and
boards/<ats>.parquet so the index and tomorrow's diff see them). jobCount == 0 means it really emptied ->
`removed`. Without this, one bad pull would look like 50,000 postings closing and reopening the next day.
"""
import shutil, argparse, concurrent.futures, glob, hashlib, json, os, sys, time, urllib.parse, urllib.request
import duckdb
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2

ap = argparse.ArgumentParser()
ap.add_argument("--new", default=os.environ.get("EXPORT_DIR"), help="today's export dir (default $EXPORT_DIR)")
ap.add_argument("--prev", default="export/latest", help="previous full export (symlink ok)")
ap.add_argument("--out", default="export/diffs")
ap.add_argument("--base", default=os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev"))
ap.add_argument("--no-verify", action="store_true", help="don't ask the Worker about vanished boards: carry them all")
ap.add_argument("--no-carry", action="store_true", help="don't append carried rows to today's parquet (diff only)")
ap.add_argument("--max-absent", type=int, default=3000, help="more vanished boards than this = the pull is broken; carry all, no per-board calls")
a = ap.parse_args()
if not a.new: sys.exit("--new or EXPORT_DIR required")
# --prev / --new: local export dirs or s3://bucket/exports/<date> prefixes (read in place through DuckDB's S3 client)
def _root(p): return p.rstrip("/") if p.startswith("s3://") else os.path.realpath(p)
new, prev = _root(a.new), _root(a.prev)
nd, pd = new.rsplit("/", 1)[-1], prev.rsplit("/", 1)[-1]
r2 = R2() if (new.startswith("s3://") or prev.startswith("s3://")) else None
def _key(p): return p.split("/", 3)[3]
def _exists(p): return (r2.head(_key(p)) is not None) if p.startswith("s3://") else os.path.exists(p)
def _has_jobs(root):
    if root.startswith("s3://"): return any(k.endswith(".parquet") for k, _, _ in r2.list(_key(root) + "/jobs/"))
    return bool(glob.glob(os.path.join(root, "jobs", "*.parquet")))
if not _has_jobs(new): sys.exit(f"no jobs parquet in {new}")
if prev == new or not _has_jobs(prev):
    print(f"no previous export to diff against ({a.prev}); skipping"); sys.exit(0)
os.makedirs(a.out, exist_ok=True)
t0 = time.time()
con = duckdb.connect()
con.execute("SET TimeZone='UTC'")  # date-only posting dates cast to the session zone; the laptop (EDT) and the container (UTC) disagreed by 4 h on 2026-09-10
con.execute(f"SET memory_limit='{os.environ.get('DIFF_MEMORY', '20GB')}'"); con.execute("SET preserve_insertion_order=false"); con.execute(f"SET threads={os.environ.get('DIFF_THREADS', '8')}")  # stream the event write: no order buffering, bounded writer threads (OOMed at 6 GB on 2026-09-11 with defaults)
os.makedirs(os.path.join(a.out, ".tmp"), exist_ok=True); con.execute(f"SET temp_directory='{os.path.join(a.out, '.tmp')}'"); con.execute("SET preserve_insertion_order=false")
if r2: r2.duckdb(con)
con.execute(f"CREATE VIEW old AS SELECT * FROM read_parquet('{prev}/jobs/*.parquet', union_by_name=true)")
con.execute(f"CREATE VIEW new AS SELECT * FROM read_parquet('{new}/jobs/*.parquet', union_by_name=true)")
# Schema drift between the two days (a column added to the export, e.g. tier/org/via on 2026-09-10): the older
# side gets the new columns as typed NULLs so every SELECT below can name the same list on both views.
_newt = [(r[0], r[1]) for r in con.execute("DESCRIBE new").fetchall()]
_oldc = {r[0] for r in con.execute("DESCRIBE old").fetchall()}
_missing = [(c, t) for c, t in _newt if c not in _oldc]
if _missing:
    print(f"old export lacks {len(_missing)} column(s) of the new one ({', '.join(c for c, _ in _missing)}); treated as NULL")
    _extra = ", ".join('NULL::%s AS "%s"' % (t, c) for c, t in _missing)
    con.execute(f"CREATE OR REPLACE VIEW old AS SELECT *, {_extra} FROM read_parquet('{prev}/jobs/*.parquet', union_by_name=true)")
cols = [r[0] for r in con.execute("DESCRIBE new").fetchall()]
collist = ", ".join(f'"{c}"' for c in cols)
old_collist = ", ".join(f'o."{c}"' for c in cols)

# narrow key tables first; the wide rows are only touched once, in the final COPY
# "changed" = a visible field moved. Not content_hash: the crawler hashes the provider's raw payload too, and
# Workday alone re-stamps ~370k of those a day ("30+ days ago" strings, counters) with nothing a reader would notice.
# embed_status is part of the key too: a job that arrives unembedded enters the public corpus (the group files) only
# when it flips to done, and a mirror has to see that flip as an event or it never learns the job became visible.
# published_at too: a board re-stamping its posted date is both a visible change and the re-stamp signal.
con.execute("CREATE TABLE ok AS SELECT ats, slug, id, title, location, url, embed_status, published_at, md5(coalesce(content, '')) AS ch FROM old")
con.execute("CREATE TABLE nk AS SELECT ats, slug, id, title, location, url, embed_status, published_at, md5(coalesce(content, '')) AS ch FROM new")
n_old, n_new = con.execute("SELECT (SELECT count(*) FROM ok), (SELECT count(*) FROM nk)").fetchone()
con.execute("CREATE TABLE addk AS SELECT n.ats, n.slug, n.id FROM nk n WHERE NOT EXISTS (SELECT 1 FROM ok o WHERE o.ats=n.ats AND o.slug=n.slug AND o.id=n.id)")
con.execute("CREATE TABLE gonek AS SELECT o.ats, o.slug, o.id FROM ok o WHERE NOT EXISTS (SELECT 1 FROM nk n WHERE n.ats=o.ats AND n.slug=o.slug AND n.id=o.id)")
con.execute("CREATE TABLE chgk AS SELECT n.ats, n.slug, n.id FROM nk n JOIN ok o USING (ats, slug, id) WHERE n.title IS DISTINCT FROM o.title OR n.location IS DISTINCT FROM o.location OR n.url IS DISTINCT FROM o.url OR n.ch <> o.ch OR n.embed_status IS DISTINCT FROM o.embed_status OR CAST(n.published_at AT TIME ZONE 'UTC' AS DATE) IS DISTINCT FROM CAST(o.published_at AT TIME ZONE 'UTC' AS DATE)")
# boards with rows yesterday and none at all today
con.execute("""CREATE TABLE absent AS
  SELECT g.ats, g.slug, count(*) AS old_jobs FROM gonek g
  WHERE NOT EXISTS (SELECT 1 FROM nk n WHERE n.ats=g.ats AND n.slug=g.slug) GROUP BY 1, 2 ORDER BY 3 DESC""")
absent = con.execute("SELECT ats, slug, old_jobs FROM absent").fetchall()

# ask the crawler which vanished boards are really empty
token = os.environ.get("ADMIN_TOKEN") or (open("admin_token.txt").read().strip() if os.path.exists("admin_token.txt") else "")
verdict = {}  # (ats, slug) -> "carry" | "removed"; reason
def ask(b):
    ats, slug, n = b
    try:
        req = urllib.request.Request(f"{a.base}/boards/{ats}/{urllib.parse.quote(slug, safe='')}?ids=_&slim=1", headers={"authorization": f"Bearer {token}", "user-agent": "open-jobs-tools/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r: meta = (json.loads(r.read()) or {}).get("meta") or {}
        jc = meta.get("jobCount"); st = meta.get("lastStatus")
        if jc is None: return (ats, slug), ("carry", "no meta")
        return (ats, slug), (("carry", f"crawler still holds {jc} open (last status {st}); our pull missed it") if jc > 0 else ("removed", f"crawler agrees: 0 open (last status {st})"))
    except Exception as e:
        return (ats, slug), ("carry", f"unreachable: {str(e)[:60]}")
if absent:
    if a.no_verify or not token or len(absent) > a.max_absent:
        why = "--no-verify" if a.no_verify else "no ADMIN_TOKEN" if not token else f"{len(absent)} vanished boards > --max-absent {a.max_absent}: the pull looks broken"
        print(f"WARNING: carrying all {len(absent)} vanished boards without asking the crawler ({why})")
        for ats, slug, n in absent: verdict[(ats, slug)] = ("carry", why)
    else:
        with concurrent.futures.ThreadPoolExecutor(16) as ex:
            for k, v in ex.map(ask, absent): verdict[k] = v
carry_boards = [k for k, v in verdict.items() if v[0] == "carry"]
con.execute("CREATE TABLE carryb (ats VARCHAR, slug VARCHAR)")
if carry_boards: con.executemany("INSERT INTO carryb VALUES (?, ?)", carry_boards)
con.execute("CREATE TABLE remk AS SELECT g.* FROM gonek g WHERE NOT EXISTS (SELECT 1 FROM carryb c WHERE c.ats=g.ats AND c.slug=g.slug)")
con.execute("CREATE TABLE carryk AS SELECT g.* FROM gonek g WHERE EXISTS (SELECT 1 FROM carryb c WHERE c.ats=g.ats AND c.slug=g.slug)")
counts = dict(zip(["added", "removed", "changed", "carried"], con.execute("SELECT (SELECT count(*) FROM addk), (SELECT count(*) FROM remk), (SELECT count(*) FROM chgk), (SELECT count(*) FROM carryk)").fetchone()))

# Removal semantics, from the ledger built earlier in the same run (step 2b): a key the crawler has marked removed
# is `closed`; one the crawler still holds open left the export for another reason (eligibility, dedup, a filter
# rule) and is `left_dataset`; a key the ledger has never seen is `unknown`. Only `removed` rows carry a value.
ledger_dir = os.path.join(os.path.dirname(a.out), "ledger", nd)
have_ledger = bool(glob.glob(os.path.join(ledger_dir, "*.parquet")))
if have_ledger:
    con.execute(f"CREATE TABLE led AS SELECT l.ats, l.slug, l.id, l.is_open, l.removed_at FROM read_parquet('{ledger_dir}/*.parquet') l WHERE EXISTS (SELECT 1 FROM remk k WHERE k.ats=l.ats AND k.slug=l.slug AND k.id=l.id)")
else:
    con.execute("CREATE TABLE led (ats VARCHAR, slug VARCHAR, id VARCHAR, is_open BOOLEAN, removed_at TIMESTAMPTZ)")
    print("WARNING: no ledger for today; removed rows get removal = 'unknown'")
removal_sql = "CASE WHEN led.is_open = false THEN 'closed' WHEN led.is_open THEN 'left_dataset' ELSE 'unknown' END"
removal_counts = dict(con.execute(f"SELECT {removal_sql} AS removal, count(*) FROM remk k LEFT JOIN led USING (ats, slug, id) GROUP BY 1").fetchall())

# one file, every event with the full record
outd = os.path.join(a.out, f"{pd}__{nd}")
if os.path.isdir(outd): shutil.rmtree(outd)  # a killed earlier attempt leaves partial files DuckDB then refuses to write over
# The write scans both exports in full over the bucket (40+ min); a transient bucket error (403/5xx on one of the range
# reads, 2026-09-11) must not cost the key computation above, so only the COPY is retried.
# One labeled key table per side, so today's export is scanned once (added, changed) and yesterday's once (removed,
# changed_prev, carried) instead of one scan per event type, and each scan only opens the sources that have events.
con.execute("CREATE TABLE evk_new AS SELECT ats, slug, id, 'added' AS op FROM addk UNION ALL SELECT ats, slug, id, 'changed' FROM chgk")
con.execute("CREATE TABLE evk_old AS SELECT ats, slug, id, 'removed' AS op FROM remk UNION ALL SELECT ats, slug, id, 'changed_prev' FROM chgk UNION ALL SELECT ats, slug, id, 'carried' FROM carryk")
def _ats_in(t):
    v = [r[0] for r in con.execute(f"SELECT DISTINCT ats FROM {t} ORDER BY 1").fetchall()]
    return "(" + ", ".join("'%s'" % x.replace("'", "''") for x in v) + ")" if v else "('')"
ats_new, ats_old = _ats_in("evk_new"), _ats_in("evk_old")
new_collist = ", ".join(f'n."{c}"' for c in cols)
for _attempt in range(4):
    try:
        con.execute(f"""COPY (
    SELECT k.op, '{pd}' AS from_date, '{nd}' AS to_date, NULL::VARCHAR AS removal, NULL::TIMESTAMPTZ AS removed_at_crawler, {new_collist}
    FROM new n JOIN evk_new k ON k.ats=n.ats AND k.slug=n.slug AND k.id=n.id WHERE n.ats IN {ats_new}
    UNION ALL
    SELECT k.op, '{pd}', '{nd}', CASE WHEN k.op = 'removed' THEN {removal_sql} END, CASE WHEN k.op = 'removed' THEN led.removed_at END, {old_collist}
    FROM old o JOIN evk_old k ON k.ats=o.ats AND k.slug=o.slug AND k.id=o.id LEFT JOIN led ON led.ats=o.ats AND led.slug=o.slug AND led.id=o.id WHERE o.ats IN {ats_old}
) TO '{outd}' (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '200MB', ROW_GROUP_SIZE 20000)""")
        break
    except duckdb.Error as e:
        if _attempt == 3 or not isinstance(e, (duckdb.HTTPException, duckdb.IOException)): raise
        print(f"WARNING: diff write failed ({str(e)[:160]}); retrying in 60s ({_attempt + 1}/3)", flush=True)
        shutil.rmtree(outd, ignore_errors=True); time.sleep(60)
parts = sorted(glob.glob(os.path.join(outd, "*.parquet"))); out_bytes = sum(os.path.getsize(f) for f in parts)
# lite projection: the same events without the vector or the raw provider / enrichment JSON. The description text
# stays on `added` and `changed` rows (a mirror has to be able to show a new job) and is dropped where only the key
# matters (removed, changed_prev, carried). Full parts remain the record.
LITE_DROP = "raw_json, detail_raw_json, enrichment_json, embedding"
lited = os.path.join(outd, "lite"); os.makedirs(lited, exist_ok=True)
for f in glob.glob(os.path.join(lited, "*.parquet")): os.remove(f)
con.execute(f"""COPY (SELECT * EXCLUDE ({LITE_DROP}) REPLACE (CASE WHEN op IN ('added', 'changed') THEN content END AS content) FROM read_parquet('{outd}/*.parquet'))
  TO '{lited}' (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '200MB')""")
lparts = sorted(glob.glob(os.path.join(lited, "*.parquet"))); lite_bytes = sum(os.path.getsize(f) for f in lparts)

# Integrity: sha256 per part, a content hash over the full parts, and the parent diff's content hash, so a chain
# of diffs breaks loudly if a file goes missing or is truncated instead of replaying something plausible and wrong.
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()
part_hashes = {os.path.basename(f): sha256_of(f) for f in parts}
lite_hashes = {os.path.basename(f): sha256_of(f) for f in lparts}
content_sha256 = hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(part_hashes.items())).encode()).hexdigest()
parent = None
cands = sorted(glob.glob(os.path.join(a.out, f"*__{pd}.json")))
if cands:
    cand = cands[-1]
    pj = json.load(open(cand)); parent = {"diff": os.path.basename(cand)[:-5], "content_sha256": pj.get("content_sha256")}
else:
    # a fresh volume (the container) has no local sidecars: the previous diff lives in the bucket. A diff without
    # its parent link breaks the chain for every mirror (2026-09-10: the feed refused to build on it).
    try:
        from r2 import R2
        r2 = R2(); keys = sorted(k for k, _, _ in r2.list("diffs/") if k.endswith(f"__{pd}.json") and k.count("/") == 1)
        if keys:
            import io
            pj = json.load(io.BytesIO(r2.client.get_object(Bucket=r2.bucket, Key=keys[-1])["Body"].read()))
            parent = {"diff": keys[-1].split("/")[-1][:-5], "content_sha256": pj.get("content_sha256")}
            print(f"parent diff from the bucket: {parent['diff']}", flush=True)
    except Exception as e:
        print(f"WARNING: could not find the parent diff for {pd} locally or in the bucket ({e}); the sidecar will have parent=null", flush=True)

n_new_final = n_new + counts["carried"]
ok_to_prune = n_new_final >= 0.9 * n_old and counts["removed"] <= 0.15 * n_old
side = {"schema_version": 2, "from": pd, "to": nd, "old_jobs": n_old, "new_jobs": n_new, "new_jobs_after_carry": n_new_final, "counts": counts,
        "removal": removal_counts, "change_key": ["title", "location", "url", "content", "embed_status", "published_at"],
        "content_sha256": content_sha256, "parent": parent,
        "vanished_boards": [{"ats": k[0], "slug": k[1], "old_jobs": n, "verdict": verdict[(k[0], k[1])][0], "why": verdict[(k[0], k[1])][1]} for k, n in [((x[0], x[1]), x[2]) for x in absent]],
        "carried_into": [], "carry_done": False, "ok_to_prune": ok_to_prune, "seconds": round(time.time() - t0),
        "dir": os.path.relpath(outd), "parts": [{"file": os.path.basename(f), "bytes": os.path.getsize(f), "sha256": part_hashes[os.path.basename(f)]} for f in parts], "bytes": out_bytes,
        "lite": {"dir": os.path.relpath(lited), "drops": LITE_DROP.split(", "), "content_on": ["added", "changed"], "parts": [{"file": os.path.basename(f), "bytes": os.path.getsize(f), "sha256": lite_hashes[os.path.basename(f)]} for f in lparts], "bytes": lite_bytes}}
json.dump(side, open(outd + ".json", "w"), indent=1)

# carry vanished-but-not-empty boards forward into today's export, so the index and tomorrow's diff keep them.
# Slugs are compared as text: a provider whose slugs are all digits gets a numeric slug column in boards/*.parquet.
carried_files = []
if carry_boards and not a.no_carry:
    for ats in sorted({k[0] for k in carry_boards}):
        jp = f"{new}/jobs/{ats}.parquet"; tmp = (jp if not new.startswith("s3://") else os.path.join(a.out, ".tmp", f"carry-{ats}.parquet")) + ".tmp"
        if _exists(jp):
            con.execute(f"""COPY (SELECT * FROM read_parquet('{jp}')
              UNION ALL BY NAME SELECT {collist} FROM old o WHERE o.ats='{ats}' AND EXISTS (SELECT 1 FROM carryk k WHERE k.ats=o.ats AND k.slug=CAST(o.slug AS VARCHAR) AND k.id=CAST(o.id AS VARCHAR))
              ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        else:
            con.execute(f"""COPY (SELECT {collist} FROM old o WHERE o.ats='{ats}' AND EXISTS (SELECT 1 FROM carryk k WHERE k.ats=o.ats AND k.slug=CAST(o.slug AS VARCHAR) AND k.id=CAST(o.id AS VARCHAR))) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        if jp.startswith("s3://"): r2.put_file(_key(jp), tmp); os.remove(tmp)
        else: os.replace(tmp, jp)
        carried_files.append(jp)
        bp_old, bp = f"{prev}/boards/{ats}.parquet", f"{new}/boards/{ats}.parquet"
        if _exists(bp_old):
            tmpb = (bp if not new.startswith("s3://") else os.path.join(a.out, ".tmp", f"carryb-{ats}.parquet")) + ".tmp"
            base = f"SELECT * REPLACE (CAST(slug AS VARCHAR) AS slug) FROM read_parquet('{bp}') UNION ALL BY NAME " if _exists(bp) else ""
            con.execute(f"""COPY ({base} SELECT b.* REPLACE (CAST(b.slug AS VARCHAR) AS slug) FROM read_parquet('{bp_old}') b WHERE EXISTS (SELECT 1 FROM carryb c WHERE c.ats=b.ats AND c.slug=CAST(b.slug AS VARCHAR))
              {"AND NOT EXISTS (SELECT 1 FROM read_parquet('" + bp + "') x WHERE x.ats=b.ats AND CAST(x.slug AS VARCHAR)=CAST(b.slug AS VARCHAR))" if _exists(bp) else ""}) TO '{tmpb}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
            if bp.startswith("s3://"): r2.put_file(_key(bp), tmpb); os.remove(tmpb)
            else: os.replace(tmpb, bp)

side.update({"carried_into": [p[len(new) + 1:] for p in carried_files], "carry_done": True, "seconds": round(time.time() - t0)})
json.dump(side, open(outd + ".json", "w"), indent=1)
pct = lambda n: f"{100 * n / max(1, n_old):.2f}%"
print(f"diff {pd} -> {nd}: {n_old:,} -> {n_new:,} jobs; added {counts['added']:,} ({pct(counts['added'])}), removed {counts['removed']:,} ({pct(counts['removed'])}), changed {counts['changed']:,}, carried {counts['carried']:,} from {len(carry_boards)} vanished board(s) [{len(absent) - len(carry_boards)} really emptied]; {out_bytes / 1e6:.0f} MB in {len(parts)} part(s) + lite {lite_bytes / 1e6:.0f} MB, {time.time() - t0:.0f}s -> {os.path.relpath(outd)}/")
if not ok_to_prune: print(f"WARNING: not ok_to_prune (new {n_new_final:,} vs old {n_old:,}, removed {pct(counts['removed'])}); the previous full export will be kept")

# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""Daily diff between the previous full export and today's, lossless: latest + diffs reconstructs any day.

  EXPORT_DIR=export/<date> uv run scripts/build-diff.py --prev export/<prev-date> [--base <worker>] [--no-verify] [--no-carry]

Writes export/diffs/<prev>__<date>/data_*.parquet (parts <= 200 MB: wrangler uploads are capped at 300 MiB), one row per
event with the full job record plus `op`:
  added         the new row (first day in the corpus)
  removed       the old row, in full (so a removed posting's text is never lost)
  changed       the new row of a posting whose title, location, url, text, or embed_status moved (not the crawler's
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
import argparse, concurrent.futures, glob, json, os, sys, time, urllib.parse, urllib.request
import duckdb

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
new, prev = os.path.abspath(a.new), os.path.realpath(a.prev)
nd, pd = os.path.basename(new), os.path.basename(prev)
if not glob.glob(os.path.join(new, "jobs", "*.parquet")): sys.exit(f"no jobs parquet in {new}")
if prev == new or not glob.glob(os.path.join(prev, "jobs", "*.parquet")):
    print(f"no previous export to diff against ({a.prev}); skipping"); sys.exit(0)
os.makedirs(a.out, exist_ok=True)
t0 = time.time()
con = duckdb.connect()
con.execute("SET memory_limit='20GB'"); con.execute(f"SET temp_directory='{os.path.join(a.out, '.tmp')}'"); con.execute("SET preserve_insertion_order=false")
con.execute(f"CREATE VIEW old AS SELECT * FROM read_parquet('{prev}/jobs/*.parquet', union_by_name=true)")
con.execute(f"CREATE VIEW new AS SELECT * FROM read_parquet('{new}/jobs/*.parquet', union_by_name=true)")
cols = [r[0] for r in con.execute("DESCRIBE new").fetchall()]
collist = ", ".join(f'"{c}"' for c in cols)

# narrow key tables first; the wide rows are only touched once, in the final COPY
# "changed" = a visible field moved. Not content_hash: the crawler hashes the provider's raw payload too, and
# Workday alone re-stamps ~370k of those a day ("30+ days ago" strings, counters) with nothing a reader would notice.
# embed_status is part of the key too: a job that arrives unembedded enters the public corpus (the group files) only
# when it flips to done, and a mirror has to see that flip as an event or it never learns the job became visible.
con.execute("CREATE TABLE ok AS SELECT ats, slug, id, title, location, url, embed_status, md5(coalesce(content, '')) AS ch FROM old")
con.execute("CREATE TABLE nk AS SELECT ats, slug, id, title, location, url, embed_status, md5(coalesce(content, '')) AS ch FROM new")
n_old, n_new = con.execute("SELECT (SELECT count(*) FROM ok), (SELECT count(*) FROM nk)").fetchone()
con.execute("CREATE TABLE addk AS SELECT n.ats, n.slug, n.id FROM nk n WHERE NOT EXISTS (SELECT 1 FROM ok o WHERE o.ats=n.ats AND o.slug=n.slug AND o.id=n.id)")
con.execute("CREATE TABLE gonek AS SELECT o.ats, o.slug, o.id FROM ok o WHERE NOT EXISTS (SELECT 1 FROM nk n WHERE n.ats=o.ats AND n.slug=o.slug AND n.id=o.id)")
con.execute("CREATE TABLE chgk AS SELECT n.ats, n.slug, n.id FROM nk n JOIN ok o USING (ats, slug, id) WHERE n.title IS DISTINCT FROM o.title OR n.location IS DISTINCT FROM o.location OR n.url IS DISTINCT FROM o.url OR n.ch <> o.ch OR n.embed_status IS DISTINCT FROM o.embed_status")
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

# one file, every event with the full record
outd = os.path.join(a.out, f"{pd}__{nd}")
if os.path.isdir(outd):
    for f in glob.glob(os.path.join(outd, "*.parquet")): os.remove(f)
con.execute(f"""COPY (
  SELECT 'added' AS op, '{pd}' AS from_date, '{nd}' AS to_date, {collist} FROM new n WHERE EXISTS (SELECT 1 FROM addk k WHERE k.ats=n.ats AND k.slug=n.slug AND k.id=n.id)
  UNION ALL SELECT 'removed', '{pd}', '{nd}', {collist} FROM old o WHERE EXISTS (SELECT 1 FROM remk k WHERE k.ats=o.ats AND k.slug=o.slug AND k.id=o.id)
  UNION ALL SELECT 'changed', '{pd}', '{nd}', {collist} FROM new n WHERE EXISTS (SELECT 1 FROM chgk k WHERE k.ats=n.ats AND k.slug=n.slug AND k.id=n.id)
  UNION ALL SELECT 'changed_prev', '{pd}', '{nd}', {collist} FROM old o WHERE EXISTS (SELECT 1 FROM chgk k WHERE k.ats=o.ats AND k.slug=o.slug AND k.id=o.id)
  UNION ALL SELECT 'carried', '{pd}', '{nd}', {collist} FROM old o WHERE EXISTS (SELECT 1 FROM carryk k WHERE k.ats=o.ats AND k.slug=o.slug AND k.id=o.id)
) TO '{outd}' (FORMAT PARQUET, COMPRESSION ZSTD, FILE_SIZE_BYTES '200MB', ROW_GROUP_SIZE 20000)""")
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

n_new_final = n_new + counts["carried"]
ok_to_prune = n_new_final >= 0.9 * n_old and counts["removed"] <= 0.15 * n_old
side = {"from": pd, "to": nd, "old_jobs": n_old, "new_jobs": n_new, "new_jobs_after_carry": n_new_final, "counts": counts,
        "vanished_boards": [{"ats": k[0], "slug": k[1], "old_jobs": n, "verdict": verdict[(k[0], k[1])][0], "why": verdict[(k[0], k[1])][1]} for k, n in [((x[0], x[1]), x[2]) for x in absent]],
        "carried_into": [], "carry_done": False, "ok_to_prune": ok_to_prune, "seconds": round(time.time() - t0),
        "dir": os.path.relpath(outd), "parts": [{"file": os.path.basename(f), "bytes": os.path.getsize(f)} for f in parts], "bytes": out_bytes,
        "lite": {"dir": os.path.relpath(lited), "drops": LITE_DROP.split(", "), "content_on": ["added", "changed"], "parts": [{"file": os.path.basename(f), "bytes": os.path.getsize(f)} for f in lparts], "bytes": lite_bytes}}
json.dump(side, open(outd + ".json", "w"), indent=1)

# carry vanished-but-not-empty boards forward into today's export, so the index and tomorrow's diff keep them.
# Slugs are compared as text: a provider whose slugs are all digits gets a numeric slug column in boards/*.parquet.
carried_files = []
if carry_boards and not a.no_carry:
    for ats in sorted({k[0] for k in carry_boards}):
        jp = os.path.join(new, "jobs", f"{ats}.parquet"); tmp = jp + ".tmp"
        if os.path.exists(jp):
            con.execute(f"""COPY (SELECT * FROM read_parquet('{jp}')
              UNION ALL BY NAME SELECT {collist} FROM old o WHERE o.ats='{ats}' AND EXISTS (SELECT 1 FROM carryk k WHERE k.ats=o.ats AND k.slug=CAST(o.slug AS VARCHAR) AND k.id=CAST(o.id AS VARCHAR))
              ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        else:
            con.execute(f"""COPY (SELECT {collist} FROM old o WHERE o.ats='{ats}' AND EXISTS (SELECT 1 FROM carryk k WHERE k.ats=o.ats AND k.slug=CAST(o.slug AS VARCHAR) AND k.id=CAST(o.id AS VARCHAR))) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        os.replace(tmp, jp); carried_files.append(jp)
        bp_old, bp = os.path.join(prev, "boards", f"{ats}.parquet"), os.path.join(new, "boards", f"{ats}.parquet")
        if os.path.exists(bp_old):
            tmpb = bp + ".tmp"
            base = f"SELECT * REPLACE (CAST(slug AS VARCHAR) AS slug) FROM read_parquet('{bp}') UNION ALL BY NAME " if os.path.exists(bp) else ""
            con.execute(f"""COPY ({base} SELECT b.* REPLACE (CAST(b.slug AS VARCHAR) AS slug) FROM read_parquet('{bp_old}') b WHERE EXISTS (SELECT 1 FROM carryb c WHERE c.ats=b.ats AND c.slug=CAST(b.slug AS VARCHAR))
              {"AND NOT EXISTS (SELECT 1 FROM read_parquet('" + bp + "') x WHERE x.ats=b.ats AND CAST(x.slug AS VARCHAR)=CAST(b.slug AS VARCHAR))" if os.path.exists(bp) else ""}) TO '{tmpb}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
            os.replace(tmpb, bp)

side.update({"carried_into": [os.path.relpath(p, new) for p in carried_files], "carry_done": True, "seconds": round(time.time() - t0)})
json.dump(side, open(outd + ".json", "w"), indent=1)
pct = lambda n: f"{100 * n / max(1, n_old):.2f}%"
print(f"diff {pd} -> {nd}: {n_old:,} -> {n_new:,} jobs; added {counts['added']:,} ({pct(counts['added'])}), removed {counts['removed']:,} ({pct(counts['removed'])}), changed {counts['changed']:,}, carried {counts['carried']:,} from {len(carry_boards)} vanished board(s) [{len(absent) - len(carry_boards)} really emptied]; {out_bytes / 1e6:.0f} MB in {len(parts)} part(s) + lite {lite_bytes / 1e6:.0f} MB, {time.time() - t0:.0f}s -> {os.path.relpath(outd)}/")
if not ok_to_prune: print(f"WARNING: not ok_to_prune (new {n_new_final:,} vs old {n_old:,}, removed {pct(counts['removed'])}); the previous full export will be kept")

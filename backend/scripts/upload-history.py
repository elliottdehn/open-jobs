"""Publish the history files to R2 so they are public at /data/diffs/* and /data/ledger/* (consolidate.sh step 5b).

  python3 scripts/upload-history.py [--diffs export/diffs] [--ledger export/ledger] [--force]

Layout on disk (build-diff.py / build-ledger.py write parts <= 200 MB because wrangler caps an upload at 300 MiB):
  export/diffs/<prev>__<date>/data_N.parquet  + export/diffs/<prev>__<date>.json (sidecar)
  export/ledger/<date>/data_N.parquet
Uploads every part and sidecar that has no marker under export/.uploaded-history/, then rewrites and uploads
diffs/index.json and ledger/index.json: the public lists of what is available, with parts and sidecar counts,
because the bucket listing itself is admin-only. Markers are per file, so a re-run only uploads what is new.
"""
import argparse, glob, json, os, subprocess, sys, time
ap = argparse.ArgumentParser()
ap.add_argument("--diffs", default="export/diffs"); ap.add_argument("--ledger", default="export/ledger")
ap.add_argument("--marks", default="export/.uploaded-history"); ap.add_argument("--force", action="store_true", help="re-upload everything")
a = ap.parse_args()
B = "jobscream-data"; os.makedirs(a.marks, exist_ok=True)

def put(key, path, ctype):
    for attempt in range(4):
        r = subprocess.run(["npx", "wrangler", "r2", "object", "put", f"{B}/{key}", "--file", path, "--content-type", ctype, "--remote"], capture_output=True, text=True)
        if r.returncode == 0: return True
        time.sleep(3 * (attempt + 1))
    print(f"FAILED {key}: {r.stderr[-200:]}", flush=True); return False

def mark_of(key): return os.path.join(a.marks, key.replace("/", "__"))
stats = {"uploaded": 0, "failed": 0}
def sync(key, path, ctype):
    if os.path.exists(mark_of(key)) and not a.force: return True
    t0 = time.time(); ok = put(key, path, ctype)
    if ok: open(mark_of(key), "w").close(); stats["uploaded"] += 1; print(f"  {key} ({os.path.getsize(path) / 1e6:.0f} MB, {time.time() - t0:.0f}s)", flush=True)
    else: stats["failed"] += 1
    return ok
def uploaded(key): return os.path.exists(mark_of(key))

# diffs: every complete directory of parts + its sidecar
def final(d):
    """A diff is final when its sidecar exists and says the carry step completed. build-diff.py writes the sidecar
    before the carry (carry_done=false) and again after; a run that died in between must be re-run, not published."""
    try: return json.load(open(d + ".json")).get("carry_done") is True
    except Exception: return False
diff_dirs = sorted(d for d in glob.glob(os.path.join(a.diffs, "*__*")) if os.path.isdir(d))
for d in diff_dirs:
    name = os.path.basename(d)
    if not final(d): print(f"  skip diffs/{name}: not final (no sidecar, or carry_done != true); re-run build-diff.py", flush=True); continue
    for part in sorted(glob.glob(os.path.join(d, "*.parquet"))): sync(f"diffs/{name}/{os.path.basename(part)}", part, "application/octet-stream")
    for part in sorted(glob.glob(os.path.join(d, "lite", "*.parquet"))): sync(f"diffs/{name}/lite/{os.path.basename(part)}", part, "application/octet-stream")
    sync(f"diffs/{name}.json", d + ".json", "application/json")
ledger_dirs = sorted(d for d in glob.glob(os.path.join(a.ledger, "20*")) if os.path.isdir(d))
for d in ledger_dirs:
    name = os.path.basename(d)
    for part in sorted(glob.glob(os.path.join(d, "*.parquet"))): sync(f"ledger/{name}/{os.path.basename(part)}", part, "application/octet-stream")

# public indexes: only entries whose every part is up
def side(d):
    try: return {k: v for k, v in json.load(open(d + ".json")).items() if k in ("old_jobs", "new_jobs", "new_jobs_after_carry", "counts", "ok_to_prune")}
    except Exception: return {}
def entry(prefix, d, extra):
    name = os.path.basename(d); parts = sorted(glob.glob(os.path.join(d, "*.parquet")))
    if not parts or not all(uploaded(f"{prefix}/{name}/{os.path.basename(p)}") for p in parts): return None
    if prefix == "diffs" and not (final(d) and uploaded(f"diffs/{name}.json")): return None
    e = {"dir": f"{prefix}/{name}/", "parts": [{"file": os.path.basename(p), "bytes": os.path.getsize(p)} for p in parts], "bytes": sum(os.path.getsize(p) for p in parts), **extra(name, d)}
    lparts = sorted(glob.glob(os.path.join(d, "lite", "*.parquet")))
    if prefix == "diffs" and lparts and all(uploaded(f"diffs/{name}/lite/{os.path.basename(p)}") for p in lparts):
        e["lite"] = {"dir": f"diffs/{name}/lite/", "parts": [{"file": os.path.basename(p), "bytes": os.path.getsize(p)} for p in lparts], "bytes": sum(os.path.getsize(p) for p in lparts), "drops": ["content", "raw_json", "detail_raw_json", "enrichment_json", "embedding"]}
    return e
diffs_index = {"built_at": int(time.time() * 1000), "base": "/data/",
               "note": "one row per event with the full job record; op = added | removed | changed | changed_prev | carried; from/to are the two consecutive full exports. Read every part of a dir together. `lite` has the same rows without text, raw JSON, or the vector.",
               "entries": [e for e in (entry("diffs", d, lambda n, d: {"from": n.split("__")[0], "to": n.split("__")[1], "sidecar": f"diffs/{n}.json", **side(d)}) for d in diff_dirs) if e]}
ledger_index = {"built_at": int(time.time() * 1000), "base": "/data/",
                "note": "every job the crawler has recorded, open or removed, with first_seen_at / last_seen_at / changed_at / removed_at; no text, no vectors. Read every part of a dir together.",
                "entries": [e for e in (entry("ledger", d, lambda n, d: {"date": n}) for d in ledger_dirs) if e]}
ok = True
for name, idx in (("diffs/index.json", diffs_index), ("ledger/index.json", ledger_index)):
    tmp = mark_of(name) + ".body"; json.dump(idx, open(tmp, "w"), indent=1)
    ok &= put(name, tmp, "application/json")
print(f"history: uploaded {stats['uploaded']} file(s), {stats['failed']} failed; indexes list {len(diffs_index['entries'])} diff(s) and {len(ledger_index['entries'])} ledger day(s)", flush=True)
if stats["failed"] or not ok: sys.exit("some history uploads failed; re-run to retry")

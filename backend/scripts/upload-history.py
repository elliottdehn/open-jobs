# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3"]
# ///
"""Publish the history files to R2 so they are public at /data/diffs/* and /data/ledger/* (consolidate.sh step 5b).

  uv run scripts/upload-history.py [--diffs export/diffs] [--ledger export/ledger] [--force]

Uploads go through the S3 API (scripts/r2.py, multipart), so there is no per-object size cap; parts are still
<= 200 MB because readers benefit from bounded files.
Layout on disk:
  export/diffs/<prev>__<date>/data_N.parquet  + export/diffs/<prev>__<date>.json (sidecar)
  export/ledger/<date>/data_N.parquet
Uploads every part and sidecar that has no marker under export/.uploaded-history/, then rewrites and uploads
diffs/index.json and ledger/index.json: the public lists of what is available, with parts and sidecar counts,
because the bucket listing itself is admin-only. Markers are per file, so a re-run only uploads what is new.
"""
import argparse, glob, hashlib, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2
ap = argparse.ArgumentParser()
ap.add_argument("--diffs", default="export/diffs"); ap.add_argument("--ledger", default="export/ledger")
ap.add_argument("--marks", default="export/.uploaded-history"); ap.add_argument("--force", action="store_true", help="re-upload everything")
a = ap.parse_args()
os.makedirs(a.marks, exist_ok=True)
r2 = R2()

def put(key, path, ctype):
    try: r2.put_file(key, path, ctype); return True
    except Exception as e: print(f"FAILED {key}: {str(e)[-200:]}", flush=True); return False

def mark_of(key): return os.path.join(a.marks, key.replace("/", "__"))
stats = {"uploaded": 0, "failed": 0}
def sync(key, path, ctype):
    if os.path.exists(mark_of(key)) and not a.force: return True
    t0 = time.time(); ok = put(key, path, ctype)
    if ok: open(mark_of(key), "w").close(); stats["uploaded"] += 1; print(f"  {key} ({os.path.getsize(path) / 1e6:.0f} MB, {time.time() - t0:.0f}s)", flush=True)
    else: stats["failed"] += 1
    return ok
def uploaded(key): return os.path.exists(mark_of(key))
def sha256_of(path):
    """Cached per file under the marks dir; the sidecar's hash wins when present."""
    c = mark_of("sha256__" + path.replace("/", "__"))
    if os.path.exists(c) and os.path.getmtime(c) >= os.path.getmtime(path): return open(c).read().strip()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    open(c, "w").write(h.hexdigest()); return h.hexdigest()
RETENTION = ("diffs/ and ledger/ are kept indefinitely and never rewritten once listed here. The full export behind them is "
             "overwritten daily (export/latest), and the group files under groups/ are rewritten daily under the same names, "
             "so a mirror bootstraps from groups/ once and replays diffs from that day on; a diff's parent chain must be unbroken.")

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
    try: return {k: v for k, v in json.load(open(d + ".json")).items() if k in ("old_jobs", "new_jobs", "new_jobs_after_carry", "counts", "ok_to_prune", "content_sha256", "parent")}
    except Exception: return {}
def side_hashes(d, sub=""):
    try:
        j = json.load(open(d + ".json")); lst = (j.get("lite") or {}).get("parts") if sub else j.get("parts")
        return {p["file"]: p.get("sha256") for p in (lst or []) if p.get("sha256")}
    except Exception: return {}
def entry(prefix, d, extra):
    name = os.path.basename(d); parts = sorted(glob.glob(os.path.join(d, "*.parquet")))
    if not parts or not all(uploaded(f"{prefix}/{name}/{os.path.basename(p)}") for p in parts): return None
    if prefix == "diffs" and not (final(d) and uploaded(f"diffs/{name}.json")): return None
    fh = side_hashes(d) if prefix == "diffs" else {}
    e = {"dir": f"{prefix}/{name}/", "parts": [{"file": os.path.basename(p), "bytes": os.path.getsize(p), "sha256": fh.get(os.path.basename(p)) or sha256_of(p)} for p in parts], "bytes": sum(os.path.getsize(p) for p in parts), **extra(name, d)}
    lparts = sorted(glob.glob(os.path.join(d, "lite", "*.parquet")))
    if prefix == "diffs" and lparts and all(uploaded(f"diffs/{name}/lite/{os.path.basename(p)}") for p in lparts):
        lh = side_hashes(d, "lite")
        e["lite"] = {"dir": f"diffs/{name}/lite/", "parts": [{"file": os.path.basename(p), "bytes": os.path.getsize(p), "sha256": lh.get(os.path.basename(p)) or sha256_of(p)} for p in lparts], "bytes": sum(os.path.getsize(p) for p in lparts), "drops": ["raw_json", "detail_raw_json", "enrichment_json", "embedding"], "content_on": ["added", "changed"]}
    return e
def snapshot_built_at():
    try: return json.load(open("export/latest/web/manifest.json")).get("built_at")
    except Exception: return None
latest_date = os.path.basename(os.path.realpath("export/latest")) if os.path.exists("export/latest") else None
diffs_index = {"schema_version": 2, "built_at": int(time.time() * 1000), "base": "/data/", "retention": RETENTION,
               "head": latest_date, "snapshot_built_at": snapshot_built_at(),
               "bootstrap": "groups/ and manifest.json are the export named by `head` when manifest.built_at == snapshot_built_at. Bootstrap from them, record head, then apply every diff whose `from` == your head, in order, verifying each part's sha256 and each diff's parent. History is never expired; a broken chain means re-bootstrap.",
               "note": "one row per event with the full job record; op = added | removed | changed | changed_prev | carried; from/to are the two consecutive full exports. Read every part of a dir together. `lite` has the same rows without the vector or raw JSON; description text is kept on added and changed rows.",
               "entries": [e for e in (entry("diffs", d, lambda n, d: {"from": n.split("__")[0], "to": n.split("__")[1], "sidecar": f"diffs/{n}.json", **side(d)}) for d in diff_dirs) if e]}
ledger_index = {"schema_version": 1, "built_at": int(time.time() * 1000), "base": "/data/", "retention": RETENTION,
                "note": "every job the crawler has recorded, open or removed, with first_seen_at / last_seen_at / changed_at / removed_at; no text, no vectors. Read every part of a dir together.",
                "entries": [e for e in (entry("ledger", d, lambda n, d: {"date": n}) for d in ledger_dirs) if e]}
ok = True
for name, idx in (("diffs/index.json", diffs_index), ("ledger/index.json", ledger_index)):
    tmp = mark_of(name) + ".body"; json.dump(idx, open(tmp, "w"), indent=1)
    ok &= put(name, tmp, "application/json")
print(f"history: uploaded {stats['uploaded']} file(s), {stats['failed']} failed; indexes list {len(diffs_index['entries'])} diff(s) and {len(ledger_index['entries'])} ledger day(s)", flush=True)
if stats["failed"] or not ok: sys.exit("some history uploads failed; re-run to retry")

# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3"]
# ///
"""Finalize stage: make R2 match the built web/ directory, then publish the pointers, in an order a reader
can never see half of.

  EXPORT_DIR=export/<date> uv run scripts/publish-web.py [--web <dir>] [--workers 8]

1. groups/: the tree build published each group file as it was written (--publish); here every local group
   file is checked against R2 by size and re-uploaded if missing or different (the reconcile), so a failed
   background upload never reaches the manifest.
2. estimator JSONs (salary, arrangement, seniority, age, location table), then centroids.bin, then
   manifest.json last: a reader that sees the new manifest finds every group it references.
Replaces upload-web.py (wrangler, 300 MiB cap, crash-and-retry).
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2, Uploader

ap = argparse.ArgumentParser()
ap.add_argument("--web", default=os.path.join(os.environ.get("WORK_DIR") or os.environ.get("EXPORT_DIR", "export"), "web"))
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--groups-prefix", default=os.environ.get("GROUPS_PREFIX", "groups/"))
ap.add_argument("--mirror-prefix", default=os.environ.get("GROUPS_MIRROR_PREFIX", "groups/"), help="after the manifest: server-side copy this build's group files here for readers that hardcode the flat path (old checkouts); '' to skip")
ap.add_argument("--root-prefix", default=os.environ.get("ROOT_PREFIX", ""), help="key prefix for the models, centroids, and manifest (tests only; production is the bucket root)")
a = ap.parse_args()
web = a.web; r2 = R2(); t0 = time.time()
manifest = json.load(open(os.path.join(web, "manifest.json")))
local = {f: os.path.getsize(os.path.join(web, "groups", f)) for f in os.listdir(os.path.join(web, "groups")) if f.endswith(".json")}
if len(local) != manifest["leaves"]: sys.exit(f"web/groups has {len(local)} files but the manifest has {manifest['leaves']} leaves; refusing to publish")
remote = {k.split("/")[-1]: s for k, s, _ in r2.list(a.groups_prefix)}
todo = sorted(f for f, s in local.items() if remote.get(f) != s)
print(f"groups: {len(local)} local, {len(remote)} in R2, {len(todo)} to upload (missing or size differs)", flush=True)
up = Uploader(r2, workers=a.workers)
for f in todo: up.put(f"{a.groups_prefix}{f}", os.path.join(web, "groups", f), "application/json")
failed = up.join()
if failed: sys.exit(f"{len(failed)} group uploads failed after retries, e.g. {failed[0]}; manifest NOT published. Re-run to retry.")
remote = {k.split("/")[-1]: s for k, s, _ in r2.list(a.groups_prefix)}
bad = [f for f, s in local.items() if remote.get(f) != s]
if bad: sys.exit(f"{len(bad)} group files still differ in R2 after upload (e.g. {bad[0]}); manifest NOT published")
for name in ("salary-model.json", "arrangement-model.json", "seniority-model.json", "age-model.json", "location-countries.json"):
    p = os.path.join(web, name)
    if os.path.exists(p): r2.put_file(a.root_prefix + name, p, "application/json"); print(f"  {name}", flush=True)
r2.put_file(a.root_prefix + "centroids.bin", os.path.join(web, "centroids.bin"), "application/octet-stream")
r2.put_file(a.root_prefix + "manifest.json", os.path.join(web, "manifest.json"), "application/json")
# A few hundred bytes next to the 23 MB manifest: the Worker reads this to resolve flat groups/<id>.json requests to the
# current build's prefix (readers written before the dated layout), and pollers can watch built_at cheaply.
head = {k: manifest.get(k) for k in ("groups", "built_at", "leaves", "nodes", "jobs", "jobs_aggregator", "jobs_total", "recipe", "dims")}
r2.put_bytes(a.root_prefix + "manifest-head.json", json.dumps(head).encode(), "application/json")
if a.mirror_prefix and a.mirror_prefix != a.groups_prefix:
    # Three copies of the group files live in the bucket: this build's dated prefix (what the manifest names), the
    # previous build's (a manifest cached for an hour must still find its files), and this flat mirror for readers
    # that predate the dated layout. The mirror is refreshed after the manifest so new readers never see a mix.
    import concurrent.futures as cf
    tm = time.time(); names = sorted(local)
    def cp(f):
        for attempt in range(4):
            try: r2.copy(f"{a.groups_prefix}{f}", f"{a.mirror_prefix}{f}", "application/json"); return None
            except Exception as e:
                if attempt == 3: return f"{f}: {e}"
                time.sleep(1 + attempt)
    with cf.ThreadPoolExecutor(max_workers=16) as ex: errs = [e for e in ex.map(cp, names) if e]
    print(f"mirrored {len(names) - len(errs)} group files to {a.mirror_prefix} for old readers ({time.time() - tm:.0f}s)" + (f"; {len(errs)} failed, e.g. {errs[0]}" if errs else ""), flush=True)
    # The mirror only ever overwrote ids 0..N-1: when a build has fewer leaves than an earlier one, the old tail stayed
    # (22,526 files for an 11,372-leaf tree on 2026-09-11) and a reader walking ids until the first 404 mixed in stale
    # groups. Delete every flat file this build did not just write.
    keep = set(names); stale = [k for k, _, _ in r2.list(a.mirror_prefix) if k.count("/") == a.mirror_prefix.count("/") and k[len(a.mirror_prefix):] not in keep]
    with cf.ThreadPoolExecutor(max_workers=16) as ex: list(ex.map(r2.delete, stale))
    if stale: print(f"deleted {len(stale)} stale group files from {a.mirror_prefix} (ids past this build's {len(names)} leaves)", flush=True)
print(f"published centroids + manifest ({manifest['jobs']:,} jobs, {manifest['leaves']:,} groups, built {time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest['built_at']/1000))}); {len(todo)} group files uploaded; {time.time()-t0:.0f}s", flush=True)

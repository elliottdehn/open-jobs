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
print(f"published centroids + manifest ({manifest['jobs']:,} jobs, {manifest['leaves']:,} groups, built {time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest['built_at']/1000))}); {len(todo)} group files uploaded; {time.time()-t0:.0f}s", flush=True)

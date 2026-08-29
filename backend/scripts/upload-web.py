#!/usr/bin/env python3
"""Upload <EXPORT_DIR>/web to the jobscream-data R2 bucket with a worker pool (wrangler r2 object put).
Groups first, then centroids, then manifest.json last so clients never see a manifest without its groups.
Skips files already uploaded in this run (marker dir web/.uploaded). Usage: EXPORT_DIR=export-full python3 scripts/upload-web.py [--workers 8]"""
import os, subprocess, sys, threading, queue, time, argparse
ap = argparse.ArgumentParser(); ap.add_argument("--workers", type=int, default=8); a = ap.parse_args()
web = os.path.join(os.environ.get("EXPORT_DIR", "export"), "web"); B = "jobscream-data"
mark = os.path.join(web, ".uploaded"); os.makedirs(mark, exist_ok=True)
def put(key, path, ctype):
    for attempt in range(4):
        r = subprocess.run(["npx", "wrangler", "r2", "object", "put", f"{B}/{key}", "--file", path, "--content-type", ctype, "--remote"], capture_output=True, text=True)
        if r.returncode == 0: return True
        time.sleep(2 * (attempt + 1))
    print(f"FAILED {key}: {r.stderr[-200:]}", flush=True); return False
groups = sorted(os.listdir(os.path.join(web, "groups")))
todo = [g for g in groups if not os.path.exists(os.path.join(mark, g))]
print(f"{len(todo)} of {len(groups)} group files to upload ({a.workers} workers)", flush=True)
q = queue.Queue(); [q.put(g) for g in todo]; done = [0]; lock = threading.Lock(); t0 = time.time()
def worker():
    while True:
        try: g = q.get_nowait()
        except queue.Empty: return
        if put(f"groups/{g}", os.path.join(web, "groups", g), "application/json"): open(os.path.join(mark, g), "w").close()
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(todo): print(f"{done[0]}/{len(todo)} groups, {time.time()-t0:.0f}s", flush=True)
ts = [threading.Thread(target=worker) for _ in range(a.workers)]; [t.start() for t in ts]; [t.join() for t in ts]
missing = [g for g in groups if not os.path.exists(os.path.join(mark, g))]
if missing: sys.exit(f"{len(missing)} group files failed; re-run to retry")
for name in ("salary-model.json", "arrangement-model.json", "location-countries.json"):  # estimators trained by consolidate.sh step 4b
    if os.path.exists(os.path.join(web, name)): put(name, os.path.join(web, name), "application/json")
put("centroids.bin", os.path.join(web, "centroids.bin"), "application/octet-stream")
put("manifest.json", os.path.join(web, "manifest.json"), "application/json")
print("uploaded centroids + manifest; done", flush=True)

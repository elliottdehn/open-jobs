#!/usr/bin/env python3
"""Pull ATS exports with a real worker pool (N in flight, no head-of-line blocking), skipping ATSes
already completed (marker in <out>/.done/<ats>). Then optionally run the parquet/manifest builders.
Usage: python3 scripts/pull-pool.py --out export-slim --workers 4 -- --status=open --slim --embed
"""
import argparse, os, subprocess, sys, threading, queue, time
ap = argparse.ArgumentParser()
ap.add_argument("--base", default=os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev"))
ap.add_argument("--out", default="export")
ap.add_argument("--workers", type=int, default=0, help="0 = all at once (each board is its own DO; the Worker scales)")
ap.add_argument("--ats", default="ashby breezy comeet crelate dark dayforce eightfold gohire greenhouse icims jibe jobscore jobvite join lever oraclecloud paycom paylocity personio phenom pinpoint recruitee recruiterbox smartrecruiters softgarden successfactors taleo teamtailor ukg usajobs workable workday")
ap.add_argument("--then", default="", help="shell command to run after all exports succeed")
ap.add_argument("flags", nargs="*")
a = ap.parse_args()
os.makedirs(os.path.join(a.out, ".done"), exist_ok=True)
todo = [x for x in a.ats.split() if not os.path.exists(os.path.join(a.out, ".done", x))]
# biggest first so the long ones don't end up alone at the tail
big = {"workday": 9, "smartrecruiters": 8, "paycom": 7, "greenhouse": 6, "oraclecloud": 5, "dayforce": 4, "eightfold": 4}
todo.sort(key=lambda x: -big.get(x, 0))
print(f"todo ({len(todo)}): {' '.join(todo)}", flush=True)
q = queue.Queue(); [q.put(x) for x in todo]
failed = []; lock = threading.Lock()
def worker():
    while True:
        try: ats = q.get_nowait()
        except queue.Empty: return
        t = time.time()
        p = subprocess.run(["node", "scripts/export.mjs", ats, a.base, f"--out={a.out}", *a.flags], capture_output=True, text=True)
        tail = [l[:300] for l in (p.stdout + p.stderr).replace("\r", "\n").splitlines() if l.startswith("wrote") or "truncated" in l or ("Error" in l and not l.startswith("{"))][-2:]
        with lock:
            print(f"[{ats}] {'ok' if p.returncode == 0 else 'FAILED'} in {time.time()-t:.0f}s :: {' | '.join(tail)}", flush=True)
            if p.returncode == 0: open(os.path.join(a.out, ".done", ats), "w").close()
            else: failed.append(ats)
ts = [threading.Thread(target=worker) for _ in range(a.workers or len(todo) or 1)]
[t.start() for t in ts]; [t.join() for t in ts]
print("=== exports done" + (f"; FAILED: {' '.join(failed)}" if failed else ""), flush=True)
if failed: sys.exit(1)
if a.then: sys.exit(subprocess.call(a.then, shell=True))

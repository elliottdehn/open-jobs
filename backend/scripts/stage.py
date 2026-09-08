# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3"]
# ///
"""One consolidation stage per invocation. This is the unit a container runs; consolidate.sh is the laptop wrapper
that calls the stages in order. Every stage is idempotent: re-running it overwrites the same outputs.

  uv run scripts/stage.py <stage> [--date YYYY-MM-DD] [--source local|r2] [--publish] [--worker URL] [--work DIR]

Stages, in pipeline order:
  ingest      fetch the local-only ATSes from this machine and post them to the Worker (cannot run inside Cloudflare)
  pull        (--source local) download per-board snapshots into <export>/snapshots; (--source r2) nothing to do
  ledger      slim status=all export of every board -> <export>/ledger-raw -> export/ledger/<date>/
  parquet     snapshots (+ /export fallback) -> jobs/<ats>.parquet, boards/<ats>.parquet [--publish -> exports/<date>/]
  diff        today's parquet vs the previous export -> export/diffs/<prev>__<date>/ (+ lite, sidecar, carry-forward)
  tree        manifest, centroids, group files [--publish streams group files to R2 as written]
  estimators  salary, arrangement, seniority, age, city table, location table -> web/
  finalize    reconcile groups/ in R2, then models, centroids, manifest (last)
  history     diffs + ledger parts + index.json to R2
  retention   (local only) delete older full exports that have a successor diff

Layout: --source local keeps today's layout (<backend>/export/<date>/ holds everything). --source r2 reads
snapshots and the previous export from the bucket in place and writes jobs/boards parquet there
(exports/<date>/); local scratch goes under --work (default <backend>/work-<date>/). State between stages is
only files: local dirs or R2 prefixes. Env: ADMIN_TOKEN (admin endpoints), R2_* (bucket), OPENAI_KEY (location table).
"""
import argparse, glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__)); BACKEND = os.path.normpath(os.path.join(HERE, ".."))
ap = argparse.ArgumentParser()
ap.add_argument("stage", choices=["ingest", "pull", "ledger", "parquet", "diff", "tree", "estimators", "finalize", "history", "retention"])
ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
ap.add_argument("--source", choices=["local", "r2"], default=os.environ.get("CONSOLIDATE_SOURCE", "local"))
ap.add_argument("--publish", action="store_true", help="write parquet / group files to R2 as produced (always on for --source r2)")
ap.add_argument("--worker", default=os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev"))
ap.add_argument("--work", help="local scratch dir (default <backend>/export/<date> for local, <backend>/work-<date> for r2)")
ap.add_argument("--prev", help="previous export for the diff (default: export/latest for local, the newest exports/<date> before --date for r2)")
ap.add_argument("--skip-models", action="store_true"); ap.add_argument("--keep-full", action="store_true")
a = ap.parse_args()
os.chdir(BACKEND)
r2_mode = a.source == "r2"; publish = a.publish or r2_mode
BUCKET = os.environ.get("R2_BUCKET", "jobscream-data")
export_local = os.path.join("export", a.date)
work = a.work or (os.path.join("work-" + a.date) if r2_mode else export_local)
export_root = f"s3://{BUCKET}/exports/{a.date}" if r2_mode else export_local   # what downstream stages read
os.makedirs(work, exist_ok=True); os.makedirs(export_local, exist_ok=True)
token = os.environ.get("ADMIN_TOKEN") or (open("admin_token.txt").read().strip() if os.path.exists("admin_token.txt") else "")
if token: os.environ["ADMIN_TOKEN"] = token
env = dict(os.environ, EXPORT_DIR=export_root, WORK_DIR=work, WORKER_URL=a.worker)
t0 = time.time()
def run(cmd, **kw):
    print(f"$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, env={**env, **kw.pop("env", {})}, **kw)
    if r.returncode != 0: sys.exit(f"stage {a.stage}: `{cmd[1] if len(cmd) > 1 else cmd[0]}` exited {r.returncode}")
def stamp(msg): print(f"=== {a.stage} {a.date}: {msg} ({time.time() - t0:.0f}s)", flush=True)

if a.stage == "ingest":
    run(["node", "--experimental-strip-types", "scripts/fetch-local.mjs", f"--ingest={a.worker}"])
elif a.stage == "pull":
    if r2_mode: stamp("nothing to pull: snapshots are read from the bucket in place")
    else: run(["node", "scripts/pull-snapshots.mjs", a.worker, f"--out={export_local}", "--exclude=jobscore,governmentjobs"])
elif a.stage == "ledger":
    raw = os.path.join(work, "ledger-raw"); os.makedirs(raw, exist_ok=True)
    run(["python3", "-u", "scripts/pull-pool.py", "--base", a.worker, "--out", raw, "--", "--status=all", "--slim", "--resume"])
    run(["uv", "run", "scripts/build-ledger.py", "--raw", raw, "--date", a.date], env={"EXPORT_DIR": export_local})
elif a.stage == "parquet":
    # the /export fallback for ATSes without snapshots (local-only providers) lands as ndjson in the local export dir
    boards = json.load(open("src/boards.json"))
    missing = sorted(x for x in boards if not glob.glob(os.path.join(export_local, "snapshots", x, "*.parquet"))) if not r2_mode else ["jobscore", "governmentjobs"]
    if missing:
        run(["python3", "-u", "scripts/pull-pool.py", "--base", a.worker, "--out", export_local, "--ats", " ".join(missing), "--", "--status=open", "--embed", "--resume"], env={"NODE_OPTIONS": "--max-old-space-size=16384"})
    cmd = ["uv", "run", "scripts/build-parquet.py"] + (["--publish"] if publish else [])
    run(cmd, env={"EXPORT_DIR": export_local, "SNAPSHOT_SOURCE": "r2" if r2_mode else "local"})
elif a.stage == "diff":
    if a.prev: prev = a.prev
    elif r2_mode:
        sys.path.insert(0, HERE); from r2 import R2
        dates = sorted({k.split("/")[1] for k, _, _ in R2().list("exports/") if k.count("/") >= 2})
        older = [d for d in dates if d < a.date]; prev = f"s3://{BUCKET}/exports/{older[-1]}" if older else ""
    else:
        prev = os.path.realpath("export/latest") if os.path.exists("export/latest") else ""
        if prev and os.path.basename(prev) == a.date: prev = ""
    if not prev: stamp("no previous export to diff against; skipping"); sys.exit(0)
    run(["uv", "run", "scripts/build-diff.py", "--prev", prev, "--new", export_root, "--base", a.worker, "--out", "export/diffs"])
elif a.stage == "tree":
    run(["uv", "run", "scripts/build-manifest.py", "--out", os.path.join(work, "web")] + (["--publish"] if publish else []))
elif a.stage == "estimators":
    if a.skip_models: stamp("skipped (--skip-models)"); sys.exit(0)
    for s in ("train-salary", "train-arrangement", "train-seniority", "train-age", "build-city-table", "build-location-table"):
        run(["uv", "run", f"scripts/{s}.py"])
elif a.stage == "finalize":
    run(["uv", "run", "scripts/publish-web.py", "--web", os.path.join(work, "web")])
    if not r2_mode:
        if os.path.islink("export/latest") or os.path.exists("export/latest"): os.unlink("export/latest")
        os.symlink(a.date, "export/latest"); print(f"export/latest -> {a.date}", flush=True)
elif a.stage == "history":
    run(["uv", "run", "scripts/upload-history.py"])
elif a.stage == "retention":
    if r2_mode or a.keep_full: stamp("no local exports to prune" if r2_mode else "kept (--keep-full)"); sys.exit(0)
    side = sorted(glob.glob(f"export/diffs/*__{a.date}.json"))
    ok = bool(side) and json.load(open(side[-1])).get("ok_to_prune") and json.load(open(side[-1])).get("carry_done")
    if not ok: stamp("today's diff is missing or failed its sanity check; older full exports kept"); sys.exit(0)
    import shutil
    for d in sorted(glob.glob("export/20*-*-*")):
        b = os.path.basename(d)
        if b >= a.date or not os.path.isdir(d): continue
        if not glob.glob(f"export/diffs/{b}__*/"): print(f"  keep {d}: no diff was ever taken from it", flush=True); continue
        size = subprocess.run(["du", "-sh", d], capture_output=True, text=True).stdout.split()[0]
        print(f"  delete {d} ({size}; its successor diff exists)", flush=True); shutil.rmtree(d)
stamp("done")

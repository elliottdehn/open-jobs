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
  feed        the paged consumer feed (JOB-CHANGES.md): bootstrap from today's export the first time, then one
              generation per diff; publishes under changes/ and keeps export/feed/published.json as the receipt
  archive     tar the export (jobs + boards + README) into exports/open-jobs-latest.tar: one link for the whole dataset
  retention   (local only) delete older full exports that have a successor diff

Layout: --source local keeps today's layout (<backend>/export/<date>/ holds everything). --source r2 reads
snapshots and the previous export from the bucket in place and writes jobs/boards parquet there
(exports/<date>/); local scratch goes under --work (default <backend>/work-<date>/). State between stages is
only files: local dirs or R2 prefixes. Env: ADMIN_TOKEN (admin endpoints), R2_* (bucket), OPENAI_KEY (location table).
"""
import argparse, atexit, glob, json, os, socket, subprocess, sys, threading, time, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__)); BACKEND = os.path.normpath(os.path.join(HERE, ".."))
ap = argparse.ArgumentParser()
ap.add_argument("stage", choices=["ingest", "pull", "ledger", "parquet", "diff", "tree", "estimators", "finalize", "history", "archive", "feed", "retention", "report", "unlock", "selftest"])
ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
ap.add_argument("--source", choices=["local", "r2"], default=os.environ.get("CONSOLIDATE_SOURCE", "local"))
ap.add_argument("--publish", action="store_true", help="write parquet to R2 as produced (always on for --source r2); group files always stream up during the tree stage unless --no-publish")
ap.add_argument("--no-publish", action="store_true", help="tree stage: keep group files local only (validation builds)")
ap.add_argument("--worker", default=os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev"))
ap.add_argument("--work", help="local scratch dir (default <backend>/export/<date> for local, <backend>/work-<date> for r2)")
ap.add_argument("--prev", help="previous export for the diff (default: export/latest for local, the newest exports/<date> before --date for r2)")
ap.add_argument("--skip-models", action="store_true"); ap.add_argument("--keep-full", action="store_true")
ap.add_argument("--only", help="parquet: restrict to these ATSes (comma list; tests)")
ap.add_argument("--dry-run", action="store_true", help="report: print the line, post nothing")
ap.add_argument("--force-lock", action="store_true", help="take the publisher lock even if another run holds it (you are sure it is dead)")
a = ap.parse_args()
os.chdir(BACKEND)
r2_mode = a.source == "r2"; publish = a.publish or r2_mode
BUCKET = os.environ.get("R2_BUCKET", "jobscream-data")
export_local = os.path.join("export", a.date)
WORK_ROOT = os.environ.get("WORK_ROOT", ".")                    # the container mounts its scratch volume here
work = a.work or (os.path.join(WORK_ROOT, "work-" + a.date) if r2_mode else export_local)
export_root = f"s3://{BUCKET}/exports/{a.date}" if r2_mode else export_local   # what downstream stages read
# Group files go under a per-build prefix so the manifest swap at finalize is atomic for readers (a manifest cached for
# an hour keeps finding its files); retention keeps this build and the previous one. GROUPS_PREFIX overrides (tests).
GROUPS_PREFIX = os.environ.get("GROUPS_PREFIX") or f"groups/{a.date}/"
os.makedirs(os.path.realpath("export"), exist_ok=True)   # in the container, export/ is a symlink into the scratch volume
os.makedirs(work, exist_ok=True); os.makedirs(export_local, exist_ok=True)
token = os.environ.get("ADMIN_TOKEN") or (open("admin_token.txt").read().strip() if os.path.exists("admin_token.txt") else "")
if token: os.environ["ADMIN_TOKEN"] = token
env = dict(os.environ, EXPORT_DIR=export_root, WORK_DIR=work, WORKER_URL=a.worker)
t0 = time.time()

# ---- the publisher lock: one consolidation run at a time, laptop or container (Worker /lock, src/lock.ts) ----
# Every publishing stage acquires it under this run's holder id (kept in <work>/lock.json so the stages of one run,
# separate processes, share it), renews it in the background, and retention releases it. Unrenewed, it expires in
# LOCK_TTL so a crashed run cannot block tomorrow's. `stage.py unlock` releases by hand; --force-lock takes it over.
LOCK_TTL_MS = 4 * 3600 * 1000
LOCKFILE = os.path.join(work, "lock.json")
def lock_call(action, body=None):
    req = urllib.request.Request(f"{a.worker}/lock" + (f"/{action}" if action else ""), data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET", headers={"authorization": f"Bearer {token}", "content-type": "application/json", "user-agent": "open-jobs-stage/1"})  # the default urllib UA is bot-blocked at the edge
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
def lock_holder():
    if os.path.exists(LOCKFILE): return json.load(open(LOCKFILE))["holder"]
    h = f"{socket.gethostname()}:{a.date}:{uuid.uuid4().hex[:8]}"; json.dump({"holder": h, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, open(LOCKFILE, "w")); return h
def when(ms): return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))
def lock_release(force=False):
    try:
        r = lock_call("release", {"holder": lock_holder(), "force": force})
        print("publisher lock released" if r["ok"] else f"lock NOT released: held by {r['lock']['holder']} since {when(r['lock']['since'])}", flush=True)
        if r["ok"] and os.path.exists(LOCKFILE): os.remove(LOCKFILE)
    except Exception as e: print(f"WARNING: lock release failed: {e}", flush=True)
if a.stage == "unlock":
    lock_release(force=True); sys.exit(0)
if a.stage not in ("ingest", "report", "selftest") and not a.dry_run:
    if not token: sys.exit("ADMIN_TOKEN is required: the publisher lock lives behind the admin endpoints")
    holder = lock_holder()
    r = lock_call("acquire", {"holder": holder, "ttlMs": LOCK_TTL_MS, "note": f"{a.stage} on {socket.gethostname()}", "force": a.force_lock})
    if not r["ok"]:
        L = r["lock"]; sys.exit(f"another publisher holds the lock: {L['holder']} ({L.get('note','')}) since {when(L['since'])}, expires {when(L['until'])} unless renewed.\n"
                                f"Wait for it, or if it is dead: uv run scripts/stage.py unlock --date {a.date}  (or re-run with --force-lock)")
    print(f"publisher lock: {holder} (since {when(r['lock']['since'])})", flush=True)
    def _renew():
        while True:
            time.sleep(300)
            try:
                lock_call("renew", {"holder": holder, "ttlMs": LOCK_TTL_MS})
                if a.stage == "parquet" and r2_mode: lock_call("freeze", {"holder": holder, "ttlMs": LOCK_TTL_MS})
            except Exception as e: print(f"WARNING: lock renew failed: {e}", flush=True)
    threading.Thread(target=_renew, daemon=True).start()
    if a.stage == "retention": atexit.register(lock_release)
RUNLOG = os.path.join(work, "run.jsonl")   # one line per stage outcome; the report stage reads it
def record(ok, msg):
    with open(RUNLOG, "a") as f: f.write(json.dumps({"stage": a.stage, "date": a.date, "ok": ok, "seconds": round(time.time() - t0), "msg": msg, "at": time.strftime("%H:%M:%S")}) + "\n")
def run(cmd, **kw):
    print(f"$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, env={**env, **kw.pop("env", {})}, **kw)
    if r.returncode != 0:
        record(False, f"`{cmd[1] if len(cmd) > 1 else cmd[0]}` exited {r.returncode}")
        sys.exit(f"stage {a.stage}: `{cmd[1] if len(cmd) > 1 else cmd[0]}` exited {r.returncode}")
def stamp(msg):
    print(f"=== {a.stage} {a.date}: {msg} ({time.time() - t0:.0f}s)", flush=True); record(True, msg)
def r2c():
    sys.path.insert(0, HERE); from r2 import R2; return R2()

if a.stage == "selftest":
    # No lock, no publishing: what this box is, whether the bucket is reachable, and how fast it reads (CONTAINER.md).
    import platform, shutil
    try: print("image build", open(os.path.join(HERE, "BUILD")).read().strip(), flush=True)
    except OSError: print("image build unknown (no scripts/BUILD)", flush=True)
    print(f"host {socket.gethostname()} {platform.machine()} python {platform.python_version()} cpus {os.cpu_count()}", flush=True)
    try: print(f"memory {int(open('/proc/meminfo').readline().split()[1]) // 1024} MiB", flush=True)
    except Exception: pass
    du = shutil.disk_usage(WORK_ROOT if os.path.isdir(WORK_ROOT) else "/"); print(f"disk {WORK_ROOT}: {du.free // 10**9} GB free of {du.total // 10**9} GB", flush=True)
    print("env:", {k: bool(os.environ.get(k)) for k in ("ADMIN_TOKEN", "OPENAI_KEY", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "SLACK_RUN_WEBHOOK", "LOW_DISK", "STAGE_TO_BUCKET")}, flush=True)
    r2 = r2c()
    keys = sorted(((k, sz) for k, sz, _ in r2.list("exports/") if k.endswith("/jobs/workday.parquet")), reverse=True)
    if keys:
        k, sz = keys[0]; want = min(sz, 1_000_000_000); t0 = time.time()
        body = r2.client.get_object(Bucket=r2.bucket, Key=k, Range=f"bytes=0-{want - 1}")["Body"]; got = 0
        for chunk in iter(lambda: body.read(8 << 20), b""): got += len(chunk)
        dt = time.time() - t0; print(f"bucket read: {got / 1e9:.2f} GB of {k} in {dt:.1f}s = {got / max(dt, 0.01) / 1e6:.0f} MB/s (single stream)", flush=True)
    print("selftest ok", flush=True); sys.exit(0)

if a.stage == "ingest":
    run(["node", "--experimental-strip-types", "scripts/fetch-local.mjs", f"--ingest={a.worker}"])
elif a.stage == "pull":
    if r2_mode: stamp("nothing to pull: snapshots are read from the bucket in place")
    else: run(["node", "scripts/pull-snapshots.mjs", a.worker, f"--out={export_local}", "--exclude=jobscore,governmentjobs"])
elif a.stage == "ledger" and r2_mode:
    # Derived from yesterday's ledger and today's export (derive-ledger.py); runs after the parquet stage. The pulled
    # ledger (status=all through the Worker) is kept for local runs only.
    r2 = r2c()
    ldays = sorted({k.split("/")[1] for k, _, _ in r2.list("ledger/") if k.count("/") == 2 and k.endswith(".parquet") and k.split("/")[1] < a.date})
    xdays = sorted({k.split("/")[1] for k, _, _ in r2.list("exports/") if k.count("/") >= 2 and k.split("/")[1] < a.date})
    if not ldays: sys.exit("no previous ledger in the bucket to derive from")
    cmd = ["uv", "run", "scripts/derive-ledger.py", "--date", a.date, "--export", f"s3://{BUCKET}/exports/{a.date}", "--prev-ledger", f"s3://{BUCKET}/ledger/{ldays[-1]}", "--out", "export/ledger"]
    if xdays and ldays[-1] <= "2026-09-10": cmd += ["--prev-export", f"s3://{BUCKET}/exports/{xdays[-1]}"]  # first derived day: drop never-published rows of the pulled ledger
    run(cmd)
elif a.stage == "ledger":
    raw = os.path.join(work, "ledger-raw"); os.makedirs(raw, exist_ok=True)
    # LOW_DISK (20 GB cloud container): pages land as gzip parts (~2 GB for the whole fleet instead of 14 GB of ndjson,
    # 12 GB of it the aggregator tier, which grows daily) and each ATS's raw input is deleted once its parquet exists
    low_disk = os.environ.get("LOW_DISK") == "1"
    run(["python3", "-u", "scripts/pull-pool.py", "--base", a.worker, "--out", raw, "--", "--status=all", "--slim", "--resume"] + (["--gzip"] if low_disk else []))
    run(["uv", "run", "scripts/build-ledger.py", "--raw", raw, "--date", a.date] + (["--low-disk"] if low_disk else []), env={"EXPORT_DIR": export_local})
elif a.stage == "parquet":
    # the /export fallback for ATSes without snapshots (local-only providers) lands as ndjson in the local export dir
    boards = json.load(open("src/boards.json"))
    missing = sorted(x for x in boards if not glob.glob(os.path.join(export_local, "snapshots", x, "*.parquet"))) if not r2_mode else ["jobscore", "governmentjobs"]
    if missing:
        run(["python3", "-u", "scripts/pull-pool.py", "--base", a.worker, "--out", export_local, "--ats", " ".join(missing), "--", "--status=open", "--embed", "--resume"], env={"NODE_OPTIONS": "--max-old-space-size=16384"})
    cmd = ["uv", "run", "scripts/build-parquet.py"] + (["--publish"] if publish else []) + ([f"--ats={a.only}"] if a.only else [])
    # Freeze snapshot rewrites while we read them: a board replacing its file mid-read hands DuckDB pages of the new
    # file under the old footer (garbage decoded as text, 2026-09-09). Boards retry their write ten minutes later.
    if r2_mode and not a.dry_run:
        lock_call("freeze", {"holder": lock_holder(), "ttlMs": LOCK_TTL_MS}); print("snapshot writes frozen for the read", flush=True)
    try:
        nw = int(os.environ.get("PARQUET_WORKERS", "0") or 0)
        if r2_mode and nw > 0 and not a.dry_run:
            # Fan out across worker containers (src/consolidate.ts): dark parts by index modulo, the other sources
            # spread by snapshot bytes; each worker runs build-parquet with its slice; then a second round runs the
            # end-of-run dedup, pass B split the same way. This stage holds the lock and the snapshot freeze meanwhile.
            r2 = r2c(); sizes = {}
            for k, sz, _ in r2.list("snapshots/"):
                if k.endswith(".parquet"): x = k.split("/")[1]; sizes[x] = sizes.get(x, 0) + sz
            others = sorted((x for x in sizes if x != "dark" and x in boards and x not in missing), key=lambda x: -sizes[x])
            slots = [[] for _ in range(nw)]; load = [0] * nw
            for x in others: i = load.index(min(load)); slots[i].append(x); load[i] += sizes[x]
            def fan(round_label, worker_args):
                import urllib.request
                hdr = {"authorization": f"Bearer {token}", "content-type": "application/json"}
                starts = {}
                for i, wargs in enumerate(worker_args):
                    body = json.dumps({"args": wargs, "env": {"EXPORT_DIR": export_local, "SNAPSHOT_SOURCE": "r2"}, "label": f"{round_label} {i}"}).encode()
                    r = json.load(urllib.request.urlopen(urllib.request.Request(f"{a.worker}/run/worker/{i}", data=body, headers=hdr), timeout=60))
                    if not r.get("started"): record(False, f"worker {i} did not start"); sys.exit(f"worker {i} did not start: {r}")
                    starts[i] = time.time() * 1000; print(f"  worker {i}: {' '.join(wargs)[:150]}", flush=True)
                done = {}
                while len(done) < len(worker_args):
                    time.sleep(60)
                    for i in starts:
                        if i in done: continue
                        st = json.load(urllib.request.urlopen(urllib.request.Request(f"{a.worker}/run/worker/{i}", headers=hdr), timeout=60))
                        stops = [e for e in st.get("journal", []) if e.get("ev") == "stop" and e.get("t", 0) > starts[i]]
                        if stops:
                            done[i] = stops[-1].get("exitCode"); tail = ((st.get("lastOutput") or {}).get("text") or "").strip().splitlines()[-3:]
                            print(f"  worker {i} exited {done[i]} after {(time.time() * 1000 - starts[i]) / 60000:.0f} min: " + " | ".join(t[:100] for t in tail), flush=True)
                bad = {i: c for i, c in done.items() if c != 0}
                if bad: record(False, f"{round_label}: worker(s) failed: {bad}"); sys.exit(f"{round_label}: worker(s) failed: {bad}")
            base = ["/usr/local/bin/uv", "run", "scripts/build-parquet.py", "--publish"]
            print(f"parquet fan-out across {nw} workers: dark parts by index, then {sum(len(x) for x in slots)} other sources by bytes", flush=True)
            fan("parquet", [base + [f"--ats=dark,{','.join(slots[i])}" if slots[i] else "--ats=dark", f"--parts=mod:{nw}:{i}"] for i in range(nw)])
            fan("dedup", [base + ["--dedup-only", f"--parts=mod:{nw}:{i}"] for i in range(nw)])
        else: run(cmd, env={"EXPORT_DIR": export_local, "SNAPSHOT_SOURCE": "r2" if r2_mode else "local"})
    finally:
        if r2_mode and not a.dry_run:
            try: lock_call("thaw", {"holder": lock_holder()}); print("snapshot writes thawed", flush=True)
            except Exception as e: print(f"WARNING: thaw failed ({e}); the freeze expires on its own", flush=True)
elif a.stage == "diff":
    if a.prev: prev = a.prev
    elif r2_mode:
        dates = sorted({k.split("/")[1] for k, _, _ in r2c().list("exports/") if k.count("/") >= 2})
        older = [d for d in dates if d < a.date]; prev = f"s3://{BUCKET}/exports/{older[-1]}" if older else ""
    else:
        prev = os.path.realpath("export/latest") if os.path.exists("export/latest") else ""
        if prev and os.path.basename(prev) == a.date: prev = ""
    if not prev: stamp("no previous export to diff against; skipping"); sys.exit(0)
    run(["uv", "run", "scripts/build-diff.py", "--prev", prev, "--new", export_root, "--base", a.worker, "--out", "export/diffs"])
    if os.environ.get("LOW_DISK") == "1":
        # 20 GB disk: the full diff parts (6.7 GB on 2026-09-10) go to the bucket now, before the tree's memmap needs the
        # room. The local copies become empty placeholders; upload-history sees the keys in the bucket and takes sizes
        # and hashes from the sidecar. The lite parts and the sidecar stay (the feed stage reads them).
        sides = sorted(glob.glob(f"export/diffs/*__{a.date}.json"))
        if sides:
            d = sides[-1][:-5]; name = os.path.basename(d); r2 = r2c(); n = 0
            for part in sorted(glob.glob(os.path.join(d, "*.parquet"))):
                r2.put_file(f"diffs/{name}/{os.path.basename(part)}", part, "application/octet-stream"); open(part, "w").close(); n += 1
            # the lite parts and the sidecar go up too (kept locally for the feed): a resume in a fresh container can
            # then still index this diff from the bucket alone
            for part in sorted(glob.glob(os.path.join(d, "lite", "*.parquet"))): r2.put_file(f"diffs/{name}/lite/{os.path.basename(part)}", part, "application/octet-stream")
            r2.put_file(f"diffs/{name}.json", d + ".json", "application/json")
            print(f"uploaded {n} diff part(s), the lite parts and the sidecar to diffs/{name}/; freed the local full parts (LOW_DISK)", flush=True)
elif a.stage == "tree":
    # group files stream to R2 while the tree writes them (the 2026-09-08 run skipped this and finalize spent 26 min
    # uploading 37 GB instead); finalize still reconciles by size, so a missed upload here is caught there
    run(["uv", "run", "scripts/build-manifest.py", "--out", os.path.join(work, "web")] + ([] if a.no_publish else ["--publish"]), env={"GROUPS_PREFIX": GROUPS_PREFIX})
elif a.stage == "estimators":
    if a.skip_models: stamp("skipped (--skip-models)"); sys.exit(0)
    # The parquet stage leaves a complete local copy of jobs/ and boards/ under export/<date>/ even in r2 mode; the
    # trainers scan the whole corpus (content + vectors), which over the S3 API inside the 16 GB VM crawled and was
    # OOM-killed (2026-09-10). Read the local copy when it is complete.
    local_jobs = glob.glob(os.path.join(export_local, "jobs", "*.parquet")); est_env = {}
    if r2_mode and local_jobs and len(local_jobs) >= 30:
        est_env = {"EXPORT_DIR": export_local}; print(f"estimators read the local export copy ({len(local_jobs)} jobs files)", flush=True)
    only_est = [x for x in os.environ.get("ESTIMATORS_ONLY", "").split(",") if x]  # rerun a subset after a failure
    for s in ("train-salary", "train-arrangement", "train-seniority", "train-age", "build-city-table", "build-location-table"):
        if only_est and s not in only_est: continue
        run(["uv", "run", f"scripts/{s}.py"], env=est_env)
elif a.stage == "finalize":
    run(["uv", "run", "scripts/publish-web.py", "--web", os.path.join(work, "web")], env={"GROUPS_PREFIX": GROUPS_PREFIX})
    if not r2_mode:
        if os.path.islink("export/latest") or os.path.exists("export/latest"): os.unlink("export/latest")
        os.symlink(a.date, "export/latest"); print(f"export/latest -> {a.date}", flush=True)
elif a.stage == "history":
    # in r2 mode the indexes are rebuilt from the bucket (local state holds only today's parts) and the snapshot
    # build time comes from this run's manifest
    run(["uv", "run", "scripts/upload-history.py"] + (["--remote-index", "--manifest", os.path.join(work, "web", "manifest.json")] if r2_mode else []))
elif a.stage == "feed":
    # JOB-CHANGES.md, automated: first run bootstraps from today's completed export against the index the history
    # stage just published; later runs project today's diff onto the last published generation. Local-source
    # only: the bootstrap reads export/<date>/jobs and web/manifest.json.
    # The receipt (last published generation) is the only state between runs; in r2 mode it lives in the bucket
    # under state/feed/published.json and is fetched before and stored after. The bootstrap needs a local export
    # (jobs/ + web/manifest.json), so in r2 mode a missing receipt means "bootstrap by hand once, locally".
    import urllib.request
    feed = os.path.join(WORK_ROOT, "export", "feed") if r2_mode else "export/feed"; os.makedirs(feed, exist_ok=True)
    receipt = os.path.join(feed, "published.json")
    if r2_mode:
        r2 = r2c()
        if r2.head("state/feed/published.json"): r2.get_file("state/feed/published.json", receipt)
        elif not os.path.exists(receipt): stamp("no feed receipt in state/feed/published.json; bootstrap once from a local export (JOB-CHANGES.md), then upload the receipt"); sys.exit(0)
    idx = os.path.join(feed, "diffs-index.json")
    # a User-Agent is required: the Worker's edge returns 403 to the default urllib agent
    with urllib.request.urlopen(urllib.request.Request(f"{a.worker}/data/diffs/index.json?check={int(time.time())}", headers={"cache-control": "no-cache", "user-agent": "open-jobs-tools/0.1"}), timeout=60) as r: open(idx, "wb").write(r.read())
    if os.path.exists(receipt):
        side = sorted(glob.glob(f"export/diffs/*__{a.date}.json"))
        if not side: stamp("no diff ending today; nothing to project"); sys.exit(0)
        run(["uv", "run", "scripts/build-job-changes.py", "--out", feed, "--diff", side[-1], "--previous", receipt, "--publish-base", a.worker])
    else:
        run(["uv", "run", "scripts/build-job-changes.py", "--out", feed, "--snapshot", export_local, "--index", idx, "--publish-base", a.worker])
    if r2_mode: r2.put_file("state/feed/published.json", receipt, "application/json")
elif a.stage == "archive":
    # one-link download of the whole export, straight from the local copy into a multipart upload (r2 mode only)
    if not r2_mode and not a.publish: stamp("skipped (no bucket)"); sys.exit(0)
    run(["uv", "run", "scripts/build-archive.py", "--export", export_local])
elif a.stage == "retention":
    # incomplete multipart uploads (a killed stage's parts) are billable and invisible to listings: abort any older than a day
    try: n_mp = r2c().abort_stale_multipart("", 86_400); print(f"aborted {n_mp} stale incomplete multipart upload(s)", flush=True)
    except Exception as e: print(f"WARNING: multipart cleanup failed ({e})", flush=True)
    if a.keep_full: stamp("kept (--keep-full)"); sys.exit(0)
    side = sorted(glob.glob(f"export/diffs/*__{a.date}.json"))
    ok = bool(side) and json.load(open(side[-1])).get("ok_to_prune") and json.load(open(side[-1])).get("carry_done")
    if not ok: stamp("today's diff is missing or failed its sanity check; older full exports kept"); sys.exit(0)
    import shutil
    if r2_mode:
        # exports/<date>/ in the bucket: keep today and the previous one (tomorrow's diff needs it), drop the rest
        r2 = r2c(); dates = sorted({k.split("/")[1] for k, _, _ in r2.list("exports/") if k.count("/") >= 2})
        for d in [d for d in dates if d < a.date][:-1]:
            keys = [k for k, _, _ in r2.list(f"exports/{d}/")]
            print(f"  delete exports/{d}/ ({len(keys)} objects; a newer export and its diff exist)", flush=True)
            for k in keys: r2.delete(k)
        # groups/<date>/ prefixes: keep this build and the previous one (a manifest cached for an hour must still find
        # its files); the flat legacy groups/<id>.json files are not dated and are left alone
        res = r2.client.list_objects_v2(Bucket=r2.bucket, Prefix="groups/", Delimiter="/")
        gdates = sorted(p["Prefix"][len("groups/"):-1] for p in res.get("CommonPrefixes", []))
        for d in [d for d in gdates if d < a.date][:-1]:
            keys = [k for k, _, _ in r2.list(f"groups/{d}/")]
            print(f"  delete groups/{d}/ ({len(keys)} objects; two newer builds exist)", flush=True)
            for k in keys: r2.delete(k)
        for d in glob.glob(os.path.join(WORK_ROOT, "work-20*")):
            if os.path.basename(d)[5:] < a.date: shutil.rmtree(d, ignore_errors=True); print(f"  removed scratch {d}", flush=True)
        # the local export copies on the volume (13 GB a night): keep today's, drop older ones
        for d in glob.glob("export/20*-*-*"):
            if os.path.isdir(d) and os.path.basename(d) < a.date: shutil.rmtree(d, ignore_errors=True); print(f"  removed local export copy {d}", flush=True)
        stamp("done"); sys.exit(0)
    for d in sorted(glob.glob("export/20*-*-*")):
        b = os.path.basename(d)
        if b >= a.date or not os.path.isdir(d): continue
        if not glob.glob(f"export/diffs/{b}__*/"): print(f"  keep {d}: no diff was ever taken from it", flush=True); continue
        size = subprocess.run(["du", "-sh", d], capture_output=True, text=True).stdout.split()[0]
        print(f"  delete {d} ({size}; its successor diff exists)", flush=True); shutil.rmtree(d)
elif a.stage == "report":
    # One line a person can read in ten seconds, posted to Slack: what ran, what passed, the numbers. SLACK_RUN_WEBHOOK
    # (an incoming webhook) if set; otherwise the Worker's /ideas relay, which reaches the shared channel.
    import urllib.request
    rows = [json.loads(l) for l in open(RUNLOG)] if os.path.exists(RUNLOG) else []
    seen = {}
    for r in rows: seen[r["stage"]] = r          # last outcome per stage
    failed = [s for s, r in seen.items() if not r["ok"]] or (["(no stage recorded)"] if not seen else [])  # an empty record is not a success
    mf = os.path.join(work, "web", "manifest.json"); jobs = json.load(open(mf))["jobs"] if os.path.exists(mf) else None
    side = sorted(glob.glob(f"export/diffs/*__{a.date}.json")); d = json.load(open(side[-1])) if side else {}
    fr = os.path.join(WORK_ROOT if r2_mode else ".", "export", "feed", "published.json"); gen = json.load(open(fr))["generation"][:8] if os.path.exists(fr) else None
    total = sum(r["seconds"] for r in seen.values())
    line = (f"{'✅' if not failed else '❌'} consolidation {a.date} ({'container' if r2_mode else 'laptop'}): "
            + (f"{jobs:,} jobs in the index; " if jobs else "no manifest; ")
            + (f"diff +{d['counts']['added']:,} -{d['counts']['removed']:,} ~{d['counts']['changed']:,}; " if d else "no diff; ")
            + (f"feed {gen}; " if gen else "feed not advanced; ")
            + f"{len(seen)} stages in {total // 60} min"
            + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    print(line, flush=True)
    if a.dry_run: sys.exit(1 if failed else 0)
    hook = os.environ.get("SLACK_RUN_WEBHOOK")
    try:
        if hook: urllib.request.urlopen(urllib.request.Request(hook, data=json.dumps({"text": line}).encode(), headers={"content-type": "application/json"}), timeout=30)
        else: urllib.request.urlopen(urllib.request.Request(f"{a.worker}/ideas", data=json.dumps({"file": "backend/scripts/stage.py", "idea": line, "tags": ["run"]}).encode(), headers={"content-type": "application/json", "user-agent": "open-jobs-tools/0.1"}), timeout=30)
        print("posted", flush=True)
    except Exception as e: print(f"WARNING: could not post the run line: {e}", flush=True)
    sys.exit(1 if failed else 0)
stamp("done")

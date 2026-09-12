# Moving consolidation into a Cloudflare Container

The daily consolidation (`scripts/consolidate.sh`) runs on one laptop. The end state is the same
pipeline as Cloudflare Containers, started by the existing cron, with no laptop in the loop. This
file is the plan: what the run looks like today, what does not fit yet, what to change, in what order.

## Today's shape

Measured on 2026-09-07 (3.13M open jobs, 63k boards):

| stage | what | wall time | peak resources |
|---|---|---|---|
| 1 ingest | local-only ATSes (jobscore) fetched from the laptop and posted to the Worker | 11 min | trivial |
| 2 pull | 62k per-board parquet snapshots from R2, `/export` fallback for two ATSes | 17 min | 35 GB disk |
| 2b ledger | slim `status=all` export of every board -> `export/ledger/<date>/` | 33 min | 2 GB disk, 535 MB out |
| 3 parquet | snapshots -> `jobs/<ats>.parquet`, `boards/<ats>.parquet` | 6 min | 8 GB disk, DuckDB |
| 3b diff | today vs previous export -> `export/diffs/<prev>__<date>/` | 1 min | DuckDB, 20 GB cap, spills |
| 4 manifest | tree over all embedded jobs, centroids, 11k group files | ~20 min | was 19.3 GB real memory; **now 9.7 GB peak** (4.5 GB through the tree, a brief 9.6 GB in the group-file pass under DuckDB's 6 GB cap); 9.6 GB float16 vector memmap + ~5 GB staging on disk; 37 GB of group files |
| 4b estimators | salary, arrangement, seniority, age, city + location tables | 15 min | a few GB |
| 5 upload | 11k group files + centroids + manifest, via `wrangler r2 object put` | 45 min (+ retry) | network; 300 MiB/object cap |
| 5b history | diffs + ledger parts + `index.json` | 5 min | same |
| 6 retention | delete older full exports once the diff verified | seconds | frees ~80 GB |

Total about 2.5 hours; ~80 GB of local disk for one full export, ~160 GB while the previous one
still exists.

## What does not fit

Cloudflare Containers, without an account-team exception: largest instance **4 vCPU, 12 GiB
memory, 20 GB disk** (`standard-4`; custom types stop at the same ceilings). Run-duration limits are
not documented, so a single three-hour process is not something to depend on.

Three things exceed that:

1. **Disk.** 80 GB staged per day vs 20 GB.
2. **Memory for the tree build.** Was 19.3 GB real memory. Done on 2026-09-08 (step 1 below): 9.7 GB
   peak, 4.5 GB through the tree itself. The remaining peak is DuckDB's buffer (capped at 6 GB) during the
   group-file pass on top of the ~4.5 GB Python baseline (PCA projection + titles). Fits `standard-4`
   with ~2 GB to spare; lowering the DuckDB cap to 4 GB now that sorts are per-chunk would widen that.
3. **Duration.** One process for 2.5 hours, with the group-file upload alone at 45 minutes.

Everything else already fits: the diff builder runs under a DuckDB memory cap and spills; the
ledger is HTTP against the Worker; the estimators are small; the retention step disappears entirely
(there is no local export to prune).

## Changes, by stage

### Pull (2) and parquet (3): read R2 in place, never stage it
- `build-parquet.py` reads `snapshots/<ats>/*.parquet` from R2 through DuckDB's S3 client instead
  of from `export/<date>/snapshots/`. Same SQL, different path. Needs an R2 S3 access key as a
  container secret. The 35 GB never touches local disk.
- The `/export` fallback stays HTTP against the Worker; it only covers ATSes without snapshots.
- Output `jobs/<ats>.parquet` and `boards/<ats>.parquet` go straight to R2 under
  `exports/<date>/` (~8 GB) rather than to local disk. Every later stage reads them from there.
- Ingest (1) cannot move: `fetch-local.mjs` exists because those providers block Cloudflare IPs. It
  stays a laptop command run on its own schedule (it only needs the Worker); the container's run
  starts at pull. See step 6.

### Ledger (2b): unchanged, output to R2
`pull-pool.py` + `build-ledger.py` as today; the raw ndjson (~2 GB) fits on the container disk;
the parquet parts go to R2 directly. 33 minutes is dominated by the Worker's `/export` throughput
and does not change with location.

### Diff (3b): unchanged, both sides from R2
`build-diff.py` already diffs two parquet trees through DuckDB with a memory cap. Point `--prev`
and `--new` at `exports/<prev>/jobs/` and `exports/<date>/jobs/` on R2; lower the cap to 8 GB and
keep the spill directory on the container disk. The carry-forward step rewrites today's
`jobs/<ats>.parquet`, which becomes a rewrite of that R2 object. The vanished-board check calls
the Worker as it does now.

### Tree build (4): the one real rewrite
- **Cluster on the projection only.** Keep `Z` (N x 256 float32, ~3 GB) in memory; do not
  materialize `X` (N x 1536). PCA is fit on a 50k sample already, so `Z` can be produced in one
  streaming pass over the parquet: read a batch, project, append.
- **Stream exact vectors at write time.** Group files need the exact vectors for their members.
  Today `X[idx]` serves that. Instead, after the tree is built, walk the parquet again in row order
  and route each job to its leaf (the leaf assignment is an N-long int array), writing group files
  as leaves complete. DFS order is preserved by writing the assignment first and emitting each leaf
  when its last member arrives, or more simply by a second pass per ATS file with an open-file map.
  Peak memory: `Z` + the assignment + one batch.
- **Stream group files to R2** as each is finished, through the S3 client, instead of writing
  37 GB locally and uploading afterwards. This deletes stage 5's group upload and the 300 MiB cap
  that came with `wrangler`. Multipart upload handles anything large.
- Centroids (68 MB) and the manifest (22 MB) upload last, as today, so a reader that sees the new
  manifest finds every group it references.
- Same for the diff parts and ledger parts: S3 multipart from the container, no 300 MiB cap, no
  `index.json` gymnastics beyond what exists.

### Estimators (4b): unchanged
They read `jobs/*.parquet` (from R2) and a location table; a few GB of memory. `train-age.py`
and friends already fit `standard-4`.

### Upload (5, 5b) and retention (6): mostly gone
With every artifact written to R2 as it is produced, "upload" reduces to the ordered final writes
(estimator JSONs, centroids, manifest, then `index.json` files). Retention becomes an R2 lifecycle
rule on `exports/<date>/` (keep the latest two) instead of local deletes; diffs and ledger stay.

## Orchestration

- **Trigger:** the Worker's existing daily cron (`0 0 * * *`) starts a Workflow instead of (in
  addition to) re-arming boards; the fleet's own fetch slots are unchanged.
- **Workflow steps = stages.** Each step starts a container with a single command
  (`consolidate-stage pull`, `... ledger`, `... parquet`, `... diff`, `... tree`, `... estimators`,
  `... finalize`), waits for it, and records the stage's output prefix in R2. A failed step retries
  that stage only; every stage is idempotent against R2 (same keys, overwritten). This replaces the
  `.done` markers and the "re-run consolidate.sh" recovery in DOCS.md.
- **One image**, Python + `uv` + DuckDB + numpy + node (for `pull-pool.py`'s `export.mjs`); the
  stage is an argument. `standard-4` for tree and parquet, `standard-2` for the rest.
- **Secrets:** `ADMIN_TOKEN` (Worker admin endpoints for `/export` and the vanished-board check),
  R2 S3 credentials scoped to `jobscream-data`, `OPENAI_KEY` only for `build-location-table.py`
  (it embeds new location strings; pennies).
- **Fitting Cloudflare's largest instance (standard-4: 4 vCPU, 12 GiB, 20 GB disk).** Memory fits today's tree build
  (~10 GB peak). Disk did not: the vector memmap was written twice (~19 GB), pass 2 staged ~30 GB locally, and the
  parquet stage left a 13+ GB local export copy. Now: the memmap is written once, in key order, straight from a
  DuckDB join (no sorted copy); `STAGE_TO_BUCKET=1` sends pass 2's staging to `tmp/` in the bucket and deletes it after;
  `LOW_DISK=1` makes the parquet stage drop each local file once uploaded (the end-of-run dedup and the archive read
  the bucket instead; the estimators already fall back to the bucket when there is no local copy). Both flags are ON in the
  image (Dockerfile ENV), so the laptop run exercises the cloud path every night; `-e LOW_DISK=0 -e STAGE_TO_BUCKET=0`
  opts out. First exercised on the laptop 2026-09-10 night.
- **Resuming across midnight.** `scripts/container-run.sh from <stage> --date <run date>`: the date is fixed once for
  the remaining stages. Running stages one by one after midnight without `--date` targets the new day (a different
  work dir and lock holder), which the lock refuses; that is how the first night's diff was refused at 03:03.
- **One publisher at a time.** Done (2026-09-09): the Worker's `/lock` Durable Object; stage.py acquires it for every
  publishing stage, renews it, and retention releases it. Laptop and container cannot both write the bucket.
- **Observability, a requirement not a nicety.** The point of the container is that the project
  runs for a week with nobody watching. So: a final step posts one line per run to Slack (date, jobs
  in the manifest, diff added/removed/changed, ledger size, feed generation, and which stages
  passed), and a second scheduled check posts the next morning if no line arrived. Anyone can read
  either in ten seconds. Each stage keeps printing the summary lines it does today; the Workflow
  keeps them for when the line says something failed.

## Status (2026-09-10)

The nightly runs end to end in the container image on the laptop (Docker Desktop, 16 GB VM), reading and
writing the real bucket. Two nights so far:

- **2026-09-09 (first night).** Every stage ran, with restarts. What broke and what changed: the build
  script's Dockerfile path; the snapshot read race (boards rewriting R2 objects while DuckDB reads them by
  byte range) -> the snapshot freeze on the lock object; two transient R2 503s -> DuckDB `http_retries`,
  block retries, and a same-run skip of sources already published; a per-stage resume after local midnight
  drifted to the new date -> `container-run.sh from <stage> --date D`; the diff OOM-killed under a 20 GB
  DuckDB default in a 16 GB VM -> `DIFF_MEMORY=10GB` in the image; 829k spurious `changed` rows from
  date-only posting dates cast in the session zone (laptop EDT vs container UTC) -> every export script pins
  UTC and the diff compares `published_at` by calendar date; a diff sidecar with no parent link on a fresh
  volume -> the parent is fetched from the bucket; salary and age trainers OOM-killed -> 250k-row samples,
  the estimators read the local export copy or the bucket with a 4 GB cap; the location table re-embedded
  437k strings because its cache gate keyed on the export path -> keyed on bucket mode. Published: manifest
  (3,160,249 first-party postings), diff, ledger, feed generation, four models.
- **2026-09-10 (second night, in progress at the time of writing).** First run with `LOW_DISK=1` and
  `STAGE_TO_BUCKET=1` (the cloud path, on by default in the image), the aggregator tier in the export, the
  `archive` stage (one tar at a stable URL), and the search tree placing job-board postings into the
  first-party tree. The `dark` source alone is ~17M snapshot rows after the crawler cap came off.

**Publishing safety, done:** one publisher at a time (`/lock` Durable Object; `stage.py` acquires per stage,
renews, retention releases; `unlock` and `--force-lock`), the snapshot freeze while the parquet stage reads,
dated group prefixes with the manifest published last (`groups/<date>/`, previous build kept, flat mirror for
old checkouts), the diff's parent chain verified by the feed, and idempotent stages with a same-run skip.

**Observability, done:** `report` posts one line per run (jobs, diff counts, feed generation, failed stages)
to `SLACK_RUN_WEBHOOK` or the ideas relay; `run.jsonl` per stage; `scripts/cf-usage.py` for the meters;
`GET /lock`, `GET /stats`, `GET /dedupe`, `GET /rowmeter?board=` for the fleet's state.

- **2026-09-10 (second night), outcome.** Every source converted (325 min through parquet, dark 2,618,955 rows kept
  for the aggregator tier, workday 838,527) and then the end-of-run dedup failed on the boards join: `read_ndjson`
  had typed paylocity's GUID-shaped slugs as UUID, and the union of boards files mixed UUID with VARCHAR. Fixed at
  the source (boards parquet casts `ats`/`slug` to VARCHAR) with defensive casts in the join; the published
  paylocity boards file was rewritten by hand and the run resumed with `from parquet --date 2026-09-10` on the
  rebuilt image, which is therefore the first full-scale run of the parts, the dedup across parts, the tree
  without the second memmap, and the gzip ledger pull (the ledger stage itself had already run on the old image).

- **2026-09-10 (second night), the rest of it.** Completed at 11:13 EDT after thirteen resumes, all published: index
  4,450,859 postings (3,251,298 first-party + 1,199,561 job-board) in 11,572 groups, diff +1,390,185 / -101,317 /
  ~36,398, ledger, feed generation bea8d075, tar 18.6 GB. Faults after the UUID slug, in order: the diff selected
  columns the older export lacks (typed NULLs now); its output dir kept a killed attempt's partial file (cleared);
  it buffered five full scans (two labeled scans now, DIFF_MEMORY 6 GB, threads 8, write retried on bucket errors,
  bytes-based progress); the Mac's maintenance sleep stalled the chain and skewed the VM clock into R2 403s
  (container-run.sh caffeinates itself); the tree's staging write blew DuckDB's cap twice (partition flush 5000,
  row groups 10k, arrays and memmap freed before pass 2, cap 8 GB, s3 uploader 4 threads) and a leftover bucket
  staging prefix blocked a rerun (cleared, incomplete multipart uploads aborted); the location table's kNN vote ran
  the container out of memory at 587k distinct locations (256-row chunks, dict and DuckDB freed first). Also: the
  diff stage is no longer warning-only; ESTIMATORS_ONLY=build-location-table reran just that model. Snapshot cap
  raised to 2M rows (two aggregators were skipped at 400k); dark no longer parks rows as detail 'na' and the 4.16M
  already parked were re-queued. Flat groups/<id>.json now resolves through manifest-head.json to the current
  build's prefix, and the flat mirror is trimmed by name after each publish.

- **2026-09-11 (evening): the cloud container exists and answers.** `wrangler.jsonc` declares the `Consolidate`
  container class (standard-4, `image: ./Dockerfile` built for linux/amd64 and pushed by `wrangler deploy`, build
  context the repo root) with its Durable Object binding `CONSOLIDATE` and a service binding `SELF` to this Worker.
  `src/consolidate.ts` starts the image's process with the full command (no ports: `start()` and `onStop`), hands it
  the secrets as env, and keeps a journal (start, output, stop with exit code) that `GET /run` returns; the process
  is wrapped so its last 200 KB of output and its exit code come back to the object through `POST /run/output`.
  Containers cannot reach `*.workers.dev`: requests to `http://worker.internal` run as an outbound handler in the
  Workers runtime (`Consolidate.outboundByHost`, which needs `ContainerProxy` exported from the entrypoint) and are
  forwarded over `SELF`; `WORKER_URL` inside the container is that name. Routes (admin): `POST /run/chain {date, from,
  env}` runs `scripts/container-chain.sh` (all stages in one process, since the disk lives only as long as the
  container), `POST /run/stage {stage, date, env}`, `POST /run/exec {args}`, `POST /run/stop`, `GET /run`.
  `scripts/cloud-deploy.sh` writes a build id into the image (`scripts/BUILD`), deploys, polls the platform until the
  rollout settles (no container starts), and verifies the id from inside. The lock-free `selftest` stage measured:
  x86_64, 4 vCPU, 12,220 MiB, 15 GB free of 19 GB, all secrets present, 94 MB/s on a single-stream 1 GB bucket read
  (the app's network limit is 4 Gbit/s; DuckDB's parallel range reads are the real number, still to measure).
  Later the same evening: the disk budget for a whole chain on 19 GB. Group files never sit on the disk (the tree
  stage's uploader deletes each one after its upload under `LOW_DISK` and records the sizes in
  `.published-groups.json`; finalize verifies those against the bucket listing instead of local files), and the diff's
  full parts go to the bucket right after the diff stage (local copies become empty placeholders that upload-history
  recognises; the lite parts and the sidecar stay for the feed). Scratch-tested end to end under `tmp/lowdisk-test/`:
  1,640 groups uploaded and deleted as written, finalize verified all of them, manifest + head + mirror published,
  then cleaned up. The container's output tail is posted back every two minutes while a stage runs (code -1 in
  `lastOutput`), and the tree's filler read is capped at `TREE_FILL_MEMORY` 3 GB for the 12 GiB box. The silence
  alarm exists: a `0 10 * * *` cron posts to Slack when `manifest-head.json` is older than 26 h.
  Deploys: `scripts/cloud-deploy.sh` (fails loudly when the registry rejects a layer, which it did once while a scratch
  test saturated the uplink; waits for the app's image to be the pushed digest, not merely "no rollout active").
  Plan for the first real cloud run: a watcher starts `POST /run/chain {date: 2026-09-12}` the moment the laptop run
  of 2026-09-11 reports and the lock frees, so a failure harms nothing published and a success is a second build that
  morning. No parallel week: the laptop stays the nightly until one cloud chain has completed.

- **2026-09-11 (third night): the ledger is derived.** The pulled status=all ledger (two hours through the Worker, then
  a 12 GiB DuckDB sort that ran the container out of memory) is replaced in r2 mode by `scripts/derive-ledger.py`:
  yesterday's ledger plus today's export, one pass, 63 s. Rows in today's export are open (first_seen_at carried from
  the previous ledger); previously open rows absent from the export left the dataset today (removed_at = that day
  00:00 UTC); removed rows carry. The first derived day drops the pulled ledger's never-published rows (`--prev-export`).
  Validated on 2026-09-10: open rows equal the export's 4,452,644 exactly, keys unique, 5.7M first-seen dates carried.
  What changes for readers: the ledger is now "every posting that has ever appeared in the dataset" rather than every
  row the crawler ever held, and a removal is stamped by day, without the closed-versus-left label (the diff's
  removal column is `unknown` from here). Chain order is now pull, parquet, diff, ledger, tree, ... The run of
  2026-09-11 was killed at the OOM and restarted from pull on this code.

- **2026-09-11 night: the first cloud chain, paused for a fan-out.** Started 20:19 EDT after the laptop run was
  stopped by hand. Pull and the laptop-only providers worked through `worker.internal`; the parquet stage found dark at
  22,873,732 snapshot rows (13 parts of 2M) and converted the first part in 38 min on four cores: eight hours for dark
  alone, 18 to 20 for the chain. Paused (the lock released, snapshots thawed by hand: a killed stage never thaws) and
  the stage was made to fan out: with `PARQUET_WORKERS=N`, stage.py starts N worker containers (`POST /run/worker/<i>`,
  objects `worker-<i>` of the same class, `max_instances` 6), each running `build-parquet.py --publish --ats=<slice>
  --parts=mod:N:<i>` (dark parts by index modulo, other sources spread by snapshot bytes), waits for their exit codes,
  then a second round with `--dedup-only` split the same way; the chain stage holds the lock and the freeze
  throughout. Every interim post from a container now ends with a host line (load, memory, disk). `POST /run/stop?
  signal=kill` for a process that ignores SIGTERM. Resumed `from parquet` with four workers; already-published parts
  are skipped by the same-run check. Standing conclusion: the export's JSON group format is a contract (mirrors depend
  on it), so CPU-bound stages scale by containers, not by changing what is written.

## Runbook (the workflows in use, 2026-09-11)

All admin calls take `authorization: Bearer $(cat backend/admin_token.txt)`; `W=https://backend.dehnbostele.workers.dev`.

**Deploy code or the image to the cloud.** `scripts/cloud-deploy.sh` (from `backend/`). It writes `scripts/BUILD`
(build id), runs `wrangler deploy` (Worker + image, built for linux/amd64 and pushed; fails loudly if the registry
rejects a layer, which it did when the uplink was busy), polls `wrangler containers info` until the app's image is
the pushed digest and no rollout is active, stops the idle instance (the keep-alive would otherwise keep the old one
answering until the rollout reaches it), waits 75 s, and asks the container for its build id until it
matches. `cloud-deploy.sh --wait` does only the wait-and-verify half. Never poll a rollout by starting the container. **A deploy is a rollout that stops every running instance of the old image** (the journal shows
"Runtime signalled the container to exit due to a new version rollout"): it killed take 3 of the 2026-09-11 chain and
its four workers mid-stage. The script now refuses while any run object is busy (`--force` overrides); deploy between
runs, or accept the restart and resume `from <stage>` after unlock.
A Worker-only change still goes through the same script.

**Run the nightly chain in the cloud.** `POST $W/run/chain {"date": "YYYY-MM-DD", "from": "all" | "<stage>",
"env": {...}}`. One container runs `scripts/container-chain.sh` (pull parquet diff ledger tree estimators finalize
history feed archive retention, then report; ledger/history/feed/archive are warning-only, the rest stop the chain).
Env worth passing: `PARQUET_WORKERS=4` fans the parquet stage out across worker containers; `ESTIMATORS_ONLY=...`
reruns a subset of the estimators. Resume after a failure with `"from": "<stage>"`; already-published parquet parts
and groups are skipped by the same-run checks, and the diff's parts, lite parts and sidecar are uploaded right after
the diff so a resume in a fresh container still indexes them.

**Run one stage.** `POST $W/run/stage {"stage": "...", "date": "...", "env": {...}}`. `selftest` is lock-free and
prints the platform facts and the bucket read rate. Every other stage but ingest/report takes the publisher lock:
a cloud stage cannot run while a laptop run holds it, and vice versa.

**Watch it.** `GET $W/run`: `state`, `current` (what is running, since when), `journal` (start / output / stop with
exit code, last 50), `lastOutput` (the process's last 200 KB, refreshed every two minutes while it runs; its last line
is the host line: load, memory, disk). Workers: `GET $W/run/worker/<i>`. The full stdout is in the Cloudflare
dashboard (Workers & Pages > backend > Containers / Observability). Locally a relay Monitor can poll `GET /run` and
write `logs/cloud-chain-<date>.tail`. Read the JSON with `curl -o file` + python, never through `echo "$var"` (the
shell mangles escaped control characters).

**Stop it.** `POST $W/run/stop` (SIGTERM; a stage may keep running) or `POST $W/run/stop?signal=kill`. After a kill:
the publisher lock stays held until its TTL and the snapshot freeze stays on, so `scripts/container-run.sh unlock
--date D` (or `POST $W/lock/release {holder, force: true}`) and `POST $W/lock/thaw {holder, force: true}`; check
`GET $W/lock` shows `lock: null, snapshotsFrozen: false`. A start that answers `busy` is waiting for the previous
process's stop event; wait for `current` to clear.

**Run it on the laptop instead.** `uv run scripts/stage.py ingest --date D` (laptop-only providers), then
`scripts/container-run.sh all --date D` or `from <stage> --date D`; the script caffeinates itself (the Mac's
maintenance sleep skews the Docker clock into R2 403s). Same image, same flags (`LOW_DISK`, `STAGE_TO_BUCKET`), the
work volume persists between stages. Do not run the laptop chain and the cloud chain at once: one lock.

**Morning checks.** The report line in Slack (or `logs/night-<date>.log` / `GET /run`), `GET /data/manifest-head.json`
(`built_at`, `jobs_total`, `groups`), the 10:00 UTC silence alarm posts if nothing published in 26 h, and
`uv run scripts/cf-usage.py --hours 24` for the meters. If a stage failed: read the traceback in `lastOutput`, fix,
`cloud-deploy.sh`, resume `from <stage>`.

## Running the container locally

```sh
scripts/container-run.sh build                       # image for this machine (arm64); build-amd64 for Cloudflare
uv run scripts/stage.py ingest                       # the laptop-only providers, first
scripts/container-run.sh all                         # pull .. archive .. retention, then report
scripts/container-run.sh from tree --date 2026-09-10 # resume from a stage; the date is fixed once (see below)
scripts/container-run.sh parquet --only jazzhr       # one stage
scripts/container-run.sh shell                       # look around inside
```
Secrets come from `backend/.dev.vars` (R2_*, OPENAI_KEY) and `admin_token.txt`; nothing is in the image.
Scratch is the named volume `open-jobs-work` at `/work`. State between runs lives in the bucket:
`exports/<date>/`, `state/feed/published.json`, `state/location-embeddings.npz`. Docker Desktop needs 16 GB.

Lessons that apply to any host:
- Run long chains inside something that survives the shell: on this machine, every `run_in_background`
  shell died at once twice in one night (containers gone); a persistent Monitor did not.
- Resume with `from <stage> --date <run date>`. Stages run one by one after local midnight pick the new
  day, a different work dir and lock holder, and the lock refuses them (correctly).
- The report stage reads `run.jsonl` cumulatively: interim ❌ lines are normal while resuming.

## What remains for Cloudflare Containers

**Next, before the next night: checkpoint the tree between the fill and pass 2.** Every pass 2 failure on
2026-09-12 (OOM in the staging write, twice) replayed the vector load (12 min), PCA, the bisection (18 min) and the
fill (an hour) to get back to the line that broke: about 90 minutes per attempt. The state pass 2 needs is small: the
row order, the node list with centroids and labels, the leaf boundaries after the split, and the hash-to-position
table (a few hundred MB). build-manifest.py should write it to the bucket (tmp/<date>.tree/) right after the split
and take `--resume-pass2` to load it and start at the staging write; stage.py tree passes the flag when the
checkpoint exists for the date. Then a staging fix costs a minute to retry, not ninety.


The largest instance is **standard-4: 4 vCPU, 12 GiB memory, 20 GB disk** (custom types cap at the same).
Measured against the 2026-09-10 run:

1. **`dark` in parts. Done 2026-09-10 (evening), not yet in the image.** The parquet stage packs a source
   whose snapshots exceed `PARQUET_PART_ROWS` (2,000,000) into `jobs/<ats>.p<n>.parquet` and
   `boards/<ats>.p<n>.parquet` by board slug, converting and publishing one part at a time (`LOW_DISK` drops
   each local copy after upload); `PARQUET_MEMORY` (6 GB in the image) caps that DuckDB connection. The
   end-of-run dedup works across the parts: pass A picks one winner per (employer, title, location) over every
   part, pass B rewrites each part, pulling and pushing one at a time. `build-parquet.py --dedup-only` reruns
   just that pass. Tested: jazzhr from the bucket in two parts (same 6,156 rows and 388 boards as one file);
   the scratch dark export split in two with 100 cross-part copies and 50 first-party copies planted, all 150
   dropped and nothing else (679,832 to 679,682). Readers already glob `jobs/*.parquet`.
2. **The tree stage's second memmap. Done 2026-09-10 (evening), not yet in the image.** The tree is built on
   every first-party posting (that set is the sample; `TREE_SAMPLE_ROWS` exists as an emergency lever and is
   off). Every other embedded row, job-board or not, is placed through the filler descent, and pass 2 takes
   each row's vector from the parquet rows it already streams, so the job-board memmap is gone and only the
   first-party memmap remains (~9.8 GB for 3.2M rows, written once in key order; with the export left in the
   bucket under `LOW_DISK` and staging under `STAGE_TO_BUCKET`, that is the stage's only large local file).
   Tested on the scratch export: identical tree (3,109 nodes, 1,555 leaves), identical placement (679,657
   filler rows), manifest `jobs`/`jobs_aggregator`/`jobs_total` now count by tier with `built_on` the builder
   count, group vectors float32 unit length as before (a NumPy 2 promotion to float64 was caught by the test).
2b. **The ledger's raw pull. Found and fixed 2026-09-10 (evening), not yet in the image.** Measured on the second
   night: `work/<date>/ledger-raw` held 14 GB of slim status=all ndjson for the whole run, 12 GB of it dark, and
   that file grows daily because the ledger keeps removed rows forever (~9 GB per 20M rows). Under `LOW_DISK`
   the export now writes every page as its own gzip part (`<ats>.ndjson.d/<offset>.ndjson.gz`, ~9x smaller,
   `--resume` skips pages already on disk) and `build-ledger.py --low-disk` converts one ATS at a time to
   `ledger/<date>/<ats>.parquet`, deleting each raw input as soon as its parquet exists. Tested on jazzhr against
   the live Worker: identical ledger rows from both paths (9,617 jobs, 436 boards), 7.1 MB to 768 KB.
3. **Split-on-overflow for leaves. Done 2026-09-11.** After the filler, a leaf over `MAX_GROUP_ROWS` (2,500) becomes
   chunks of its DFS range: children sharing the parent's centroid and label, ids appended. The 2026-09-10 build's
   largest group was 18,792 rows (~225 MB); the scratch test went from 11,728 to 2,479 rows, 33 MB, 71 groups into
   156 chunks, every count reconciled. Readers take leaves from the tree, so nothing changes for them.
4. **Image to the registry** (`build-amd64`), **a Workflow on the Worker's cron** that starts one container per
   stage with the date fixed once and stops on a non-warning failure, **`SLACK_RUN_WEBHOOK`** set, and a
   **missed-run check** (no report line by 10:00 UTC = post a warning).
5. **A tap on the run, running or not.** A `Run` Durable Object in the Worker is the run journal: the container
   posts stage started / done (elapsed) / warning / failure (with the last 200 log lines) and a batched tail of
   ordinary log lines every few seconds (one row per batch). `GET /run` answers whether or not anything is
   running: the live stage and its start time, the last event, the previous run's outcome, the next scheduled
   start. `/run/tail` is a WebSocket on the same object (hibernation API, free while idle) that pushes every
   event as a frame, so an AI session can keep a persistent monitor on it and react the moment something breaks.
   Silence is a signal: the object's alarms turn "no started by 00:30 UTC" and "no report by 10:00 UTC" into
   synthetic failure events (this replaces the missed-run check in 4). `POST /run/stage` (stage, date) restarts a
   stage as a Workflow step, the remote form of `from <stage> --date`. Containers give no shell into a running
   instance, so the shipped tail plus a restart is the whole fix loop.
6. **A week in parallel** with the laptop, diffing manifests and diff counts, then switch. Ingest stays a
   laptop command; the container never waits for it.

## Open questions
- Container run-duration limit (undocumented): with the job boards the parquet stage runs 2-3 h and the tree
  ~70 min. If a hard limit exists below that, parquet splits per source (it already skips published sources
  on a rerun, so a chain of short containers works).
- R2 operation counts: ~40 GB read and ~60 GB written per night from inside Cloudflare are egress-free;
  class A/B counts (114k snapshot reads, 11k group writes, 11k mirror copies, the staging prefix) were ~1M
  class A and ~2M class B per day on the meter, inside the free tiers.
- The aggregator tier's own age curve, so job-board postings can carry freshness verdicts on the page.

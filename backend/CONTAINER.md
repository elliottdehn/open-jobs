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

The largest instance is **standard-4: 4 vCPU, 12 GiB memory, 20 GB disk** (custom types cap at the same).
Measured against the 2026-09-10 run:

1. **`dark` in the parquet stage does not fit 20 GB.** Its first-pass file passed 10 GB on local disk and the
   tier rewrite and the end-of-run dedup each make a second copy. Write and upload it in parts of a few GB
   (by board slug range), never holding the whole source locally; cap that DuckDB connection at 6 GB (it has
   no cap today and sits at 11 GB in the VM).
2. **The tree stage's two memmaps do not fit 20 GB.** First-party 9.7 GB plus ~9 GB for three million
   job-board rows. The fix is the sample design: build the tree on a uniform sample of first-party rows
   (`WHERE_` with `USING SAMPLE`), place every other row through the existing filler descent, and have pass 2
   take each row's vector from the parquet rows it already streams instead of from a memmap. Memory becomes
   the sample plus centroids; disk becomes the staging, which `STAGE_TO_BUCKET` already moves to the bucket.
3. **Split-on-overflow for leaves** (optional): after filling, 9 of 1,605 test leaves exceeded 5k rows.
4. **Image to the registry** (`build-amd64`), **a Workflow on the Worker's cron** that starts one container per
   stage with the date fixed once and stops on a non-warning failure, **`SLACK_RUN_WEBHOOK`** set, and a
   **missed-run check** (no report line by 10:00 UTC = post a warning).
5. **A week in parallel** with the laptop, diffing manifests and diff counts, then switch. Ingest stays a
   laptop command; the container never waits for it.

## Open questions
- Container run-duration limit (undocumented): with the job boards the parquet stage runs 2-3 h and the tree
  ~70 min. If a hard limit exists below that, parquet splits per source (it already skips published sources
  on a rerun, so a chain of short containers works).
- R2 operation counts: ~40 GB read and ~60 GB written per night from inside Cloudflare are egress-free;
  class A/B counts (114k snapshot reads, 11k group writes, 11k mirror copies, the staging prefix) were ~1M
  class A and ~2M class B per day on the meter, inside the free tiers.
- The aggregator tier's own age curve, so job-board postings can carry freshness verdicts on the page.

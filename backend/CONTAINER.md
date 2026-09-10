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
  the bucket instead; the estimators already fall back to the bucket when there is no local copy). Both env flags
  are opt-in: the laptop run keeps local disk. Untested in the cloud as of 2026-09-10; test with a scratch
  ROOT_PREFIX/GROUPS_PREFIX first.
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

## Status (2026-09-08)

Steps 1-3 below are done, each validated on real data:
- **Write as produced.** `scripts/r2.py` (S3 API, multipart) replaces `wrangler` everywhere: the tree build
  streams group files to `groups/` while writing them (`--publish`), `publish-web.py` (finalize) reconciles
  `groups/` by size and then publishes models, centroids, and the manifest last, `upload-history.py` uses the
  same transport. `upload-web.py` is gone, and with it the 300 MiB cap and the crash-and-retry.
- **Read in place.** `build-parquet.py --source r2` reads the 62k snapshots straight from the bucket
  (DuckDB S3, footer metadata included) and publishes `jobs/`/`boards/` to `exports/<date>/`; the tree
  build, the diff (both roots, carry-forward rewriting the R2 object), and the estimators accept an
  `s3://` `EXPORT_DIR`. Verified on a one-provider export in the bucket.
- **Stages.** `scripts/stage.py <stage>` is the unit a container runs; `consolidate.sh` is now a thin
  wrapper that sequences them, with `--from` to resume. The laptop path is unchanged in behaviour.

Not yet: ingest still runs from the laptop (step 6), and no image, Workflow, or cron trigger exists (step 5).
Reading 62k snapshots from a laptop through DuckDB's S3 client is ~46 files/s (about 22 min for the
fleet), comparable to the current pull; inside Cloudflare it should be well under that.

## Running the container locally (2026-09-09)

```sh
scripts/container-run.sh build                 # image for this machine (arm64); build-amd64 for Cloudflare
scripts/container-run.sh all                   # the nightly run: pull .. report, one container per stage
scripts/container-run.sh parquet --only jazzhr # one stage
scripts/container-run.sh shell                 # look around inside
```
Secrets come from `backend/.dev.vars` (R2_*, OPENAI_KEY) and `admin_token.txt`; nothing is in the image.
Scratch is the named volume `open-jobs-work`, mounted at `/work`: `work-<date>/` (memmap, staging, web/),
`export/diffs`, `export/ledger`, `export/feed`. State between runs lives in the bucket, not on disk:
`exports/<date>/` (the parquet the next diff needs), `state/feed/published.json` (the feed cursor),
`state/location-embeddings.npz` (the estimator's embedding cache). Docker Desktop needs at least 14 GB of
memory for the tree stage; `container-run.sh` warns if it has less.

What differs from the laptop run: no ingest (a laptop command), no local export dir (snapshots are read from
the bucket), the history indexes are rebuilt from the bucket's own listing plus today's parts, retention
prunes `exports/<date>/` prefixes older than the previous one, and a final `report` stage posts one line
(date, jobs, diff counts, feed generation, stages passed) to `SLACK_RUN_WEBHOOK` or, without one, to the
ideas relay. Each stage appends its outcome to `work-<date>/run.jsonl`; that is what the report reads.

## Order of work

Each step is useful on its own and lands on the laptop first, so nothing is a big-bang move.

1. **Tree build streams text and metadata** — DONE 2026-09-08 (see Status). Two passes: pass 1 loads vectors
   into the float16 memmap and keeps only titles/locations/company hints/board per row; the tree is
   built unchanged; pass 2 streams the parquet back in DFS order (DuckDB join on the exact job key,
   sort by position) and writes each group file as its last row arrives. Output verified byte-identical
   to the original on the 2026-09-07 export: same 22,375-node tree, same centroids, all 11,188 group
   files identical. Also made the build deterministic: rows are key-sorted after loading, so the tree
   no longer depends on parquet scan order (the original produced a different tree every run).
   Measured: 19.3 -> 9.7 GB peak real memory, 4.5 GB during the tree itself; 26.9 -> ~20 min. Three
   things it took to get there, each found by a footprint sampler rather than by reasoning: keeping an
   Arrow column from a batch pins the whole batch (19 GB by the end of loading; fixed by copying the
   keys out); a single ORDER BY over the corpus exhausts DuckDB's buffer at any cap (fixed by staging
   into 250k-position chunks and sorting each); and `time -l` measures the `uv` wrapper, not Python.
   The 9.6 GB vector memmap and ~5 GB staging stay on disk (fits the 20 GB instance if parquet is read
   from R2).
2. **Write artifacts to R2 as produced** — DONE 2026-09-08 (see Status).
3. **Read snapshots and exports from R2 in place** — DONE 2026-09-08 as `--source r2` (see Status).
   With it the run needs ~15 GB of local disk (memmap + staging) and ~10 GB of memory.
4. **Split `consolidate.sh` into stage commands** — DONE 2026-09-08 (`scripts/stage.py`; see Status).
5. **Container image + Workflow + cron + the Slack line.** Run it in parallel with the laptop for
   a week, diffing the two manifests, then switch. The Slack summary and the missed-run check ship
   with it, not after.
6. **Ingest** for the local-only ATSes — decided 2026-09-08: it stays a laptop command,
   `uv run scripts/stage.py ingest`, run nightly or whenever. It only talks to the Worker, and the
   boards it posts flow into snapshots like every other board, so the container never depends on it
   and never waits for it; skipping it just leaves those two providers stale until the next run.

Until step 5, a `launchd` job on the laptop at a fixed hour makes the current script hands-off:
it is re-runnable, prunes its own exports, and publishes everything it produces.

## Open questions
- Container run-duration limit (undocumented): the tree stage at 23 minutes and the ledger at
  33 minutes are the long ones. If a hard limit exists below that, the ledger pull splits per ATS.
- Egress: reading 35 GB of snapshots plus writing ~45 GB per day from inside Cloudflare should be
  free of egress charges, but R2 class A/B operation counts on 62k small snapshot reads and 11k
  group writes per day should be checked against the pricing page before committing.
- Whether to keep the 300-MiB-safe part layout for diffs and ledger once multipart uploads exist
  (yes: readers benefit from bounded parts regardless of how they were uploaded).

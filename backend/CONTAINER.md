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
- Ingest (1) is the one stage that cannot move as is: `fetch-local.mjs` exists because those
  providers block Cloudflare IPs. Either it keeps running from a machine outside Cloudflare on its own
  schedule (it only needs the Worker), or those ATSes are dropped from the daily run.

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
- **Observability:** each stage prints the same summary lines it does today; the Workflow keeps
  them, and a final step posts the run summary (counts, diff line, ledger line) to the ideas Slack
  webhook or a new one, so a failed night is noticed without reading logs.

## Order of work

Each step is useful on its own and lands on the laptop first, so nothing is a big-bang move.

1. **Tree build streams text and metadata** — DONE 2026-09-08. Two passes: pass 1 loads vectors
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
2. **Write artifacts to R2 as produced** (group files, diff parts, ledger parts) via the S3 API
   with multipart; retire `upload-web.py` / `upload-history.py` and the 300 MiB cap.
3. **Read snapshots and exports from R2 in place**; stop staging `export/<date>/` locally. At this
   point the laptop run needs ~10 GB of disk and ~8 GB of memory.
4. **Split `consolidate.sh` into stage commands** with R2 prefixes as the only state between them;
   keep a thin shell wrapper for local runs.
5. **Container image + Workflow + cron.** Run it in parallel with the laptop for a week, diffing
   the two manifests, then switch.
6. **Ingest** for the local-only ATSes: a separate small job outside Cloudflare, or drop them.

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

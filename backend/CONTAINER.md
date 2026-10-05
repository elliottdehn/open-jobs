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

## Status (2026-10-05): the tenth cloud night, an eleven-day diff, 12 h plus the feed by hand

The 2026-10-05 consolidation ran 04:55 to 16:54 UTC after an eleven-day gap (the last export was 09-24): 7,362,561
-> 7,579,522 postings (+1,562,447 -1,345,490 ~232,333, carried 0, 980 boards emptied), 3,577,995 distinct vectors,
12,836 group files (87.1 GB), head flipped 15:54 UTC, archive 31.80 GB, ledger 12,564,657 ever. The feed came 80
min later by hand: generation 35cf2877 (1,794,780 upserts, 1,345,490 removes, 3,396 pages). Retention kept the
09-24 full export, as designed: the diff removed more than 15% of the previous export, so it is not ok_to_prune.
Same layout as the ninth night (chain on worker-4, fan-out on 0, 1, 2 and 5; `logs/hand/chain-on-w4.template.json`).

Three hand steps, two of them fixes deployed the same day:

- Worker-5's host was slow again, so the same slice ran as a helper on idle worker-0 (the recipe of the ninth
  night); parquet took 4 h 57 min for 27,988,521 dark rows in 16 parts, dedup 39.7 -> 31.8 GB.
- The diff ran out of disk at 12.7 GB of finished parts: eleven days of changes do not fit next to the two exports
  on a 20 GB volume. The diff now streams each finished part to the bucket during the write under LOW_DISK and
  reads the lite projection back from the bucket urls (4d81e0a, deployed while the chain was down); the resume
  from diff wrote 62 parts (14.2 GB) with 12 GB still free. Pass 2's staging retry (50b71ae) fired once and worked.
- The feed failed on one record: a teamtailor description of 7.9 MB against the 4 MiB page limit, and the writer
  failed the whole generation ("job exceeds page byte limit"; the chain treats feed as warning-only and went on to
  archive). An upsert that cannot fit a page now keeps its key and the other fields, loses the tail of its content
  and carries `content_truncated: true` (a3a33a4, JOB-CHANGES.md); the page contract is unchanged. Deployed after
  the report, then `stage.py feed` posted by hand to `/run/worker/4` (`logs/hand/feed-2026-10-05-on-w4.json`) and
  an `unlock` after it: a hand stage holds the publisher lock at exit.

Timings: parquet 4 h 57 min, diff 19 min on the second try, tree 2 h 50 min, estimators 2 h 11 min, archive 28
min, retention 7 s, feed 80 min (by hand). Objects 0, 1, 2 and 4 still sit on good hosts; 3 and 5 do not.

## Status (2026-09-25): the ninth cloud night, on worker-4, 15.3 h

The 2026-09-24 consolidation ran 02:02 to 17:20 UTC on the 24th, the first night on the new layout: the chain on
worker-4's container (`container-chain.sh all` posted to `/run/worker/4`), the fan-out on objects 0, 1, 2 and 5
(`PARQUET_WORKER_IDS`). 7,380,024 -> 7,362,561 postings (+257,188 -274,647 ~30,792), 3,608,407 distinct vectors,
12,855 group files (84.1 GB), head flipped 16:28 UTC, feed 859f2050 (287,980 upserts, 274,647 removes), archive
30.85 GB, ledger 11,138,384 ever. The carry fix held: 4 rows carried from 4 boards against 60,653 the night
before, with 360 boards confirmed emptied (the filtered dark job boards now count as read).

Two hand steps. Worker-5's host is slow on the bucket as well (its dark pre-scan took an hour, part 11 nearly two;
workers 0 to 2 do the pre-scan in ten minutes): the same slice was started as a helper on idle worker-1, which
built the seven remaining sources in 51 min while worker-5 kept its own accounting and skipped what the helper
had published. And pass 2 died on an R2 502 during the staging write, the second night running; the write is now
retried from a cleared prefix (50b71ae, deployed mid-run while the chain was down) and the checkpoint resume put
the tree back at pass 2 in three minutes. Timings: parquet 8 h 06 min (workers 95 / 183 / 198 / 460 min, dedup 22
min), diff 19 min, ledger 67 s, tree 2 h 50 min before the 502 plus 70 min from the checkpoint, estimators 2 h 36
min, finalize 29 s, history 32 s, feed 15 min, archive 35 min, retention 30 s. Objects 0, 1, 2 and 4 sit on good
hosts; 3 and 5 do not. Tomorrow: `PARQUET_WORKER_IDS=0,1,2,5` again unless a helper is cheaper, or three workers.

## Status (2026-09-24): the eighth cloud night, two bad hosts, 18.7 h, and four fixes

The 2026-09-23 consolidation ran 04:47 to 23:29 UTC on the 23rd: 7,435,736 -> 7,319,371 postings over three days
(+351,079 -406,791 ~51,932), 3,622,547 distinct vectors, 12,775 group files (84.2 GB), head flipped 22:41 UTC,
feed e6b1ead1 (403,011 upserts, 406,791 removes), archive 30.89 GB, ledger 10,937,191 ever. The night's story is
two Durable Objects whose containers sit on bad hosts. A DO's container lands on the same host every time.

- worker-3's host read the bucket ten times slower than the others (three nights running) and sat three hours at
  load 0 on the dark pre-scan; a restart under the same object landed on the same host. The slice moved to worker-4
  (`PARQUET_WORKER_IDS`, a02df63, picks the objects; tomorrow: 0,1,2,5 with the chain itself on worker-4).
- the chain's own object (`consolidate`) was three times slower at everything, disk and CPU included (PCA 183 s
  against 60, the split 59 min against 20). The stages from tree onward ran on worker-4's container by posting
  `container-chain.sh tree <date>` to `/run/worker/4`: every hand-off is in the bucket, so any container can run any
  stage. The run tap for that run is `/run/worker/4`, not `/run`.

Fixes, all deployed the same day: the parquet stage keeps the snapshot freeze when the fan-out fails (86cf9f0; the
unlock-stage fix of the 21st ran after the stage's own thaw, and the 70 s gap moved the dark layout by 8,839 rows);
a resume completes a partially published source from the published parts instead of rebuilding it (2ab8cf7,
coverage from the boards files 211688f: the job rows made every filtered-out board look uncovered); footer values
are decoded per file (0fc8791; the "utf-8 codec" error turned out to be DuckDB's binding choking on a binary R2
error body, not a bad footer); the diff treats a board present in today's boards file as read, so the five dark
job boards whose rows the aggregator rule filters out entirely (iitjobs.com alone 112k open) stop being carried
back in as 60k stale rows a night (6910403). Also: pass 2 died once on an R2 502 and once with the container
(nine minutes of silence after "Network connection lost": the first real loss since the post-age rule, read
correctly); both resumed from the checkpoint. The boards files of the leftover parts were rewritten by hand to
drop 10,908 rows duplicating the original parts (12,446 distinct dark boards, 12,446 rows). Timings on the good
host: tree 63 min from the checkpoint, estimators 2 h 19 min, feed 17 min, archive 31 min.

## Status (2026-09-21): the seventh cloud night, slow R2, two interventions, 14.6 h

The 2026-09-20 consolidation ran 02:35 to 17:14 UTC on the 21st: 7,416,508 -> 7,378,672 postings (+182,550
-163,322 ~18,250), 3,617,631 distinct vectors, 12,887 group files (85.7 GB), head flipped 15:31 UTC, feed 371cffa4
(200,800 upserts, 163,322 removes), archive 31.12 GB, ledger 10,673,975 ever. R2 reads were slow all day from the
container's region: worker 3's dark part 11 timed out once and took 43 min on the retry, greenhouse took 55 min
(10 the night before), the feed took 75 min (13). Two interventions, both fixed the same day:

1. Worker 3 exited 1 after 6.7 h with its whole slice published: the end-of-slice summary table (a per-ATS count
   read back from every jobs parquet in the bucket) timed out on another worker's file. The tables are for the log
   only; `show()` now prints "summary skipped" on a bucket error (75f9440).
2. The resume from parquet set out to rebuild all 15 dark parts: the failure path had thawed snapshot writes, boards
   rewrote snapshots in the 14 min before the resume, and the recomputed pack layout no longer matched the sidecars.
   Stopped the four workers and the coordinator 60 s into the first rebuild (no rebuilt part reached the bucket:
   every dark part still dated 03:09 to 06:24 UTC), released the lock, ran the dedup round by hand on worker 0
   (18 min; aggregator 6,048,410 -> 3,770,524) and resumed from diff. A parquet failure now keeps the freeze on so
   a resume inside the TTL sees the same snapshots (ff1bd8b). The chain's stages after that ran without a hand.

The diff carried 57,105 rows from 242 vanished boards (22 really empty), against ~11k on a normal night: three dark
job boards are 55k of it (iitjobs.com 44,597; scotjobsnet.co.uk 8,412; meinestelle.de 2,100), the crawler still
holds them open with status ok, and "our pull missed it": their snapshots were not in the parquet read. The carry
put them in the export and the tree, so nothing was lost; if it repeats, look at multi-part snapshots of very large
dark boards under the freeze. Timings: parquet 6 h 44 min to the first failure (workers 93 / 179 / 201 / 401 min),
dedup 18 min by hand, diff 19 min, ledger 63 s, tree 2 h 45 min (label pass 66 min), estimators 2 h 03 min, finalize
138 s (slow bucket), history 7 s, feed 75 min, archive 28 min, retention 14 s. Docker Desktop was not running on the
laptop for the first deploy attempt (the CLI symlink pointed at an unmounted DMG); `open -a Docker` and a symlink to
/Applications fixed it, noted in the runbook.

## Status (2026-09-20): the sixth cloud night, zero interventions, 10.5 h, and the search page back

The 2026-09-19 consolidation ran 00:40 to 11:09 UTC on the 20th with nobody touching it: 7,345,902 -> 7,405,788
postings (+320,699 -250,093 ~33,155; 10,720 carried from 212 vanished boards), 3,624,739 distinct vectors, 12,859
group files (85.5 GB; 341 oversized leaves split into 874 chunks), head flipped 10:34 UTC, feed faa2169a (353,854
upserts, 250,093 removes), archive 31.04 GB, ledger 10,541,162 ever. The fastest cloud night so far: parquet
4 h 55 min (workers 97 / 173 / 207 / 261 min; the dedup round 31 min), diff 21 min, ledger 80 s, tree 2 h 47 min
(label pass 72 min, fill 8 min, staging 17 min, group files 53 min), estimators 1 h 50 min, finalize 14 s, history
8 s, feed 13 min, archive 22 min, retention 10 s. The post-age rule from 3485aa6 fired once: worker 0's object
reported "Container connectivity was lost" at 01:56 UTC while the wrapper kept posting; the coordinator printed
"the process still posts output; waiting" and the worker published its last part 90 s later. Under the old rule
that slice would have been restarted mid-write.

Before the run, the search page was found dead and fixed (4523df0): since the 2026-09-14 cutover every search
had died at "finding the nearest groups" with "Cannot read properties of undefined (reading 'length')". The tree
appends the chunks of an oversized leaf at the end of the node list, so a small subtree's centroid rows are no
longer contiguous; the page's beam walk fetched rows by range, never loaded the chunk rows, and the dot product
hit an undefined row. The walk now fetches any child row the range missed. The page's inline scripts compiled
the whole time, which is why the deploy guard did not catch it: `scripts/site-smoke.js` (puppeteer-core, headless
Chrome) now runs the first example search and requires "Done" with jobs rendered, `cloud-deploy.sh` runs it after
the image is live (exit 4 on failure), and it is run by hand after every cutover (tonight: Done, 2,768 jobs from
4 groups on the new tree). The 3.5-day outage showed in the counters as searches near zero with page loads normal.

## Status (2026-09-19): the fifth cloud night, zero interventions, 12.5 h

The 2026-09-18 consolidation ran 00:20 to 12:51 UTC on the 19th (20:20 to 08:51 local) with nobody touching it:
the first night the chain did everything itself, including the dedup round the coordinator now runs on worker 0
after the last slice (31 min, 39.6 -> 30.7 GB). Four days since the last export, so the numbers are four days of
change: 6,941,630 -> 7,334,821 postings (+1,037,346 -633,074 ~99,661; 11,081 carried from 215 vanished boards),
3,620,891 distinct vectors, 13,054 group files (84.8 GB), head flipped 11:53 UTC, feed 4e7ead74 (1,137,007
upserts, 633,074 removes), archive 30.75 GB, ledger 10,266,666 postings ever recorded (past 10M on the lifetime
count; 7.35M open). Dark was 26,394,096 rows in 15 parts. Workers: 0 in 116 min (workday + four dark parts), 1 in
211, 2 in 237, 3 in 361 (dark parts plus greenhouse and paycom; the long pole for the third night running, so the
byte balance should weigh dark parts by their share, queued). Parquet 6 h 35 min, diff 26 min (36 parts, 8 GB),
ledger 61 s, tree 2 h 39 min (load 12 min, split 21 min, label pass 68 min, fill 7 min, checkpoint 19 s, staging
17 min, group files 48 min), estimators 1 h 52 min, finalize 14 s, history 5 s, feed 37 min, archive 21 min,
retention 11 s. No "Network connection lost" events this time. The laptop relays lost their link for 2.5 h in the
middle and caught up; the cloud did not notice. One thing seen: worker 0 spent 20 min after its last part in the
end-of-slice summary scan (reads its published parquet back from the bucket); worth a cheaper summary.

## Status (2026-09-16): the fourth cloud night, one lost worker, 13.4 h, and a lesson about "lost"

The 2026-09-14 consolidation ran 12:37 UTC on the 15th to 02:02 UTC on the 16th (08:37 to 22:02 local):
6,913,710 postings in the export (6,811,344 the night before; +388,837 -258,794 ~42,308 in the diff, 27,920 carried
from 166 vanished boards), 3,593,134 distinct vectors in the tree, 12,644 group files (80 GB), head flipped 01:18
UTC, feed d605f94e (431,145 upserts, 258,794 removes), archive 29.01 GB, ledger 9,298,367 postings ever recorded.
One intervention: worker 2's object reported "Network connection lost" at 14:10 UTC and the coordinator (before
2a9568a) would have waited forever, so I restarted the slice by hand; my first restart carried an empty `--ats`
(read off the coordinator's truncated echo of the command) and was stopped in seconds, which the coordinator
recorded as exit 143 and failed the stage on when the last worker finished five hours later. The dedup round ran by
hand on worker 0 (33 min; aggregator tier 5,723,648 -> 3,320,270 after dedup against first-party boards) and the
chain resumed from diff. Workers: 0 in 94 min (workday alone), 1 in 227 min, 2 (restart) in 209 min, 3 in 389
min; the slices are byte-balanced on input, and dark parts plus paycom (125k jobs in 42 min: long bodies) made
worker 3 the long pole. Diff 29 min, ledger 77 s, tree 3 h 07 min (load 15 min, PCA 73 s, split 24 min for 24,339
nodes, the label pass 85 min, fill 8.5 min for 3,346,759 aggregator rows, checkpoint 35 s, staging 19 min, group
files 52 min), estimators 1 h 57 min, finalize 18 s, history 17 s, feed 16 min, archive 27 min, retention 12 s.

The lesson. During the tree stage the chain object logged "Network connection lost" and its state flipped to
stopped; the relay called the run dead, the checkpoint prefix was empty, and I posted a chain from tree. The
container was alive the whole time: the wrapper's two-minute output posts kept landing (host lines at 21:01 and
21:03 after the 20:59 error), and the library's `start()` takes a fast path when the container is running, so my
post started nothing and only relabeled `current`. The same event fired three more times that night (23:50, 00:17,
00:47) and the process never died; the run finished and `onStop` fired normally. So: a "Network connection lost"
error event is the object losing the container's stream, not the container dying. The only evidence of death is
the posts stopping. Fixed in 3485aa6: the fan-out counts a worker as lost only when its posts have been silent
for 6 min (and takes the wrapper's final post, a real exit code, as the exit when no stop event was recorded); the
object refuses a new start while interim posts are fresh, naming the reason. Whether worker 2 in the morning was
truly lost is unknown: its restart ran under the old rule and the same-day skip kept the parts consistent either
way. The relay now keys STALE on post age, not object state. The label pass (exemplars per node: a small k-means
plus random reads into the 11 GB memmap, single-threaded) is the tree's slowest single step and is queued below.

## Status (2026-09-14): the third cloud night, two interventions, 11.5 h

The 2026-09-13 consolidation ran 19:18 (restart 20:15 after a NameError in the new `--ndjson-only` pass and a
Docker Desktop disk reset) to 06:44 local: 6,811,344 postings (6,670,670 the day before; net growth is settling
near +135k/day as the dark backfill drains), 12,647 group files (79 GB), head flipped 10:15 UTC, feed 8ef452c2
(268,693 upserts, 110,740 removes) streamed in the cloud, archive 28.47 GB. Fresh governmentjobs and jobscore
exports for the first time since the 10th; vanished boards down from 214 to 134. Two interventions: the dedup
round's one-row-per-key rule was a window over the wide part and ran DuckDB out of its cap (now found on the key
columns alone; it dropped 1,316 duplicate rows the old snapshot writer had left), rerun by hand on a worker and the
chain resumed from diff; and my laptop's Docker Desktop VM disk had filled (268 GB) and went read-only before the
first deploy of the evening (reset; the deploy script now prunes the build cache). Parquet 4.3 h (workers 2.1 to
4.3 h), diff 21 min, ledger 53 s, tree 2 h 46 min with pass 2 first try, estimators 1 h 48 min, finalize 14 s
without the flat mirror, history 13 s, feed 7 min, archive 21 min, retention 9 s.

## Status (2026-09-13): the second cloud night, and the first with a ✅ from the container itself

The 2026-09-12 consolidation ran in the cloud from 23:05 to 15:08 local (16 h): 6,669,472 postings (6,322,291 the
day before), 12,633 group files (76.5 GB), head flipped ~18:14 UTC, feed generation 948913ba (570,506 upserts,
185,943 removes) built with streamed pages, archive 27.83 GB. Four interventions, each a fix, a deploy and a resume:

- 04:13 the parquet coordinator died on a transient read error polling a worker (the dedup on the worker finished
  on its own): polls retry now, and an uncaught exception records a failure (the report had ticked the dead stage).
- 07:02 the tree was OOM-killed at the last rows of the fill, before the checkpoint: the 3.6 GB projection is freed
  before the fill (it went 7.4 -> 3.8 GiB resident going in). Rebuild 2 h.
- 09:34 pass 2 OOM-killed in the staging write: node centroids and key arrays freed once used, staging row groups
  and partition flushes halved, `TREE_PASS2_MEMORY=5GB`. Resumed from the checkpoint in 2 min.
- 10:11 the group pass hit a repeated position: one dark board's snapshot carried a job twice (12 rows). Two
  attempts to filter the duplicates in the scans flipped the staging join's build side and spilled the disk; the
  scans are plain again, the group pass skips a repeated position, and the dedup round keeps one row per key.

Parquet: 5 h for 25.8M dark snapshot rows in 14 parts plus 33 sources, single dedup 5,425,377 -> 3,073,084
aggregator rows in 20 min. Diff 25 min first try. Estimators 2 h. Finalize 11 min (9 of them the flat mirror).

## Status (2026-09-12): the first night run entirely in the cloud

The 2026-09-11 consolidation ran end to end on Cloudflare Containers (standard-4, 4 vCPU / 12 GiB / 20 GB), no
laptop in the loop: 6,320,453 postings (4,452,644 the day before), 12,438 group files (72.6 GB), the head flipped at
00:59:10 UTC on 2026-09-13. It took 27 hours instead of four. Stage timings on this box, for the record:

| stage | wall clock | notes |
|---|---|---|
| parquet | ~7 h | 4 workers; dark 24.1M snapshot rows in 14 parts, 12-27 min/part; the footer read 13-74 min per worker (bucket contention); single-pass dedup 12 min |
| diff | 30 min | `DIFF_MEMORY=7GB DIFF_THREADS=2` (6 GB/8 threads was OOM-killed; 4 GB ran DuckDB out); carry step needs the slug filter |
| ledger | 2.5 min | derived; parts uploaded on write |
| tree | 105 min to the checkpoint + 70 min pass 2 | load 13 min, PCA 1, bisection 20, fill 65 (1.7 GB free at peak); staging 18-27 min at `TREE_PASS2_MEMORY=6GB`; 12,438 files in 40 min |
| estimators | 110 min | salary scan sampled 8% (bernoulli); the extractor is single-threaded, ~10k rows/min |
| finalize | 11 min | 9.5 min of it the flat mirror copy |
| history / archive / retention | 32 s / 21 min / 8 s | |
| feed | failed once (tempfile on the root overlay), rerun after the TMPDIR fix | |

What broke and what changed (all in the image now): a deploy rolls over every running instance, so the deploy script
refuses while a run is busy; the wrapper is PID 1 and ignored SIGTERM, so it traps and forwards it; the same-day
resume of a dark part is layout-aware (pack sidecar); slice workers no longer run the dedup and the dedup round is
one worker (parallel dedup tore reads); DuckDB's pool is released after the vector load and the S3 upload parts are
5 MB (two OOM kills in the tree); the tree checkpoints after the fill and resumes at pass 2 (kept until finalize);
the container is the process, so every hand-off goes through the bucket; the trainers sample with bernoulli
(row-count samples are reservoirs); history never uploads a placeholder; TMPDIR is on the work volume.
Done the same night (ea82ff0): **the feed streams its pages to the bucket** (`--stream-pages`, passed by stage.py
in r2 mode): each page is verified from memory as verified_rows() would, put under a staging prefix, and
server-side copied under changes/<generation>/ once the header is known; publish() confirms every page's size in
the bucket before the head moves. The local path (laptop) is unchanged. Three ENOSPC failures on 2026-09-12
(2,313,018 events with text, more than the 20 GB volume holds) forced it; the 2026-09-11 feed itself was built on
the laptop against the bucket. Fixed 2026-09-13 (8da7abf, next): the "vanished" governmentjobs/jobscore boards were real: those two sources are
pulled through the API into ndjson on the coordinator's disk, and since the fan-out no worker converted them, so the
diff saw every board gone, asked the crawler, and carried the previous copy forward two nights running (three boards
lost on the way). The coordinator now converts them itself (`build-parquet.py --ndjson-only`) before the fan-out.
Also fixed: the snapshot writer paged parts by OFFSET while rows changed between the awaited puts, so a job could
land in two parts (keyset paging now); the unlock stage keeps the freeze while a worker is still converting; the
flat mirror is off by default (the Worker resolves flat ids through the head). Queued: delete the stale flat copies
(groups/<id>.json, ~76 GB) once nobody reads them straight from the bucket (the Worker resolves flat ids through the head;
tonight proved it); work stealing in the parquet fan-out; the salary extractor across the cores; an estimators step
the chain fans out itself; a cron trigger for the nightly chain. Added 2026-09-16: the tree's label pass across the cores (85 min single-threaded: per-node k-means plus random reads into the memmap; the nodes are independent), and a run-object endpoint that returns "alive" from post age so relays and the coordinator share one rule.

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
matches. `cloud-deploy.sh --wait` does only the wait-and-verify half. After the image is live it runs
`scripts/site-smoke.js` (headless Chrome: the first example search must reach "Done" with jobs rendered; exit 4 if
not). Run the smoke by hand after every cutover too: the 2026-09-14 tree appended chunk nodes and every search died
at the tree walk for 3.5 days, on a page whose scripts compiled fine. Never poll a rollout by starting the container. **A deploy is a rollout that stops every running instance of the old image** (the journal shows
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

**The container is the process (learned 2026-09-12, cost a tree recompute).** An instance stops when its entrypoint
exits and /work goes with it, so a `from <stage>` resume always starts with an empty work dir. Finalize in a fresh
container found no manifest.json/centroids.bin (the tree had written them in an earlier chain process), and the feed
and retention stages would have found no diff sidecar. Since commit f52b2f4 every hand-off goes through the bucket:
the tree and estimator stages put their outputs in `tmp/<date>.web/`, the ledger stage uploads its parts as soon as
they are written, and finalize/history/feed/retention restore whatever they are missing from the bucket before they
run (finalize deletes `tmp/<date>.web/` and the tree checkpoint `tmp/<date>.tree/` after a successful publish). The
tree stage also takes `TREE_GROUPS_PUBLISHED=1`: when this build's group files are already in the bucket (the build is
deterministic), it verifies they are exactly the manifest's leaves, records their sizes, hands off and skips pass 2.


**Done 2026-09-12 (commit 45f17ac): the tree checkpoints between the fill and pass 2.** Every pass 2 failure on
2026-09-12 (OOM in the staging write, twice) replayed the vector load (12 min), PCA, the bisection (18 min) and the
fill (an hour) to get back to the line that broke: about 90 minutes per attempt. The state pass 2 needs is small: the
row order, the node list with centroids and labels, the leaf boundaries after the split, and the hash-to-position
table (a few hundred MB). build-manifest.py writes it to the bucket (tmp/<date>.tree/, meta.json last so its
presence means complete, signed with the export's jobs file list) right after the manifest, and at the next start
of the stage a complete checkpoint for the same export is picked up on its own and the build resumes at the staging
write; a successful pass 2 deletes it. A staging fix now costs a minute to retry, not ninety.


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

# open-jobs backend

A Cloudflare Worker + Durable Objects crawler that fetches every job board in `../slugs.json`
once a day, keeps a diffed snapshot per board, and lets you pull everything down to a laptop
for consolidation (NDJSON → parquet). No central database: each board is its own Durable Object.

Production: `https://backend.dehnbostele.workers.dev`

## Architecture

```
cron 0 0 * * *  ──►  scheduled()  ──►  Registry DO (one per ATS)
                                         └─ alarm loop: 250 slugs/tick, 25 concurrent
                                            └─ Board.ensureScheduled(name)  (idempotent)

Board DO (one per "ats/slug", 64k of them)
  ├─ fixed daily slot  = hash(name) % 24h            (deterministic, spreads load evenly)
  ├─ alarm()  ──►  fetcher.fetchJobs(slug)  ──►  diff vs SQLite  ──►  enrich pending  ──►  re-arm
  └─ SQLite: jobs (snapshot + history), runs (per-fetch diff counts), meta (KV)
```

### `Board` (`src/board.ts`)
- **Naming**: `env.BOARD.getByName("greenhouse/stripe")`. `ats` and `slug` are parsed from the name.
- **Scheduling**: on first touch the board picks `slotMs = fnv1a(name) % 86_400_000` and stores it in
  `meta`; it never changes. The first fetch is jittered within an hour of arming; afterwards the
  alarm fires at the slot every day (computed from UTC midnight, so no drift). One alarm per DO:
  `meta.nextFetchAt` is the fetch time, and `arm()` picks the sooner of that and a 1-minute
  enrichment tick when there is pending enrichment work.
- **Detail fetch** (`runDetails`): providers whose listing has no description implement
  `fetchDetail(slug, job)`; new jobs without `content` get `detail_status = 'pending'` and the Board
  fetches the full posting **once per job** (150 per tick, 6 in flight, minute follow-up alarms
  until the backlog is empty; `error` rows retry after a day; `na` = no endpoint / posting gone).
  Detail data is stored in its own `detail` column and merged over the listing at read time, so the
  listing `content_hash` diff is unaffected. Enrichment waits for a job's detail to be done.
- **Freshness**: every completed fetch (armed or forced) stamps `meta.lastRunAt`. A board that
  completed a non-error fetch within `DEFAULT_FRESH_MS` (6 h) is fresh: the daily alarm skips it
  and rolls to the next slot, and `/fetch-all` skips it unless `skipRecent=0`. So a force fetch
  just before a board's slot (or the reverse) never hits the provider twice.
- **Failure handling**: fetch errors and `gone` results are recorded, never thrown (so the alarm
  is not retried in a tight loop). After `BACKOFF_AFTER = 5` consecutive failures the board runs
  weekly instead of daily.
- **Diffing** (`applySnapshot`): jobs are keyed by provider id with a `content_hash` of the
  normalized job. Each fetch classifies added / changed / unchanged / removed. Removed jobs are
  **kept** with `removed_at` set (history for parquet); a job that reappears is revived. New jobs
  get `enrich_status = 'pending'`. Enrichment is **one-shot**: a job that is `done` is never
  re-queued, even if its content changes later (`changed_at`/`content_hash` still update, so a
  consumer can detect drift). Every fetch inserts a `runs` row with the counts.
- **Storage schema**:
  - `jobs(id, data JSON, content_hash, first_seen_at, last_seen_at, changed_at, removed_at, enrich_status, enriched_at, enrichment JSON, enrich_error, detail_status, detail JSON, detail_error, detail_fetched_at)`
  - `runs(id, run_at, status ok|gone|error, added, changed, removed, unchanged, error)`
  - KV `meta`: `BoardMeta` (slot, last run/ok, status, error, failures, jobCount, nextFetchAt, nextAlarmAt)

### `Registry` (`src/registry.ts`)
One per ATS, named by the ATS key. `sync(ats)` stores a cursor and walks the slug list in
alarm-driven chunks (250 slugs per tick, 25 concurrent stub calls, one retry per slug for
transient platform errors), so no single invocation ever needs thousands of subrequests.
`status()` exposes progress. Re-running `sync` while one is in flight is a no-op.

### Fetchers (`src/ats/`)
Every provider is a file exporting `<ats>: AtsFetcher` with `fetchJobs(slug) → {status:"ok", jobs} | {status:"gone"}`
(throw on anything else). Jobs are normalized to `Job` (`src/ats/types.ts`):
`id, title, location, url, departments[], publishedAt, updatedAt, content (HTML|null), raw`.
Registration is the `fetchers` map in `src/ats/index.ts`; only registered providers get
Registry sweeps / DOs. Each file's header comment documents endpoints, pagination and caveats.

| ATS | Slug is | Content in list? | Notes |
|---|---|---|---|
| ashby | board name | yes | single call |
| breezy | subdomain | via `fetchDetail` (job page ld+json) | many dead portals |
| comeet | company name | yes | **disabled** — needs `src/comeet-uids.json` (see below) |
| crelate | portal name | snippet; full via `fetchDetail` (`GetJob`) | |
| dayforce | `ns` or `ns/site` | yes | bare slug ⇒ `ns/CANDIDATEPORTAL`; ~35% of bare slugs are unmappable site codes → gone |
| eightfold | subdomain | via `fetchDetail` (untested: WAF-blocked during dev) | 10/page, rate-limit sensitive |
| gohire | `name-<hash>` or `name-<id>` | via `fetchDetail` (`getJobId`) | numeric ids need a cached 1.6 MB sitemap lookup |
| greenhouse | board token | yes | |
| icims | subdomain | snippet; full via `fetchDetail` (job page) | HTML scrape; login/SSO boards throw |
| jobscore | careers slug | yes | **local-only**: blocks Cloudflare IPs (403 from Workers); fetched from the laptop by `scripts/fetch-local.mjs` |
| jobvite | company | via `fetchDetail` (job page) | HTML scrape |
| lever | site name | yes | |
| oraclecloud | hostname | partial; full via `fetchDetail` (`recruitingCEJobRequisitionDetails`) | discovers active sites; bogus hosts take ~23 s to fail |
| paycom | 32-hex clientkey | via `fetchDetail` (`job-postings/{id}`, cached JWT) | JWT from career page, then search API |
| paylocity | GUID | via `fetchDetail` (details page JSON-LD) | |
| personio | subdomain | yes | XML feed |
| pinpoint | subdomain | yes | |
| recruitee | subdomain | yes | |
| recruiterbox | client name | yes | |
| smartrecruiters | company id | via `fetchDetail` (`/postings/{id}`) | 0 results ⇒ probe careers page to distinguish gone/empty |
| taleo | subdomain | via `fetchDetail` (jobdetail.ftl state blob) | default career section only; `tbe` (Business Edition) unsupported |
| workable | account subdomain | yes | widget API |
| workday | hostname | via `fetchDetail` | sites discovered from robots.txt; 20/page; full JD from `/wday/cxs/.../job/...` once per job |
| ukg, successfactors | datacenter/CDN hostnames | — | **not enabled**: slugs carry no company id; stubs throw |

### Board (company) enrichment (`src/company.ts`, `src/openai.ts`)
Identifies the company behind a board — name, homepage, careers/LinkedIn URLs, HQ, industry,
staffing-agency flag, size — with one OpenAI **Responses API** call using **Structured Outputs**
(strict JSON schema, `company_v1`) and the hosted `web_search` tool (`gpt-5.6-luna`, reasoning effort `none`).
Free signals go in first: job-link hostnames minus ATS domains become `candidate_domains`, and the
slug becomes `candidate_name`; the model only searches when those aren't conclusive (Stripe resolves
without a search; most others use 1–2 queries, ~10–17k input tokens). Result lives on
`meta.company` (with `sources`, `searches`, `usage`) and is **one-shot** — never re-resolved unless
`?force=1`; failures land in `meta.companyError` and are retried on the next successful fetch.
Automatic resolution after each board's first successful fetch is gated by the `BOARD_ENRICH` var
(`"off"` by default: ~40k live boards × ~$0.01–0.015 ≈ a few hundred dollars once; flip to `"on"`
to run it fleet-wide, or call the endpoint per board). Field definitions: `FIELDS.md` §1.
Requires the `OPENAI_KEY` secret.

### Embeddings (`Board.runEmbed`, `src/openai.ts`)
Every live job is embedded **once** (`text-embedding-3-small`, 1536 dims) in batches of 100 per
alarm tick, after its detail fetch, when
the `EMBED` var is `"on"` (default). Stored as a float32 BLOB (`embedding`, `embed_model`,
`embed_status`). `POST /boards/:ats/:slug/embed` embeds a whole board immediately. Export with
`?embed=1` / `--embed` (≈6 KB per job in JSON; `embedding FLOAT[]` in parquet). Cost ≈ $0.02 per
1M tokens at ~1.2k tokens/job → ~$45 for the current 1.9M jobs. **What is embedded**: labelled
lines for company (resolved name or slug-derived), title, location, departments, posted date,
industry / staffing flag when known, source ATS, then the **full JD text** (capped at 28k chars ≈
the model's 8k-token input) — the whole record, so classifiers can be trained on the vector alone.
The recipe is versioned (`EMBED_TAG`, e.g. `text-embedding-3-small:1536:v3`); bumping it makes
boards re-embed jobs tagged with an older recipe. The org-wide embeddings limit is
10M tokens/min: on a 429 a board leaves its rows pending and backs off 1–5 min (jittered), so a
fleet-wide backfill self-paces at the cap (~7k jobs/min ≈ 4–5 h for everything). Changing model
or dims requires a re-embed.

### Job enrichment (`src/enrich.ts`, `src/jobschema.ts`)
Structured extraction of FIELDS.md §2 (`job_v1`, strict JSON schema) with one Responses API call
per job on `OPENAI_MODEL`, no tools, reasoning `none`, JD capped at 12k chars. **One-shot** per job.
Two entry points:
- **Lazy (default)**: `POST /jobs/enrich` with `{"jobs":[{"ats","slug","id"},…],"force":false}` —
  idempotent: jobs already `done` are returned from storage (`cached: true`), the rest are
  extracted now (4 concurrent per board) and stored. The board's company is resolved first if it
  hasn't been (one-shot, cached on `meta.company`, used as context) and returned under `boards`. Intended flow: filter by embeddings first,
  then enrich only the candidates. Upper bound on spend = enriching everything.
- **Automatic**: set `JOB_ENRICH` to `"on"` and the alarm enriches every new job
  (`ENRICH_BATCH = 20` per tick, minute follow-ups). Off by default.
Errors land in `enrich_error` / `enrich_status = error`; re-queue with
`POST /boards/:ats/:slug/retry-enrichment` or re-call `/jobs/enrich` (errors are retried, `done` is not).

## Local-first client (JobScream)

`web/` is a static app served by the Worker at `/` (Workers static assets). Flow: paste a résumé →
`POST /embed` (the only server call that sees user text; IP rate-limited) → the client loads the
**tree manifest** and walks it from the résumé vector to show the nearest job *groups* (medoid,
exemplars, size, spread) → **Maybe** downloads that group's file, **No** skips it (and seeds hard
negatives) → screen 3 labels jobs with `J`/`K` and refits a logistic regression in the tab, live →
**Export** yields `{recipe, w[1536], b, groups_maybe, labels}` — the user's search as a weight vector.

Data for the client lives in the `jobscream-data` R2 bucket, served at `GET /data/*` (Range
supported, CORS open):
- `manifest.json` — recursive-bisection tree over all embedded open jobs: nodes with `lo/hi` row
  range (DFS order), `radius`, `size`, `label`, `medoid`, `exemplars`, `children`; plus `recipe`.
- `centroids.bin` — float16 `[nodes × dims]` unit centroids, same order as `manifest.tree`.
- `groups/<leaf>.json` — jobs of one leaf (ats, slug, id, title, company, location, url, seen,
  jd text ≤ 4k chars) with exact float32 embeddings (`v`, base64 little-endian).
Build + publish: `uv run scripts/build-manifest.py` (from `export/jobs/*.parquet` pulled with
`--embed`) then `scripts/upload-web.sh`. Rebuild whenever the embedding recipe or the corpus changes.
Experiment / evaluation of the tree: `scripts/experiments/tree.py`.

## HTTP API

All endpoints are optionally protected by `Authorization: Bearer <ADMIN_TOKEN>`; with the
`ADMIN_TOKEN` var empty (current state) they are open. Cloudflare's bot rules 403 the default
Python `urllib` user agent — send any custom UA (curl is fine).

| Method | Path | Purpose |
|---|---|---|
| POST | `/enrich` | **public**, per-IP metered (`ENRICH_HOUR_USD` 5 / `ENRICH_DAY_USD` 50, actual token + web-search cost; cached results free): body `{jobs:[{ats,slug,id}…]}` (≤ 300) → `{boards:{name:{company,…}}, jobs:{key:{status,enrichment,cached}}, cost:{thisCallUsd,hourUsd,dayUsd,…}}`; 429 + `retry-after` when a window is exhausted (cached part still returned). Runs job extraction + company resolution, both one-shot |
| GET | `/enrich/budget` | this IP's hour/day spend and limits |
| POST | `/embed` | **public**, IP rate-limited (`EMBED_RATE_LIMIT` per `EMBED_RATE_WINDOW_MS`, default 10 / 10 min): body `{text, title?, location?}` → `{vector[1536], recipe}`; 429 with `retry-after` when limited, 503 when the embeddings API is saturated |
| GET | `/data/<key>` | **public**: object from the `jobscream-data` R2 bucket (manifest, centroids, group files), Range + CORS |
| GET | `/ats[?all=1]` | providers fetched by the Worker fleet → slug counts; `all=1` includes local-only ones |
| POST | `/sync` | start a Registry sweep for every enabled ATS (what the cron does) |
| GET | `/sync/:ats` | sweep status: `mode`, `cursor/total`, `touched`, `fetched`, `skipped`, `errors`, `lastError`, `finishedAt` |
| POST | `/backfill[?ats=a,b]` | kick every board with a detail/embed/enrich backlog so it drains now (per-board minute ticks); progress via `/sync/:ats` (`fetched` = kicked) |
| POST | `/fetch-all[?ats=a,b][&skipRecent=<ms>]` | on-demand fetch of every board (arms if needed) via the Registry sweep in `fetch` mode. Boards that are *fresh* — completed a non-error fetch within `skipRecent` (default 6 h; `0` forces) — are skipped. Does not change daily slots. Progress via `/sync/:ats` |
| GET | `/boards/:ats/:slug[?filters]` | `{meta, jobs}` for one board |
| GET | `/boards/:ats/:slug/runs` | last 30 fetch runs with diff counts |
| POST | `/boards/:ats/:slug/fetch` | arm (if needed) and fetch now; returns meta |
| POST | `/boards/:ats/:slug/retry-enrichment` | reset `error` enrichments to `pending` |
| POST | `/jobs/enrich` | body `{"jobs":[{"ats","slug","id"}…],"force"?:bool}` → `{boards: {"ats/slug": {company, companyError}}, jobs: {"ats/slug#id": {status, enrichment, cached}}}`; idempotent lazy enrichment; resolves the board's company first (cached) |
| POST | `/boards/:ats/:slug/embed` | embed all un-embedded jobs on the board now |
| POST | `/boards/:ats/:slug/ingest` | body = `{status:"ok", jobs:[Job…]}` \| `{status:"gone"}` \| `{status:"error", error}` fetched off-Cloudflare; runs the normal fetch pipeline (local-only ATSes) |
| POST | `/boards/:ats/:slug/enrich-board[?force=1]` | resolve the company behind the board now (OpenAI Responses + web_search, one-shot unless `force`) |
| GET | `/export/:ats?offset=0&limit=200[&filters][&skipEmpty=1]` | NDJSON, one `{ats, slug, meta, jobs}` line per board; headers `x-total`, `x-next-offset` |
| GET | `/comeet/resolve/:slug` | resolve a comeet slug → `{name, uid}` from the edge (used by the map builder) |

Slugs containing `/` (dayforce) must be URL-encoded (`%2F`).

**Job filters** (query string, apply to `/boards/...` and `/export/...`; evaluated in SQL inside each DO):

| Param | Values | Meaning |
|---|---|---|
| `status` | `open` \| `removed` \| `all` (default) | `open` = still listed on the board; `removed` = has `removedAt` |
| `enrich` | `pending` \| `done` \| `error` | enrichment status |
| `since` | epoch ms | jobs seen, changed, or removed at/after this time (incremental pulls) |
| `slim` | `1` | drop `raw` and `content` from each job |
| `embed` | `1` | include the embedding vector per job |
| `ids` | `a,b,c` | only these job ids |
| `skipEmpty` | `1` | (`/export` only) omit boards with no matching jobs |

## Workflows

### Deploy
```sh
npm run build:boards        # regenerate src/boards.json from ../slugs.json (dedupes; run when slugs change)
npx wrangler types          # after changing bindings/vars
npx tsc --noEmit
npx wrangler deploy
```
Migrations: `v1` created the scaffold's `MyDurableObject`; `v2` deleted it and created `Board` + `Registry`.
Adding a DO class = new migration tag. Deploying mid-sweep is safe: Registry/Board state is in storage.

### Bootstrap / re-arm the fleet
```sh
curl -X POST https://backend.dehnbostele.workers.dev/sync
curl https://backend.dehnbostele.workers.dev/sync/greenhouse
```
Runs automatically at 00:00 UTC. It is idempotent: boards that already have an alarm are untouched,
boards that lost one (or failed to arm) are re-armed. New slugs in `boards.json` get created here.

### Force-fetch the whole fleet now
```sh
curl -X POST "$B/fetch-all"                        # every board not fetched in the last 6h (default)
curl -X POST "$B/fetch-all?skipRecent=0"           # everything, unconditionally
curl -X POST "$B/fetch-all?ats=lever,ashby"        # subset
curl $B/sync/lever                                  # progress: fetched / skipped / errors
```
Same code path as the daily alarm fetch (diff + enrichment queue), just on demand. ~100 boards
per Registry tick, 6 concurrent per ATS (workable returns 429 at higher concurrency); a full
fleet pass takes a few hours. The normal daily schedule spreads each provider over 24h instead.

### Backfill JD details / embeddings now
```sh
curl -X POST "$B/backfill"                # all ATSes; or ?ats=workday,paycom
curl $B/sync/workday                      # fetched = boards kicked, skipped = nothing pending,
                                          # pendingDetails / pendingEmbeds = backlog tallied during the sweep
```
Boards drain their own backlog on minute ticks (150 detail requests + 100 embeddings per tick,
6 detail requests in flight per board), so a 2,000-job workday tenant finishes in ~15 min and the
fleet in about an hour. Without a kick the same work happens at each board's next daily slot.


## Daily consolidation (pull everything → parquet → manifest → R2)

One command: `scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload]`. It writes
`export/<YYYY-MM-DD>/` (never touches earlier days), repoints `export/latest`, and streams an
unbuffered log to `logs/consolidate-<date>.log`. Every stage is re-runnable; finished ATSes are
skipped via `.done` markers, and interrupted exports resume from their last complete page.

### The stages, and what each one taught us

1. **Ingest local-only ATSes** (`fetch-local.mjs --ingest`). Jobscore 403s Cloudflare IPs, so the
   laptop fetches it and POSTs each snapshot to `/boards/jobscore/:slug/ingest`; the Board DO then
   runs the normal diff/embed/enrich pipeline. Do this *first* so those jobs are embedded and land
   in the same pull as everything else. ~1 min for 106 boards.

2. **Pull** (`pull-pool.py … -- --status=open --embed --resume`). All 22 ATSes at once: each board
   is its own DO and the Worker scales, so the only limits are the laptop's disk/CPU. Observed:
   ~35 GB of NDJSON for 1.96M jobs with JD bodies and 1536-float vectors; workday alone is 32 GB
   and ~30 min, everything else finishes inside that. Things that bit us, all fixed in code but
   worth knowing:
   - Vector pages are **5 boards** (`export.mjs` picks this when `--embed`); big boards are paged
     **300 jobs per DO call** and emitted as multi-part lines (`part`, `more`), because a single
     board's jobs with vectors exceed the DO RPC response limit (~32 MB).
   - The export stream is **pull-driven** (`ReadableStream.pull`, one-board lookahead). Pushing
     chunks eagerly OOMed the 128 MB isolate on pages of big boards; the client's read pace now
     bounds Worker memory.
   - The Worker's CPU limit is raised to **5 min** (`limits.cpu_ms`); a page of JD + vector JSON is
     seconds of `JSON.stringify`, and the default 30 s cut streams off with "other side closed".
   - `export.mjs` verifies each page's distinct-board count against `x-page-boards` and retries;
     a board that fails inside the Worker is emitted as `{error}` rather than killing the stream.
   - `--resume` streams the existing file (never loads it into one string: Node caps strings at
     512 MB), truncates after the last complete page, and continues; the page size used is kept
     in `<file>.page`. Use `NODE_OPTIONS=--max-old-space-size=16384` for multi-GB files.
   - Don't use `xargs -I{}` with long commands on macOS ("command line cannot be assembled");
     `pull-pool.py` is a plain Python worker pool. Don't run two pools on the same output dir.
   - Never `rm` an export you haven't listed first. Each day is its own directory precisely so
     cleanup is a deliberate, separate step (`ls export/`, then remove old days by hand).

3. **Parquet** (`build-parquet.py`, `EXPORT_DIR=<dir>`). Streams each ATS: a Python pre-pass splits
   per-board lines into per-job JSONL (a board line can be 100+ MB; DuckDB's reader can't take
   that), then DuckDB writes `jobs/<ats>.parquet` and `boards/<ats>.parquet` with an explicit
   `columns=` schema (no struct inference; missing keys become NULL). Bounded memory
   (`memory_limit 10GB`, 2 threads, 20k-row groups); ~25 min for the full set, 8.1 GB out.
   Multi-part boards repeat `meta` on every part; the board row is emitted from `part 0` only.
   Query with `read_parquet('export/latest/jobs/*.parquet')`.

4. **Manifest** (`build-manifest.py`, `EXPORT_DIR=<dir>`). Streams all `is_open AND embed_status =
   done AND embed_model = <dominant recipe>` vectors in record batches into a preallocated float32
   array (~12 GB for 2M × 1536; text columns stay in Arrow; peak ~20 GB — never copies the full
   matrix, big nodes use chunked dot products and sampled sub-medoids), PCA-256 on a 50k sample, recursive 2-means bisection (stop at ≤ 400
   members or radius ≤ 0.30), DFS row order, then per node: label (top title words), medoid,
   sub-cluster medoids as exemplars (title-distinct), radius, `distinct_titles`. Writes
   `web/manifest.json`, `web/centroids.bin` (float16), `web/groups/<leaf>.json` (jobs with JD text
   ≤ 4k chars and exact float32 vectors, base64). Tree stats worth eyeballing in the log: leaf
   count (expect ~3–5k), median leaf size, max leaf size (a huge leaf under the radius rule = one
   employer's location-replicated postings, harmless), purity if you run
   `scripts/experiments/hull.py` on a sample.

5. **Upload** (`upload-web.sh`, `EXPORT_DIR=<dir>`). Puts `groups/*` first, then `centroids.bin`,
   then `manifest.json` last, so a client never sees a manifest whose groups aren't there yet.
   Objects are served at `/data/*` with `cache-control: max-age=3600`; group ids change every
   build, so stale caches only ever miss, never mismatch. (Old group files accumulate in the
   bucket; prune by listing keys not referenced by the current manifest.)

### Before you run it
- The fleet should be quiet: `POST /backfill` and wait for a sweep with `pendingDetails`/
  `pendingEmbeds` ≈ 0 (`GET /sync/:ats`), otherwise the pull captures half-embedded boards.
  The daily cron handles steady state; a big recipe change (new `EMBED_TAG`) needs a full
  backfill first (~5 h at the 10M TPM embeddings cap).
- Disk: ~35 GB NDJSON + ~8 GB parquet + ~5 GB web per day. Keep 2 days.
- Time: ~3 h end to end at 2M jobs (pull ~70 min with the 8.7k-job Oracle tenants and Workday,
  parquet ~25, manifest ~10, upload ~85 min for ~7k group files at 16 workers).

### Verifying a build
```sql
-- coverage: bodies and vectors per ATS
SELECT ats, count(*) jobs, count(*) FILTER (length(content)>800) with_body,
       count(*) FILTER (embed_status='done') embedded
FROM read_parquet('export/latest/jobs/*.parquet') GROUP BY 1 ORDER BY 2 DESC;
```
Expect ≥ 90% bodies overall (workday/greenhouse/paycom ~99%). Known low-body providers to
watch: paylocity, eightfold, crelate, gohire (detail endpoints returning "unavailable" more than
expected — investigate before trusting those slices).

### When something breaks mid-run
- A single ATS failed in the pull: just re-run `consolidate.sh` (or `pull-pool.py --ats <a> … --resume`); markers skip the rest.
- Parquet failed on one ATS: fix, re-run `build-parquet.py`; outputs newer than their NDJSON are skipped.
- Manifest OOM: it needs ~15 GB free; close DuckDB/other builders first.
- Never chain a delete after an unverified guard; list, then delete, in separate steps.

### Pull everything to the laptop → parquet (manual pieces)
```sh
scripts/pull-all.sh [worker-url] [export flags]   # worker ATSes (4 in parallel) + local-only ATSes, then parquet
# or per ATS:
npm run export -- greenhouse https://backend.dehnbostele.workers.dev --status=open --skip-empty
# flags: --status=open|removed|all  --enrich=pending|done|error  --since=<epoch ms>  --slim  --skip-empty
# env:   ADMIN_TOKEN=...  WORKER_URL=... (default base)
uv run scripts/build-parquet.py                    # export/*.ndjson -> export/jobs.parquet + boards.parquet
```
Providers in `localOnlyAts` (`src/ats/index.ts`; currently jobscore, which 403s Cloudflare IPs) are
not fetched by the Worker fleet. `scripts/fetch-local.mjs --ingest=<worker-url>` runs their
fetchers on this machine and POSTs each snapshot to `/boards/:ats/:slug/ingest`; the Board DO then
runs the **same pipeline as an online fetch** (diff, `runs`, company resolution, embeddings,
enrichment queue) and is exported from the Worker like any other board. Such boards are marked
`meta.localOnly` so their daily alarm never tries to fetch — it only drains backlogs. `pull-all.sh`
does the ingest before exporting. Keep `LOCAL_ONLY` in that script in sync with `localOnlyAts`.

`jobs.parquet`: one row per job (`ats, slug, id, title, location, url, departments[], published_at,
updated_at, content, raw_json, content_hash, first/last_seen_at, changed_at, removed_at, is_open,
enrich_status, enriched_at, enrichment_json`). `boards/*.parquet`: one row per board with fetch meta and `company_*` columns.
Incremental: pass `--since=<last pull ms>`; removed jobs come through with `removed_at` set.

### Inspect / debug one board
```sh
curl -X POST $B/boards/lever/wealthfront/fetch          # force fetch
curl "$B/boards/lever/wealthfront?status=open&slim=1"    # current open jobs
curl $B/boards/lever/wealthfront/runs                    # diff history
```
Test a fetcher without the Worker: `node --experimental-strip-types scripts/try-fetcher.mjs <ats> <slug>...`

### Add a provider
1. `src/ats/<ats>.ts` exporting `<ats>: AtsFetcher` (see `greenhouse.ts` as the minimal reference).
2. Probe with `scripts/try-fetcher.mjs` against real slugs incl. a bogus one (must return `gone`).
3. Register in `src/ats/index.ts` → deploy → `POST /sync`.

### Re-enable comeet
Comeet careers pages are keyed by a server-assigned company uid (`/jobs/<name>/<uid>`, e.g. `C3.00F`)
plus a per-company API token embedded in the page; neither is derivable from the slug. The uid is
resolved from the Common Crawl URL index (DuckDuckGo fallback). Cloudflare's edge can reach those
when the laptop can't, hence `--via`:
```sh
node scripts/build-comeet.mjs --via=https://backend.dehnbostele.workers.dev   # writes src/comeet-uids.json (resumable)
# uncomment `comeet` in src/ats/index.ts, then deploy + POST /sync
```

### Turn on fleet-wide company resolution
```sh
# wrangler.jsonc vars: "BOARD_ENRICH": "on"  → deploy. Each board resolves once, after its next successful fetch.
curl -X POST $B/boards/greenhouse/stripe/enrich-board          # or per board, any time
```

### Lock down the endpoints
```sh
npx wrangler secret put ADMIN_TOKEN     # then remove the empty "vars" entry from wrangler.jsonc
```

## Operational notes
- Limits that matter: DO alarm handler ≤ 15 min wall time; paid plan 10k subrequests per invocation
  (the chunked Registry keeps far below this); 6 concurrent outbound connections per DO.
- Arming 64k boards took ~35 min with ~0.25% transient platform errors (`Network connection lost`,
  storage reset); the daily re-sweep heals them.
- A large share of crawled slugs are dead boards (`gone`): ~15–40% depending on ATS. They back off
  to weekly after 5 attempts.
- Heavy providers: workday (~100 requests for big tenants), eightfold (10/page, CloudFront blocks
  aggressive IPs), dayforce (3+ sequential calls). The per-board random slot is what keeps
  provider-level request rates low.
- Costs scale with boards × fetch requests per day; empty/`gone` boards are almost free.

## Files
```
src/index.ts         Worker: scheduled() + HTTP API
src/board.ts         Board DO (schedule, fetch, diff, enrichment queue, queries)
src/registry.ts      Registry DO (chunked arming sweep)
src/ratelimit.ts     per-IP fixed-window RateLimit DO (used by /embed)
src/budget.ts        per-IP USD meter (hour/day windows) for /enrich; src/pricing.ts has the rates
web/                 JobScream client (index.html, app.js)
scripts/build-manifest.py  tree manifest + centroids + group files for the client
scripts/upload-web.sh      publish export/web to R2
src/enrich.ts        job enrichment (structured extraction) + JD text cleanup
src/jobschema.ts     job_v1 strict JSON schema + instructions (FIELDS.md §2)
src/company.ts       board/company resolver (schema, candidate derivation, prompt)
src/openai.ts        Responses API structured-output client
FIELDS.md            enrichment field spec (board + job + embeddings)
src/ats/*.ts         one fetcher per provider; index.ts registers them
src/boards.json      generated: { ats: slug[] }
src/comeet-uids.json generated: comeet slug → {name, uid} | null
scripts/build-boards.mjs   slugs.json → boards.json
scripts/build-comeet.mjs   comeet uid resolver (--via worker)
scripts/export.mjs         laptop pull → export/<ats>.ndjson
scripts/fetch-local.mjs    run local-only fetchers on this machine → export/<ats>.ndjson
scripts/consolidate.sh     the daily workflow: ingest → pull (all ATSes, resumable) → parquet → manifest → R2
scripts/pull-pool.py       worker-pool exporter with .done markers (used by consolidate.sh)
scripts/pull-all.sh        older sequential pull (superseded by consolidate.sh)
scripts/build-parquet.py   export/*.ndjson → export/jobs/*.parquet + export/boards/*.parquet (uv run, per-ATS streaming)
scripts/try-fetcher.mjs    run a fetcher directly in Node
```

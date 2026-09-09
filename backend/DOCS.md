# open-jobs backend

A Cloudflare Worker + Durable Objects crawler that fetches every job board in `../slugs.json` (schema 2:
`ats` = boards the crawler fetches, `gone` = boards it found dead; 114k across 36 providers, rebuilt from the
fleet itself by `scripts/build-slugs.py`, so the file and the running fleet cannot drift apart) once a day, keeps a diffed history per board, and writes each board's open jobs as a parquet
snapshot to R2. A nightly consolidation (laptop today, a container next; see [CONTAINER.md](CONTAINER.md))
folds the snapshots into one dataset, publishes the search index, the daily diffs, the ledger, and
the paged feed. No central database: each board is its own Durable Object.

Production: `https://backend.dehnbostele.workers.dev` — the search page at `/`, the API and the
published data under it.

Consumers: the hosted search page (`../site/index.html`), the agent tools (`../tools/jobs.py`),
DuckDB straight against `/data/*`, and the [job changes feed](JOB-CHANGES.md) for anyone keeping a
mirror in a database.

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
- **Snapshots** (`src/snapshot.ts`): after a fetch that changed something, once the board's embed
  backlog has drained (or after 48 h regardless), the board writes its *open* jobs, vectors included,
  as one parquet object to R2 at `snapshots/<ats>/<slug>.parquet`, board meta in the footer.
  Consolidation reads these instead of calling every DO. An empty board deletes its object; boards
  over 15k open jobs are skipped (isolate memory); `POST /boards/:ats/:slug/snapshot` forces one.
- **Storage schema**:
  - `jobs(id, data JSON, content_hash, first_seen_at, last_seen_at, changed_at, removed_at, enrich_status, enriched_at, enrichment JSON, enrich_error, detail_status, detail JSON, detail_error, detail_fetched_at, embedding BLOB, embed_model, embed_status)`
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
| comeet | company name | yes | enabled — uid map `src/comeet-uids.json` (73 live of 443 legacy slugs; rebuild: `scripts/build-comeet.mjs --via=<worker>`) |
| crelate | portal name | snippet; full via `fetchDetail` (`GetJob`) | |
| dayforce | `ns` or `ns/site` | yes | bare slug ⇒ `ns/CANDIDATEPORTAL`; ~35% of bare slugs are unmappable site codes → gone |
| eightfold | subdomain | via `fetchDetail` (untested: WAF-blocked during dev) | 10/page, rate-limit sensitive |
| gohire | `name-<hash>` or `name-<id>` | via `fetchDetail` (`getJobId`) | numeric ids need a cached 1.6 MB sitemap lookup |
| join | company slug (join.com/companies/<slug>) | listing via __NEXT_DATA__; full JD via `fetchDetail` (JSON-LD) | European SMEs |
| softgarden | tenant hostname | listing /vacancies HTML; full JD via `fetchDetail` (JSON-LD) | DACH |
| teamtailor | site hostname (tenant or custom domain) | listing /jobs HTML; full JD via `fetchDetail` (JSON-LD) | Nordic/EU SMEs |
| jibe | site hostname | full JD in listing (`/api/jobs`) | iCIMS Attract sites (careers.costco.com) |
| phenom | site hostname | teaser; full via `fetchDetail` (job page `phApp.ddo`) | Phenom People sites (jobs.cvshealth.com, careers.cencora.com) |
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
| successfactors | RMK site hostname (jobs.exxonmobil.com) | full JD in the /sitemap.xml RSS feed | mined via rmkcdn CDN references |
| ukg | host:companyCode:boardGuid | brief in listing; full via `fetchDetail` (embedded JSON) | boards mined from Common Crawl |
| smartrecruiters | company id | via `fetchDetail` (`/postings/{id}`) | 0 results ⇒ probe careers page to distinguish gone/empty |
| taleo | subdomain | via `fetchDetail` (jobdetail.ftl state blob) | default career section only; `tbe` (Business Edition) unsupported |
| workable | account subdomain | yes | widget API |
| workday | hostname | via `fetchDetail` | sites discovered from robots.txt; 20/page; full JD from `/wday/cxs/.../job/...` once per job |
| dark | career-site hostname | yes (schema.org JobPosting JSON-LD in the HTML) | "dark pool": bespoke career sites with no shared ATS, mined from Web Data Commons / Common Crawl; the aggregator guard drops boards naming >2 hiring organizations |
| bamboohr | company subdomain | via `fetchDetail` | slugs mined from Common Crawl (*.bamboohr.com) |
| jazzhr | company subdomain (applytojob.com) | via `fetchDetail` | slugs mined from Common Crawl |
| cornerstone | corp subdomain (csod.com) | yes (public search API) | SPA, but the search API returns the full posting |
| usajobs | organization code (e.g. `AF00`) | yes | data.usajobs.gov API, `USAJOBS_KEY` secret; 10k cap per query |
| governmentjobs | NEOGOV search slice | yes | **local-only** (IP-pinned pagination): state and local government, ~22k jobs |
| snowflake | (abandoned experiment, 2026-08-31: custom extractors for big companies' own sites; registered, see `src/snowflake/index.ts`) | | |

Registered providers: 36 (`src/boards.json`, 114k slugs); `localOnlyAts` (`jobscore`, `governmentjobs`)
are fetched from the laptop and ingested, the other 34 by the fleet.

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
1M tokens at ~1.2k tokens/job → ~$75 for the current 3.1M jobs. **What is embedded**: labelled
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

## What is published, and who reads it

The hosted search page (`../site/index.html`, one self-contained file uploaded as a Worker static
asset and served at `/`) and the agent tools (`../tools/jobs.py`, see `../AGENTS.md`) both work the
same way: embed the person's ideal job once (`POST /jd` writes it, `POST /embed` embeds it), walk the
tree manifest to the nearest groups, download those group files, and do everything else locally.
No server sees labels, notes, or a résumé.

Everything lives in the `jobscream-data` R2 bucket, served at `GET /data/*` (Range, CORS, one hour
of cache), rebuilt nightly:
- `manifest.json` — recursive-bisection tree over all embedded open jobs: nodes with `lo/hi` row
  range (DFS order), `radius`, `size`, `label`, `medoid`, `exemplars`, `children`; plus `recipe`,
  `jobs`, `leaves`, `built_at`.
- `centroids.bin` — float16 `[nodes × dims]` unit centroids, same order as `manifest.tree`; a client
  walks the tree with byte-range reads (each subtree is contiguous in DFS order) instead of
  downloading the 69 MB file.
- `groups/<leaf>.json` — jobs of one leaf (ats, slug, id, title, company, location, url, seen, pub,
  jd text ≤ 4k chars, enrichment and company when known) with exact float32 embeddings (`v`, base64
  little-endian). Leaf ids restart at 0 every build; old files above the current count linger,
  unreferenced and harmless.
- `age-model.json`, `salary-model.json`, `arrangement-model.json`, `seniority-model.json`,
  `location-countries.json` — the estimators (`FIELDS.md`, `scripts/train-*.py`), applied client-side.
- `diffs/`, `ledger/`, `changes/` — history: see "Layout and retention" below and JOB-CHANGES.md.
Build + publish: the `tree`, `estimators`, `finalize`, `history`, and `feed` stages of the daily
consolidation. Experiment / evaluation of the tree: `scripts/experiments/tree.py`.

## HTTP API

Admin endpoints require `Authorization: Bearer <ADMIN_TOKEN>` (a Worker secret). With no secret set they
fail closed (401 for everyone); public endpoints (`/status`, `/probe`, `/embed`, `/jd`, `/enrich`, `/ideas`, `/data/*`)
never need it, and every response, 401s and 404s included, carries CORS headers. Locally the token lives in
`backend/admin_token.txt` (git-ignored); `consolidate.sh` / `stage.py` export it as `ADMIN_TOKEN`. Cloudflare's
bot rules 403 the default Python `urllib` user agent — send any custom UA (curl is fine; the scripts use
`open-jobs-tools/0.1`).

| Method | Path | Purpose |
|---|---|---|
| POST | `/status` | **public**, 60 / 10 min per IP: body `{keys:["ats/slug#id",…]}` (≤ 1000 keys, ≤ 150 boards) → `{statuses:{key:{status:"open"\|"removed"\|"unknown", firstSeenAt, lastSeenAt, removedAt}}}`, straight from the Board DOs (the source of truth for `removed_at`) |
| POST | `/enrich` | **public**, per-IP metered (`ENRICH_HOUR_USD` 5 / `ENRICH_DAY_USD` 50, actual token + web-search cost; cached results free): body `{jobs:[{ats,slug,id}…]}` (≤ 300) → `{boards:{name:{company,…}}, jobs:{key:{status,enrichment,cached}}, cost:{thisCallUsd,hourUsd,dayUsd,…}}`; 429 + `retry-after` when a window is exhausted (cached part still returned). Runs job extraction + company resolution, both one-shot |
| GET | `/enrich/budget` | this IP's hour/day spend and limits |
| GET | `/probe?url=<job url>[&board=ats/slug]` | **public**: "why isn't this posting in the corpus?" — resolves the board from the URL (`src/probe.ts`; slugs.json-matched for icims/gohire; workable/paylocity/embedded `gh_jid` need `board=`), returns `{resolved, crawled, board:{lastOkAt,jobCount,nextFetchAt,…}, job:{found,status,firstSeenAt,embedStatus,embedding,…}}`. 60/10 min per IP. `tools/jobs.py probe` adds the group membership (from downloaded leaf files) and slice rank |
| POST | `/ideas` | **public**: `{file, line, idea, tags[]}` (or `{text}`; anonymous, no identity collected) relayed to the #multipenny-ideas Slack channel as a Block Kit message with a GitHub-linked file:line header (`SLACK_IDEAS_WEBHOOK` secret); 30/hour per IP |
| POST | `/embed` | **public**, IP rate-limited (`EMBED_RATE_LIMIT` per `EMBED_RATE_WINDOW_MS`, default 10 / 10 min): body `{text, title?, location?}` → `{vector[1536], recipe}`; 429 with `retry-after` when limited, 503 when the embeddings API is saturated |
| POST | `/jd` | **public**, 20 / 10 min per IP and metered against the same per-IP USD windows as `/enrich`: body `{title, location, blurb, model?: "luna"\|"astra"}` → `{jd, sections, model, usage, costUsd, budget}`. Expands a person's short description of the job they want into the *ideal* JD in the shape of a real posting (`src/jd.ts`, structured output, no tools), ready for `/embed`. `luna` (gpt-5.6-luna, default, ~$0.003/call) or `astra` (gpt-6-astra, ~$0.1/call) |
| GET | `/data/` | **public**: HTML index of the public files (ledger days, diffs, snapshot, feed head) rendered from the indexes below, `max-age=3600` |
| GET | `/data/exports/[<date>/[jobs/\|boards/]]` | **public**: JSON listing of that prefix (`dirs`, `files[{file,bytes}]`); the full export has no index file of its own |
| GET | `/data/<key>` | **public**: any object in the `jobscream-data` R2 bucket (manifest, centroids, group files, estimator models, `diffs/`, `ledger/`, `changes/`), Range + CORS, `max-age=3600` |
| GET | `/ats[?all=1]` | providers fetched by the Worker fleet → slug counts; `all=1` includes local-only ones |
| POST | `/sync` | start a Registry sweep for every enabled ATS (what the cron does) |
| GET | `/sync/:ats` | sweep status: `mode`, `cursor/total`, `touched`, `fetched`, `skipped`, `errors`, `lastError`, `finishedAt` |
| GET | `/snapshots?ats=<ats>[&cursor=…]` | list the per-board snapshot objects of one ATS (key, size, etag, uploaded); what `pull-snapshots.mjs` walks |
| POST | `/backfill[?ats=a,b]` | kick every board with a detail/embed/enrich backlog so it drains now (per-board minute ticks); progress via `/sync/:ats` (`fetched` = kicked) |
| POST | `/fetch-all[?ats=a,b][&skipRecent=<ms>]` | on-demand fetch of every board (arms if needed) via the Registry sweep in `fetch` mode. Boards that are *fresh* — completed a non-error fetch within `skipRecent` (default 6 h; `0` forces) — are skipped. Does not change daily slots. Progress via `/sync/:ats` |
| GET | `/boards/:ats/:slug[?filters]` | `{meta, jobs}` for one board |
| GET | `/boards/:ats/:slug/runs` | last 30 fetch runs with diff counts |
| POST | `/boards/:ats/:slug/fetch` | arm (if needed) and fetch now; returns meta |
| POST | `/boards/:ats/:slug/retry-enrichment` | reset `error` enrichments to `pending` |
| POST | `/jobs/enrich` | body `{"jobs":[{"ats","slug","id"}…],"force"?:bool}` → `{boards: {"ats/slug": {company, companyError}}, jobs: {"ats/slug#id": {status, enrichment, cached}}}`; idempotent lazy enrichment; resolves the board's company first (cached) |
| POST | `/boards/:ats/:slug/embed` | embed all un-embedded jobs on the board now |
| POST | `/boards/:ats/:slug/snapshot` | write the board's R2 parquet snapshot now |
| POST | `/boards/:ats/:slug/wipe` | drop every row (recovery for a pathological board); fetch again after |
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
npm run build:boards        # regenerate src/boards.json from ../slugs.json (live + gone; --live-only for a fresh deploy)
uv run scripts/build-slugs.py   # the other direction: rebuild ../slugs.json from the fleet's own board metadata (after a consolidation)
npx wrangler types          # after changing bindings/vars
npx tsc --noEmit
npx wrangler deploy
```
`../site/index.html` (the search page) is uploaded as a static asset on every deploy and served at `/`;
every API path is listed under `run_worker_first` in `wrangler.jsonc` so the assets layer never shadows an endpoint.
Migrations: `v1` created the scaffold's `MyDurableObject`; `v2` deleted it and created `Board` + `Registry`;
`v3` added `RateLimit`, `v4` `Budget`.
Adding a DO class = new migration tag. Deploying mid-sweep is safe: Registry/Board state is in storage.

### Self-host the search page

`../site/index.html` is one self-contained file with no build step. It decides where to talk to at load time:
served over HTTP it uses its own origin for both the API and the data; opened from disk it uses production.
Two query parameters override that, and they are the whole self-hosting story:

- `?api=<origin>`: where `POST /jd` and `POST /embed` go. Those are the page's only API calls; freshness is
  computed in the browser from each job's first-seen date in the group file and `age-model.json`.
- `?data=<origin>`: where `/data/*` comes from: `manifest.json`, `centroids.bin`, `groups/<id>.json`,
  `age-model.json`. Defaults to the API origin.

Copying the file onto another host **without** `?api=` breaks it: every request goes to that host and 404s.
Three ways to host it, from least to most independent:

1. **Front door only.** Serve the file and link to it as `/?api=https://backend.dehnbostele.workers.dev`
   (or edit the `API` constant near the top of the script). Search, freshness, and data all still come from
   this Worker and its bucket. Nothing to run; reads are free on this side.
2. **Own data, shared brain.** Mirror the bucket (`tools/jobs.py export` nightly, or replay `diffs/` or the
   [change feed](JOB-CHANGES.md)), serve your copy of `manifest.json`, `centroids.bin`, `groups/`, and
   `age-model.json` under some `/data/` with CORS and HTTP Range enabled (the tree walk is byte-range reads
   into `centroids.bin`), and load the page with `?data=<your origin>&api=https://backend.dehnbostele.workers.dev`.
   The data files are rewritten nightly under the same names, so the mirror must refresh `manifest.json`,
   `centroids.bin`, and `groups/` together, from the same build (`manifest.built_at` names it).
3. **Fully independent.** Also run the two endpoints that call OpenAI. `/embed` turns the typed JD into a
   query vector and `/jd` expands a short description into an ideal JD; both spend on the key of whoever
   hosts them, rate-limited and metered per visitor IP (see the API table). A hosted board with real
   traffic is many IPs, each with its own allowance, so a third party's board should point `?api=` at
   their own deployment of this Worker (`npx wrangler deploy` with their `OPENAI_KEY`; the fleet bindings
   can stay empty for that use) or at a small proxy of their own that holds their key.

Cost exposure, in one line: `/data/*` costs the origin nothing per read (R2 has no egress fee); only `/jd`
and `/embed` spend money, on whoever's key the `api` origin holds.

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

One command: `scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload] [--skip-models] [--skip-ledger] [--keep-full] [--source r2] [--from STAGE]`.
It is a thin wrapper: the work is twelve stages in `scripts/stage.py`, each one an idempotent command
(`uv run scripts/stage.py <stage> --date <date>`), and the wrapper only sequences them and keeps the log
(`logs/consolidate-<date>.log`). `--from <stage>` resumes after a failure.

| stage | does | reads | writes |
|---|---|---|---|
| ingest | local-only ATSes fetched from this machine, posted to the Worker | the sites | Worker |
| pull | per-board snapshots -> `export/<date>/snapshots/` (skipped with `--source r2`) | R2 | local |
| ledger | slim `status=all` export of every board -> `export/ledger/<date>/` | Worker | local |
| parquet | snapshots (+ `/export` fallback) -> `jobs/<ats>.parquet`, `boards/<ats>.parquet` | local or R2 in place | local, and `exports/<date>/` in R2 with `--publish`/`--source r2` |
| diff | today vs the previous export -> `export/diffs/<prev>__<date>/` | local or R2 | local |
| tree | manifest, centroids, group files; `--publish` streams each group file to `groups/` as written | local or R2 | `web/`, R2 |
| estimators | salary, arrangement, seniority, age, city + location tables | local or R2 | `web/` |
| finalize | `scripts/publish-web.py`: reconcile `groups/` in R2 by size, then models, centroids, manifest last; repoint `export/latest` | `web/` | R2 |
| history | `scripts/upload-history.py`: diff and ledger parts + `index.json` | local | R2 |
| feed | `scripts/build-job-changes.py` ([JOB-CHANGES.md](JOB-CHANGES.md)): the paged consumer feed. First run bootstraps from today's export against the index just published; after that, one generation per diff. Receipt in `export/feed/published.json` | local, R2 index | R2 `changes/` |
| retention | delete older full exports that have a successor diff; in r2 mode, `exports/<date>/` prefixes older than the previous one | | local or R2 |
| report | one line per run (jobs, diff counts, feed generation, stages passed) to `SLACK_RUN_WEBHOOK`, else the ideas relay; reads `run.jsonl` | scratch | Slack |

All uploads go through the S3 API (`scripts/r2.py`: boto3, multipart, retries; credentials `R2_*` in the
environment or `.dev.vars`), so there is no 300 MiB per-object cap and no `wrangler` in the pipeline.
The container: `Dockerfile` + `scripts/container-run.sh` (see [CONTAINER.md](CONTAINER.md)). `--source r2` is the container layout: snapshots and the previous export are read from the bucket in
place through DuckDB's S3 client, parquet is written to `exports/<date>/`, and only scratch
(`work-<date>/`: vector memmap, staging, DuckDB spill) is local. Every script that reads an export takes
`EXPORT_DIR` as a local dir or an `s3://bucket/exports/<date>` prefix, with `WORK_DIR` for local scratch.

[`CONTAINER.md`](CONTAINER.md) has the container: how to build and run it locally against the bucket, what state
lives in R2 between runs, the measured stage sizes, and what remains for the Cloudflare side (image push,
Workflow on the cron, a week in parallel).

### Layout and retention: one full export, plus history as diffs
```
export/latest -> 2026-09-07/         the one full export (snapshots, jobs/, boards/, web/), ~80 GB
export/diffs/2026-09-06__2026-09-07/data_*.parquet   what changed between two consecutive full exports (lossless; parts <= 200 MB)
export/diffs/2026-09-06__2026-09-07/lite/data_*.parquet   the same rows without the vector or raw JSON; text kept on added/changed
export/diffs/2026-09-06__2026-09-07.json      counts, vanished boards and their verdicts, ok_to_prune
export/ledger/2026-09-07/data_*.parquet   every job the crawler has ever recorded, open or removed, with dates
```
- **Diffs** (`scripts/build-diff.py`, the `diff` stage): one row per event with the full job record and `op` =
  `added` | `removed` (the old row, in full) | `changed` / `changed_prev` (title, location, url, or text
  or `embed_status` moved; the crawler's `content_hash` is *not* the criterion, it churns for ~370k Workday rows a day) |
  `carried`. `latest` + the diffs reconstructs any day. 1.4–3.1% added and removed per day so far; 0.4–1 GB full, 36–96 MB lite.
  `lite/` under each diff holds the same rows minus `embedding`, `raw_json`, `detail_raw_json`, `enrichment_json`,
  with `content` kept on `added` and `changed` rows and null elsewhere: everything a mirror needs to show a job,
  at a fraction of the size. Two tiers, on purpose: **lite** to filter and display, **full** to search (the vector
  on every new job) or to reconstruct history. Vectors on added/changed rows alone would bring lite to ~330 MB
  against ~400 MB for full, so there is no middle tier. Apply every `added` and
  `changed` row; `embed_status` says whether the job is in the public group files yet (`done`), and the flip to
  `done` is itself emitted as a `changed` row, so a mirror that only wants the public corpus can gate on it and
  still catch jobs that were added before they were embedded.
- **What a removal means.** Every `removed` row carries `removal`: `closed` (the crawler marked the job
  removed; `removed_at_crawler` says when, from the same run's ledger), `left_dataset` (the crawler still
  holds it open, so it left the export for another reason: an eligibility or filter rule, or dedup picking a
  different mirror), or `unknown` (never in the ledger). The sidecar counts them. Closure is a crawler
  guarantee only for `closed`; `POST /status` answers for any key at any time.
- **What counts as a change.** `title`, `location`, `url`, `content`, `embed_status`, `published_at`. Not
  tracked: `departments`, `updated_at`, enrichment, and company metadata, which lives in `boards/` and is
  not part of the diff (the sidecar's `change_key` lists the tracked fields for the schema version).
- **Vanished boards.** A board with rows yesterday and none today is asked about: `GET /boards/:ats/:slug`
  says whether the crawler still holds open jobs for it. If so, our pull missed it and its rows are
  `carried` (appended into today's `jobs/` and `boards/` parquet so the index and tomorrow's diff keep
  them); if the crawler says 0, it really emptied and the rows are `removed`. Without this, one bad pull
  reads as 50,000 postings closing and reopening. More than `--max-absent` (3000) vanished boards means
  the pull is broken: everything is carried and nothing is deleted.
- **Ledger** (`scripts/build-ledger.py`, the `ledger` stage): a slim `status=all` export of every board (no text,
  no vectors; minutes, not hours), so removed jobs and their `removed_at` come straight from the Board
  DOs. This, not the diffs, is the source for posting lifetimes and survival curves.
- **Integrity.** Every part carries a sha256 (in the sidecar and in `index.json`); a diff's `content_sha256`
  is the hash of its full parts' hashes, and `parent` names the previous diff and its `content_sha256`, so the
  chain from any bootstrap day forward breaks loudly if a file goes missing or is truncated. `schema_version`
  is in every sidecar and index. Published diffs and ledger days are never rewritten (a re-run of a diff that
  was not final is fine, since it was never listed).
- **Published** (`scripts/upload-history.py`, the `history` stage): both go to R2 under `diffs/` and `ledger/`, public at
  `GET /data/diffs/<prev>__<date>/data_N.parquet` (or `.../lite/data_N.parquet`) and `GET /data/ledger/<date>/data_N.parquet`; `GET /data/diffs/index.json`
  and `/data/ledger/index.json` list what is available with the parts and the sidecar counts (the bucket listing
  itself is admin-only). DuckDB reads them in place: `read_parquet(['https://backend.dehnbostele.workers.dev/data/diffs/<a>__<b>/data_0.parquet', ...])`.
- **Bootstrap and recovery (for a mirror).** `diffs/index.json` carries `head` (the export date behind the
  current `groups/` and `manifest.json`) and `snapshot_built_at` (that manifest's `built_at`). Bootstrap from
  `groups/` when `manifest.built_at` equals `snapshot_built_at`, record `head`, then apply every diff whose
  `from` equals your head, in order, verifying each part's sha256 and each diff's `parent`. Diffs are never
  expired, so there is no cursor expiry; a hash mismatch or a missing diff is a broken chain, and the answer
  is to re-bootstrap, never to skip.
- **Retention policy, as stated in the public indexes:** diffs and ledger days are kept indefinitely; the full
  export is overwritten daily; the group files are rewritten daily under the same names. A mirror bootstraps
  from `groups/` once and replays diffs from that day. Not yet done: a periodic full anchor (a monthly published
  `jobs/` parquet), which would bound replay for someone reconstructing an arbitrary past day.
- **Retention** (the `retention` stage): once today's diff exists and passes its sanity check (`ok_to_prune`: job
  count within 10% and removals under 15%), every older full export that has a successor diff is
  deleted, listed first. `--keep-full` keeps them; a failed or skipped diff keeps them too.

### What the stages taught us (the parts that still apply)

- **Ingest** (`fetch-local.mjs --ingest`): jobscore and governmentjobs block or IP-pin Cloudflare, so
  the laptop fetches them and POSTs each snapshot to `/boards/:ats/:slug/ingest`; the Board DO then runs
  the normal diff/embed/enrich pipeline and writes its R2 snapshot like any other board. It only needs
  the Worker, so it runs on its own schedule; the container's run starts at `pull`. ~15 min.
- **Pull** is 62k R2 snapshot objects (`pull-snapshots.mjs`, 24 concurrent, etag skip; ~35 GB, ~17 min),
  or nothing at all under `--source r2`, where `build-parquet.py` reads them from the bucket in place
  (~46 files/s from a laptop, faster inside Cloudflare). The `/export` JSON path survives for the two
  local-only providers and for the ledger, and everything learned about it still holds: it is
  **pull-driven** with one-board lookahead so the client's read pace bounds Worker memory; every query
  is paged (2,000 jobs, 150 with vectors) because a 30k-job history in one response blew the DO's
  memory; `export.mjs` verifies each page's board count and retries; `--resume` never loads a file into
  one string. Don't run two pools on the same output dir.
- **Parquet** (`build-parquet.py`): from snapshots, DuckDB reads the columnar files and the footer meta
  directly (~6 min for 3.1M jobs); from ndjson, a Python pre-pass splits per-board lines into per-job
  JSONL first (a board line can be 100+ MB). Explicit `columns=` schema, bounded memory, 20k-row groups.
  Dedups by (ats, slug, id) and drops dark boards naming more than two hiring organizations.
- **Tree** (`build-manifest.py`): see the next section. Streams every group file to R2 as it is written.
- **Finalize** (`publish-web.py`): reconcile `groups/` in R2 by size (a failed background upload never
  reaches the manifest), then the estimator JSONs, `centroids.bin`, and `manifest.json` last, so a
  client never sees a manifest whose groups aren't there yet. Group ids change every build, so a stale
  cache only ever misses, never mismatches. Old group files above the current leaf count accumulate;
  leaving them is harmless and deliberate.
- Never `rm` an export you haven't listed first. Retention lists, then deletes, in separate steps.

### The manifest build (`scripts/build-manifest.py`)
Two passes over `jobs/*.parquet`. Pass 1 loads every embedded open job's vector into a float16 memmap
(`.vectors.f16.sorted.npy`, deleted at the end) and keeps per row only what labels and exemplars need.
Rows are then **key-sorted** (`ats/slug#id`), so the same export always yields the same tree: the tree's
random choices are seeded but index into rows, and parquet scan order varies with column selection and
threads. The tree is built as before (bisection in PCA-256 space, centroids and radii in the full space).
Pass 2 joins the parquet to each row's DFS position in one scan, writes the joined rows to a local staging
dir partitioned into 250k-position chunks, then sorts and streams one chunk at a time, writing each group
file when its last row arrives. No description text is ever held for the whole corpus, and no sort is
larger than a chunk (a single `ORDER BY` over the corpus exhausted DuckDB's buffer). Verified byte-identical
to the single-pass build on 2026-09-07, twice. `--out` writes elsewhere than `<EXPORT_DIR>/web` (validation
builds); temp files carry the pid, so two builds on one export do not collide. Peak real memory 9.7 GB
(1.4 GB loading, 4.5 GB through the tree, ~9.6 GB briefly in pass 2 with DuckDB capped at 6 GB), ~20 min
at 3.1M jobs. `BUILD_MANIFEST_STOP_AFTER=load` exits after loading (memory probes).

### Before you run it
- The fleet should be quiet: `POST /backfill` and wait for a sweep with `pendingDetails`/
  `pendingEmbeds` ≈ 0 (`GET /sync/:ats`), otherwise the pull captures half-embedded boards.
  The daily cron handles steady state; a big recipe change (new `EMBED_TAG`) needs a full
  backfill first (~5 h at the 10M TPM embeddings cap).
- Disk: ~80 GB for the one full export, plus ~0.5 GB per day of diffs and ~0.3 GB per day of
  ledger. While a run is in progress two full exports exist (~160 GB); step 6 drops the older one.
- Time, measured 2026-09-08 at 3.1M jobs: ingest 15 min, pull 19, ledger 14, parquet 6, diff 1, tree 19
  (group files stream up during it), estimators 59, finalize seconds when the tree published (26 min
  when it did not), history 2, feed a few minutes per day (the one-time bootstrap was 45), retention
  seconds. About 2.5 h end to end.

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
- Any stage: fix, then `scripts/consolidate.sh --from <stage>` (or `uv run scripts/stage.py <stage> --date <date>`;
  in the container, `scripts/container-run.sh <stage> --date <date>`). Stages are idempotent and skip work
  already done (etag-skipped snapshots, `.done` markers, per-file upload markers, the reconcile).
- Tree OOM: it peaks near 10 GB of real memory; close other DuckDB/numpy processes first.
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
Providers in `localOnlyAts` (`src/ats/index.ts`; jobscore, which 403s Cloudflare IPs, and governmentjobs, whose pagination is IP-pinned) are
not fetched by the Worker fleet. `scripts/fetch-local.mjs --ingest=<worker-url>` runs their
fetchers on this machine and POSTs each snapshot to `/boards/:ats/:slug/ingest`; the Board DO then
runs the **same pipeline as an online fetch** (diff, `runs`, company resolution, embeddings,
enrichment queue) and is exported from the Worker like any other board. Such boards are marked
`meta.localOnly` so their daily alarm never tries to fetch — it only drains backlogs. `pull-all.sh`
does the ingest before exporting. Keep `LOCAL_ONLY` in that script in sync with `localOnlyAts`.

`content_hash` covers title, location, url, departments, dates, and description text, not the raw provider
payload (changed 2026-09-10; values now start with `s`). Do not diff on it across that date; the diff stage and the
change feed use their own change key and are unaffected.

`last_seen_at` of an open job is derived at read time as max(stored value, the board's last successful fetch):
a daily fetch does not rewrite unchanged rows (that was ~40M DO row writes a day, the largest Cloudflare
line item in September 2026); only added, changed, removed, and re-listed rows are written.

`jobs/<ats>.parquet`: one row per job (`ats, slug, id, title, location, url, departments[], published_at,
updated_at, content, raw_json, detail_raw_json, detail_status, content_hash, first/last_seen_at, changed_at,
removed_at, is_open, enrich_status, enriched_at, enrichment_json, embed_status, embed_model, embedding FLOAT[]`;
from snapshots the raw JSON columns are NULL and only open jobs are present). `boards/<ats>.parquet`: one row
per board with fetch meta and `company_*` columns.
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
npx wrangler secret put ADMIN_TOKEN     # required; there must be no ADMIN_TOKEN var in wrangler.jsonc (it would shadow the secret)
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
src/index.ts         Worker: scheduled() + HTTP API; serves ../site as static assets at /
src/board.ts         Board DO (schedule, fetch, diff, detail, embed, enrichment queue, queries, snapshot trigger)
src/snapshot.ts      per-board R2 parquet snapshot (hyparquet-writer; open jobs + vectors, meta in the footer)
src/registry.ts      Registry DO (chunked arming sweep)
src/ratelimit.ts     per-IP fixed-window RateLimit DO (/status, /probe, /embed, /jd, /ideas)
src/budget.ts        per-IP USD meter (hour/day windows) for /enrich and /jd; src/pricing.ts has the rates
src/jd.ts            POST /jd: the ideal-JD writer (structured output on luna, astra opt-in)
src/probe.ts         GET /probe: job URL -> ats/slug/id
src/enrich.ts        job enrichment (structured extraction) + JD text cleanup
src/jobschema.ts     job_v1 strict JSON schema + instructions (FIELDS.md §2)
src/company.ts       board/company resolver (schema, candidate derivation, prompt)
src/openai.ts        Responses API structured-output client + embeddings
FIELDS.md            enrichment field spec (board + job + embeddings)
JOB-CHANGES.md       the paged consumer feed (publisher, consumer protocol, recovery)
CONTAINER.md         the consolidation container: run it, what lives in R2, what remains
src/ats/*.ts         one fetcher per provider; index.ts registers them (localOnlyAts, enabledAts)
src/boards.json      generated: { ats: slug[] }
src/comeet-uids.json generated: comeet slug → {name, uid} | null
../site/index.html   the hosted search page (served at /)
Dockerfile           the consolidation image (build from the repo root)
scripts/consolidate.sh     laptop wrapper: stage.py stages in order; --from resumes; --source r2
scripts/container-run.sh   the same stages in the image: build | all | <stage> | shell
scripts/stage.py           one consolidation stage per invocation (ingest pull ledger parquet diff tree estimators finalize history feed retention report)
scripts/r2.py              R2 through the S3 API (boto3, multipart) + DuckDB S3 config
scripts/pull-snapshots.mjs per-board R2 snapshots -> export/<date>/snapshots/ (etag skip)
scripts/export.mjs         /export JSON pull -> <ats>.ndjson (local-only providers, ledger); paged, resumable
scripts/pull-pool.py       worker pool over export.mjs with .done markers
scripts/fetch-local.mjs    run local-only fetchers on this machine and --ingest them
scripts/build-parquet.py   snapshots or ndjson -> jobs/<ats>.parquet + boards/<ats>.parquet (SNAPSHOT_SOURCE=r2, --publish)
scripts/build-ledger.py    slim status=all export -> export/ledger/<date>/ (every job ever, with dates)
scripts/build-diff.py      today vs previous export -> export/diffs/<prev>__<date>/ (+ lite, sidecar, carry-forward)
scripts/build-manifest.py  tree manifest + centroids + group files (two-pass, key-sorted, --publish)
scripts/train-*.py, build-city-table.py, build-location-table.py   the estimators (web/*.json; location cache in state/)
scripts/publish-web.py     finalize: reconcile groups/, then models, centroids, manifest
scripts/upload-history.py  diffs + ledger + index.json (--remote-index rebuilds from the bucket)
scripts/build-job-changes.py, test_job_changes.py   the feed publisher and its tests (PR #7)
scripts/build-boards.mjs   slugs.json -> boards.json (live + gone by default; --live-only)
scripts/build-slugs.py     the fleet -> slugs.json: every board the Worker runs, split into `ats` (fetchable) and `gone` (dead), from the ledger export
scripts/build-comeet.mjs   comeet uid resolver (--via worker)
scripts/discover-careers.py  career-site discovery for the dark pool
scripts/try-fetcher.mjs    run a fetcher directly in Node
scripts/pull-all.sh        the old sequential pull (superseded by consolidate.sh)
```

# The custom-site crawler (the "dark pool")

Most companies don't use a shared ATS — they run a bespoke career page. Those are invisible to the
ATS fetchers (which key off a known provider API). This document designs a narrow, Google-style
crawler that covers them, scoped to the domains we *know* publish jobs, and running on a dedicated
box that ingests into the existing pipeline.

## Why a crawler (and not another fetcher)

Measured, not assumed:
- Static `fetch()` of a random `careers.*` host finds a job **~30%** of the time; the other ~50% are
  JS single-page apps that load postings from a backend API after render, so nothing is in the HTML.
- Probing 20 known job-schema employer domains at their **apex** sitemap found **0** job pages — the
  apex sitemap is the marketing sitemap; jobs live on a `careers.`/`jobs.` subdomain behind SPA routing.
- So: no single API to call, and static extraction alone leaves most of the pool on the floor. What
  reaches it is what Google runs — render the page, read the posting — but scoped to a finite seed
  list instead of the whole web.

## The seed list: Web Data Commons

Google for Jobs made `schema.org/JobPosting` JSON-LD near-mandatory, and **Web Data Commons** already
extracts it from Common Crawl. The 2024-12 release: **4.29M JobPosting entities across 63,320 domains**,
downloadable as class-specific N-Quads with each posting's **source URL** as the quad provenance.

That gives us, for free:
1. **The domain seed list** — 63,320 domains that publish jobs.
2. **Per-domain job-URL patterns** — e.g. `www.k3-team.com/jobs/<slug>` — so the crawler knows where a
   site's jobs live without blind discovery.
3. **A cold-start backfill** — 4.3M historical postings (stale, ~quarterly; use for URL patterns and
   breadth, not as live listings).

Filtering the seed list:
- **Drop ATS-fronted domains** (canonical host resolves to greenhouse.io, myworkdayjobs.com, phenom,
  teamtailor, … — anything in our 30 ATSes). Those are already covered; crawling them double-ingests.
- **Drop aggregators / job boards** (rabota.ru, mynavi.jp, hipo.ro, jobboardsearch.com, …). These
  re-list other companies' jobs; ingesting them pollutes the corpus with duplicates and dead links.
  Maintain an explicit denylist + a heuristic (a domain whose postings' `hiringOrganization` spans
  hundreds of distinct companies is a board, not an employer).
- Result: **~40–45k genuine employer career domains**, refreshed each WDC release, topped up between
  releases by our own JobPosting extraction over the newest monthly Common Crawl (same technique).

## Per-domain crawl (tiered, cheap-first)

For each domain, one worker produces a snapshot (list of `Job`), tiered so the browser is a last resort:

1. **Entry point** — try, in order: the WDC source-URL host; `careers.<domain>`; `jobs.<domain>`;
   `<domain>/careers`. Take the first that returns a job-listing-shaped page.
2. **Enumerate current job URLs** (never trust the stale WDC URLs — jobs churn):
   - `sitemap.xml` / `robots.txt` sitemaps, filtered to job URLs (`/job`, `/career`, `/vacancy`, …).
   - else render the listing page, harvest job links, follow pagination.
3. **Extract per job**, cheapest path that works:
   - **Static fetch → JobPosting JSON-LD** (fast, no browser). Covers server-rendered sites.
   - **Headless render → JSON-LD** (SPA sites that inject markup after hydration).
   - **Headless render → DOM heuristics** (last resort: title/location/description from the rendered
     DOM when there's no JSON-LD at all).
4. **Normalize** to the existing `Job` shape (`id` = stable posting id or URL hash; content = the JD).

The tiering matters at scale: most large employers *do* serve JSON-LD statically on the job page once
you have the right URL — the browser is only spent on the SPA fraction.

## Architecture: one DO per dark-pool company (primary), a box for the residual

The elegant fit — and the primary design — is **no separate box for the static-serving majority**.
Common Crawl (which WDC extracts from) does **not execute JavaScript**, so every one of the 4.3M
JobPostings WDC found was in *static HTML*. A site that only injects JSON-LD via client-side JS never
enters WDC at all. Therefore the WDC seed is, by construction, the set a plain `fetch()` can read —
which is exactly what a Durable Object can do. So each dark-pool company is just another board:

- New pseudo-ATS **`dark`**, `slug` = the company's careers host (e.g. `careers.acme.com`). One DO per
  company, self-scheduling its daily fetch like every other board.
- `fetchJobs(slug)` = the crawler's discovery pass: fetch the site's `sitemap.xml` (or careers page),
  return the current job URLs as the listing (title from the sitemap/URL).
- `fetchDetail(job)` = fetch that job page and extract the `JobPosting` JSON-LD (title, location,
  description, datePosted, hiringOrganization). Identical to teamtailor/softgarden/join/successfactors,
  and the existing batched detail stage drains large sites across alarm ticks.
- Everything downstream — diff, embeddings, enrichment, export, manifest — is reused unchanged.

**Validate before spawning.** A seed-builder probes each filtered WDC domain with a static extraction
test and creates a `dark` board only for the ones that actually yield JSON-LD in-DO. Live measurement
on 500 filtered WDC employer domains: **26%** yield JobPosting JSON-LD in-DO (after a careers-page
link-harvest fallback lifted it from 21%), median ~32 job URLs/board. The 500-domain `no-jobs` bucket
sub-classifies as: ~64% SPA/shell (render tier), ~22% unreachable/bot-walled, ~13% recoverable-by-
discovery (now folded into the 26%), ~1% genuinely no open roles. So the `dark` DO model covers
**~26% of the seed (~10k companies) with zero new infrastructure**; ~33% of the whole seed is the
render residual; the rest is unreachable/dead. **Apply the aggregator guard to the validated set** —
job boards (jobbasel.ch, mnschooljobs.org, …) leak the domain filter and inflate the job count.

**The box is the escalation, not the start.** Sites that went SPA-only since the WDC crawl, or that
bot-wall Cloudflare egress (403, like RTX), can't be read in-DO. Those are handled by an off-Cloudflare
renderer that ingests via the `localOnly` path — the same pattern as today's RTX board, scaled. Build
it only once the DO model's residual justifies it.

## The render residual — Cloudflare Browser Rendering, not a box

The SPA fraction (~64% of `no-jobs` ≈ ~a third of the whole seed) needs a real browser. Cloudflare's
**Browser Rendering** is a Worker binding, so this stays on-platform — no external box. A render-Worker
(triggered per `needs-render` board; a DO can't hold a browser itself) loads the page, extracts the
JobPosting JSON-LD / DOM, and the board ingests through the normal pipeline.

Pricing (Workers Paid, 2026): 10 browser-hours/mo + 10 concurrent browsers included, then **$0.09 /
browser-hour** and **$2.00 / additional concurrent browser** (metered on wall-clock browser-hours and
peak concurrency). Cost = pages rendered × seconds each × $0.09/hr, so the entire lever is rendering
*fewer* pages:
- **Render only to discover.** Render the SPA *listing* to get job URLs, then **static-fetch** the
  detail pages — many SPAs are SPA on the list but server-render the detail with JSON-LD (free path).
- **Content-hash cache**: daily passes render only new/changed pages; the expensive part is the first pass.

Order-of-magnitude: a naive full daily render (~13k domains × ~50 pages × ~3s) ≈ 500+ browser-hours/day
≈ **~$1.5k/mo** + concurrency overage; with discover-only + delta caching, steady-state is plausibly
**low-hundreds/mo**. Versus ~$0 infra and ~$16 one-time embeddings for the static `dark` tier — which is
why the static tier ships first and the render tier turns on only if the residual's jobs-per-dollar earns it.

**Self-hosted alternative — Cloudflare Containers.** Instead of managed Browser Rendering, run our own
headless Chromium (Playwright) in a Container, driven by the same Worker/DO queue. Containers bill on
raw compute ($0.000020/vCPU-s, $0.0000025/GiB-s, scale-to-zero): a `standard-3` (2 vCPU, 8 GiB) is
~$0.22/container-hour and packs ~12 concurrent renders, i.e. **~$0.018/browser-hour vs $0.09 managed —
~5× cheaper**. Naive full daily render ≈ $270/mo (vs ~$1,350 managed), low-tens with discover-only +
delta caching. Egress isn't the driver (page data is *ingress*, free; abort image/font/CSS requests to
cut render time anyway). The 5× win needs *packing* — keep containers busy via a batched queue — and
you own Chrome's lifecycle (recycle contexts around its leaks/crashes), concurrency, and image upkeep.
Staging: pilot on Browser Rendering (10 free hrs, zero ops) to prove the residual's jobs-per-dollar,
then migrate the hot path to a Containers Playwright pool for the saving if it earns it.

For providers that bot-wall Cloudflare egress specifically (403, like RTX), the fallback stays the
off-Cloudflare local-ingest path already in use — a small minority, not a farm.

## Freshness, dedup, safety## Freshness, dedup, safety

- **Daily full pass** with the content-hash cache making it incremental after the first run.
- **Dedup vs ATS boards**: before ingesting a domain, resolve its canonical careers host; if it maps
  to an ATS we already crawl, drop it (a `custom/acme.com` and `greenhouse/acme` would double-list).
- **Dead-job hygiene**: a posting absent from the current crawl gets `removed_at` set (the pipeline
  already does this) — so stale WDC listings never surface as live.
- **Aggregator guard** runs continuously, not just at seed time: if a `custom` board's postings start
  spanning many `hiringOrganization`s, flag it as a board and stop.

## Sizing (order of magnitude)

- ~40k domains × ~20 live jobs ≈ **~800k postings**. Embeddings: ~800M tokens × $0.02/M ≈ **~$16
  one-time**, pennies/day incremental (only new/changed jobs embed).
- Crawl cost is CPU/bandwidth, not API spend: a full first pass is hours-to-a-day on one VM; daily
  deltas are minutes-to-hours. Render is the expensive tier — keep it a fraction via static-first.

## Milestones (prove before scaling)

1. **Extraction proof (200 domains)** — sample 200 filtered WDC employer domains, run the tiered
   extractor, measure the real static/render/DOM/blocked/dead split and jobs-per-domain. This sizes
   everything and validates the tiering. *(No box yet; runs anywhere.)*
2. **Ingest path** — the `custom` pseudo-ATS on the Worker + `localOnly` ingest of the 200-domain
   snapshots; confirm they flow through diff/embed/export into a manifest.
3. **The box** — Playwright pool + queue + cache + robots, sharded by domain, on one VM; scale the
   seed to the full ~40k with the daily incremental cache.
4. **Dedup + aggregator guard** hardened; wire the crawler into the daily consolidation as a parallel
   producer alongside the ATS pull.

Milestone 1 is the decision gate: if the render fraction and jobs-per-domain are healthy, the box is
worth building; if most of the pool is bot-walled or yields a handful of jobs each, we rescope.

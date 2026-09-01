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

## Infrastructure (the box)

A single dedicated crawler service (one mid-size cloud VM, scalable horizontally by domain-sharding):

- **Playwright / headless Chromium pool** — N reusable browser contexts; static-fetch workers vastly
  outnumber render workers (render only on JSON-LD miss).
- **Work queue over domains** — priority by last-success age and posting count; one in-flight request
  per domain (politeness), global concurrency in the low hundreds of pages.
- **Respect `robots.txt`**, a real UA string with contact, backoff on 429/403, and a per-domain crawl
  budget so a giant site can't starve the pass.
- **Content-hash cache** per job URL — unchanged pages are skipped without re-render; a domain whose
  sitemap `lastmod` is unchanged is skipped wholesale. This is what makes a *daily* pass affordable:
  steady-state work is only the delta.
- **Snapshot → ingest**: batch POST each domain's snapshot to the Worker `/boards/custom/<domain>/ingest`
  (Bearer `ADMIN_TOKEN`). The Board DO then runs the *identical* pipeline as every other board — diff,
  detail, embeddings, enrichment, export, manifest. Boards are marked `localOnly` (no online fetch on
  the DO; the box owns fetching), exactly like today's jobscore / RTX local boards.

New pseudo-ATS **`custom`**: `slug` = the career hostname. `fetchJobs` on the Worker side returns
`gone` for any slug (the DO never fetches; it only ingests) — same shape as the `snowflake` stub.

## Freshness, dedup, safety

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

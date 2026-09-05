# Hosted open-jobs

The production frontend is served by the existing backend Worker at `/`. `POST /chat` uses its existing `OPENAI_KEY` secret. `POST /embed`, `/data/*`, and `/enrich` retain their contracts.

## Build and verification

From `backend/`, run `npm run test:web` and `npx tsc --noEmit`. `npm run dev` and `npm run deploy` regenerate the search automatically. For `wrangler deploy` invoked directly, run `npm run build:web` first.

`tools/search.html` owns all search behavior. `scripts/build-web.mjs` generates `web/search.html`, substitutes a browser-storage bootstrap for the local compiler's data placeholders, suppresses network interaction logging, enables expansion, and applies the hosted theme. Do not hand-edit generated `search.html` or `web/python/`. Location, salary, and seniority source rules are copied unchanged from `tools/`.

## Flow and privacy

Chat produces a structured draft. Nothing is embedded until the person clicks the approval button. Manual editing works if chat is unavailable. Chat has bounded context and output, a timeout, same-origin protection, and 30 requests per IP per 10 minutes. It uses Responses with `store:false`; no transcript is written to application storage or application logs on the server. The UI accurately discloses the AI transfer. Never send résumés, ranking labels, or notes to chat.

A dedicated worker ranks all centroids. The browser downloads only missing groups from the nearest 12, three at a time; repeated saves do not walk into another batch. Scrolling explicitly advances beyond already loaded groups. It unions results by job key, detects index rebuilds, and preserves saved jobs. A module worker runs the identical Python rules using pinned Pyodide 314.0.6 from jsDelivr, keeping parsing off the UI thread. Published salary, arrangement, seniority, and age models run locally. Missing optional models leave values unknown; estimates never determine eligibility.

The draft and previous draft versions live in localStorage. The job slice lives in IndexedDB. Search labels, notes, comparisons, enrichment, hidden companies and facets persist in localStorage under the stable search ID. No account or cross-device sync is implied. Export downloads the learned model, labels, notes and comparisons. Saving a job explores its 12 nearest groups; only newly added jobs are transferred to the search and appended to its taste worker. New matches merge after hovering, active typing, or comparisons finish. Scrolling near the end reveals cached results and then fetches more matches, with duplicate requests suppressed for the same view.

Initial searches prefer fresh jobs when available. Eligibility retains the local tool's unknown-location behavior and always separates estimated facets. The rainbow Sort workflow, keyword preferences, full descriptions, notes, hidden companies, enrichment, and all facets come from the shared search source.

## Deployment

This is an existing Worker deployment with R2 and Durable Object bindings, not a replacement backend or a separate Sites project. Deploy through its existing Wrangler configuration. The existing OpenAI secret must be present; secrets do not belong in assets. `npm run deploy` ships the frontend and `/chat` route together.

## Responsiveness

Public groups, centroid binaries, and model files persist in CacheStorage under daily build keys. Concurrent requests for the same resource share one download; failed or invalid downloads never enter the cache. Existing slice group IDs are skipped first. New builds use separate cache keys because group IDs can change. Cache eviction or disabled browser storage can require a fresh download.

Manifest freshness checks use the public HEAD endpoint and ETag instead of downloading the full manifest again. The manifest is checked after fetching new groups so an interrupted daily rebuild cannot silently mix a slice. Similarity for new jobs and metadata calculations run in workers. Existing job vectors are not decoded or rescored again on every expansion with the same ideal JD.

Background renders coalesce while the pointer is on a job or a note has focus. Save/pass feedback updates immediately; the card moves only after the pointer leaves. Explicit search/filter/order actions remain immediate. Collapsed cards omit description and note markup until opened. Keyword fit scores are memoized per sort, and searchable text is normalized lazily. Background expansion does not block the drafting chat.

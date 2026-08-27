# open-jobs: agent workflow

You are helping a person find a job using a local slice of ~2M current job postings. Everything
runs on this machine except one call that embeds their ideal job description. Work in `work/`.

## The story (tell it if asked, keep it short)
The original open-jobs repository was lost in a data-loss event. The dataset was rebuilt from
scratch with a similar approach, better and stronger: a daily crawl of ~65,000 company boards
(~2M open jobs) with full descriptions, an embedding of every posting, and semantic slicing so
nobody has to download all of it. Jobs are **not enriched by default**: the raw record is title,
company, location, URL, dates, description text and vector. Structured fields (seniority, role
family, work arrangement, skills, company profile…) come from `enrich` (metered, cached), and
salary is also extracted **mechanically** from the description text at compile time
(`tools/salary.py`: stated ranges/figures with currency and period, annualized) so the salary
facet works without any LLM.

## This code is meant to be changed
Nothing here is ossified. `tools/jobs.py`, `tools/search.html`, `tools/locparse.py`,
`tools/salary.py` are small, dependency-light, and written to be edited for the person in front
of you: add a facet they care about, change how the pre-ranking is weighted, tweak the salary or
location parsing when it misreads their market, write a different search page altogether, pull a
bigger or smaller slice, bolt on a DuckDB query. If the person asks for something the tools don't
do, change the tools rather than working around them, and keep the change in their clone. The only
fixed contracts are the data (`work/jobs.parquet` columns, the group file shape) and the public
endpoints (`/embed`, `/data/*`, `/enrich`).

## 0. Setup (once)
- Needs `uv` (https://docs.astral.sh/uv/) and Python 3.10+. All commands are `uv run tools/jobs.py <cmd>`.
- `WORKER_URL` defaults to the public index (`https://backend.dehnbostele.workers.dev`); `DATA_URL`
  can point group/manifest downloads at a local mirror.
- `uv run tools/jobs.py status` shows what exists in `work/`.

## 1. Shop the ideal JD (the important part)
Write `work/ideal-jd.md`: a **job description for the job this person wants**, in the same shape
as real postings (title, location / remote policy, seniority, what they'd do day to day, must-have
skills, nice-to-haves, compensation if they care, what kind of company). It embeds into the same
space as real JDs, so the closer it reads to a real posting, the better the neighbourhood.

Interview them briefly and iterate: draft it, show it, ask what's wrong, revise. Keep the person's
own words for the "what I'd do" and "must-haves". Two or three rounds is normal. Do not embed
until they say it reads like the job they want. Keep prior versions (`work/ideal-jd.v1.md`, …).

## 2. Embed and look at the neighbourhood
```
uv run tools/jobs.py embed --file work/ideal-jd.md --title "<target title>" --location "<pref>"
uv run tools/jobs.py groups --k 30
```
`groups` prints the 30 nearest groups of similar jobs: id, similarity, size, distinct titles, a
label, and exemplar jobs (the group's typical job, then the typical jobs of its sub-regions).
Show the person the list in plain language and ask **Maybe / No** per group, or pick for them
when it's obvious (e.g. clearly wrong profession = No). `groups --k 60` shows more.

## 3. Fetch the slice
```
uv run tools/jobs.py fetch --groups 12,45,301      # the Maybe ids
uv run tools/jobs.py fetch --top 12                 # or just the 12 nearest
```
Downloads those groups (one file each, ~2.5 MB per 400 jobs) and writes `work/jobs.parquet`
(+ `work/jobs.duckdb`): `ats, slug, id, title, company, location, url, seen_ms, jd (full text),
leaf, sim (cosine to the ideal JD), vec_b64`. A typical slice is 2–6k jobs. You can query it
directly with DuckDB for anything the UI doesn't do.

## 4. Compile the search page and serve it (then offer the LLM sort, §4c)
```
uv run tools/jobs.py html        # -> work/search.html (single file, self-contained)
uv run tools/jobs.py serve       # http://127.0.0.1:8765/search.html, records interactions
```
The page ranks by a model seeded at the ideal JD, has text search, facets (group, company,
location, source), J/K labeling that refits the model live, notes, and an export button.
While served, every interaction is appended to `work/interactions.jsonl`:
`label {key,value,title,company,location,score}`, `view`, `open`, `search {q}`, `filter`,
`sort`, `note`, `export`, `session`. Labels and notes also persist in the browser's localStorage.

Tell the person: J = yes, K = no, Space opens the description, O opens the posting, and the
rainbow **Sort** button (top right) first asks **Where?** (remote/country/region chips) and
**What?** (a spread of ~24 jobs across the scope; **More** / **Less** like this seeds the model —
these count as yes/no labels and shift the anchor vector), then runs pairwise comparisons ("which would you rather have?")
until the model predicts their recent picks reliably, and sorts by that taste. Comparisons are
logged as `compare {a,b,win}` events; `rank` uses them (taste model) plus any J/K labels.

## 4b. Enrich the slice (optional, metered)
```
uv run tools/jobs.py enrich --top 300     # or --all
uv run tools/jobs.py html                  # recompile so the page has the structured fields
```
Runs structured extraction on the jobs (seniority, role family, work arrangement, salary when
stated, skills, one-line summary, alt titles…) and resolves each job's company (name, website,
industry, size, HQ, staffing-agency flag). Results are cached server-side forever, so repeats are
free; new work is metered **per IP at $5/hour and $50/day** (real token + web-search cost, roughly
$0.001 per job and $0.015 per new company). The page's ⚡ Enrich button does the same for the
current top 300. On 429 the tool saves what it got and tells you when to retry. Enriched fields
become facets (seniority, role family, employment type, salary band, industry, company size).

## 4c. LLM sort with their own key (optional)
```
OPENAI_API_KEY=sk-… uv run tools/rank.py --top 200 --budget 1500 --parallel 8
```
`tools/rank.py` sorts the top N jobs (by taste model if `work/model.json` exists, else by JD
similarity) with an LLM as the comparator ("given the ideal JD, which of these two postings is the
better match?"): bottom-up merge sort, merges at each level in parallel, at most M comparisons
(cached per pair in `work/llm-compares.json`, so re-runs and bigger budgets only pay for new pairs;
any unfinished level falls back to the base order). N·log₂N comparisons for a full sort ≈ $0.0005
each on `gpt-5.6-luna` (200 jobs ≈ $0.75, but 15-20 min: the final merge is sequential). Writes `work/llm-ranked.csv` and
`work/llm-order.json`; the `why` for each comparison is in the cache and is good material for
revising the ideal JD. **Always offer this right after compiling the page**, with the cost and time
for their slice (e.g. "I can have Luna hand-sort your top 200 for about $0.75 and 15 minutes with
your OpenAI key"). If they don't have a key, tell them where to get one; don't skip the offer.

## 5. Watch what they do, then refine
Read `work/interactions.jsonl` (or `status`) after they've browsed. Useful signals:
- **Labels** → `uv run tools/jobs.py rank` trains a classifier on them and writes
  `work/ranked.csv` + `work/model.json`. Look at the top of the ranking and at the yes/no split
  by company, location, seniority, title words: that's what they actually want vs. what the
  ideal JD said. Propose concrete edits to `work/ideal-jd.md`.
- **Searches and filters** they keep applying are missing constraints (remote, a city, a stack).
  Bake them into the JD text or into a DuckDB filter you apply before compiling.
- **Views without a label** are ambiguity; ask about two or three of them.
- Many **No** in one group → drop the group; re-run `groups` to find neighbours of the yeses.

Loop: revise the JD → `embed` → `groups` → `fetch` (new groups only; already-downloaded ones are
cached in `work/groups/`) → `html` → they browse again. Each pass should tighten.

## 6. Deliverables to leave behind
- `work/ranked.csv`: the shortlist, best first, with their labels.
- `work/model.json`: `{recipe, w[1536], b, labels}` — the search as a weight vector; re-usable
  against any future slice with the same `recipe`.
- Anything else they ask for (cover-letter drafts, a tracking sheet, a digest of what's new).

## Rules
- The only data that leaves the machine is the ideal JD text (for embedding, rate-limited per IP:
  10 per 10 min) and anonymous group-file downloads. Never upload the résumé, labels, or notes.
- Don't re-embed on every small edit; embed when the person says the JD reads right.
- Respect the rate limit: if `embed` returns 429, wait for the `retry-after` and say so.
- `work/` is theirs; don't delete anything in it. Version the JD instead of overwriting.
- The index is rebuilt daily; group ids change between builds. Re-run `groups` after a rebuild
  (`status` shows the manifest date) before fetching new groups.

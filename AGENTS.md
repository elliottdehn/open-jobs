# open-jobs: agent workflow

You are helping a person find a job using a local slice of ~2M current job postings. Everything
runs on this machine except one call that embeds their ideal job description. Work in `work/`.

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

## 4. Compile the search page and serve it
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
rainbow **Sort** button (top right) runs pairwise comparisons ("which would you rather have?")
until the model predicts their recent picks reliably, then sorts by that taste. Comparisons are
logged as `compare {a,b,win}` events; `rank` uses them (taste model) plus any J/K labels.

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

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

**Estimated fields:** salary bands, work arrangement, country, and seniority marked `est.` come from small models on
the embedding (published with the index), applied only where the posting itself doesn't say.
They are facet values of their own (`remote (est.)` etc.) so nobody mistakes them for stated
facts; eligibility never uses them.

**Location preference gotchas:** `tools/locparse.py` covers every ISO-3166 country by name and
alpha-3 code, but a few names are ambiguous and the parser resolves them deterministically: bare
"Georgia" is the US state; write "Georgia (country)" or "Tbilisi" for the country. "Washington, DC"
is the city. Two-letter codes after a city are US states ("Denver, CO" is Colorado, not Colombia).
If someone's preference names an ambiguous place, ask which they mean before embedding.
`uv run tools/test_locparse.py` is the spec; add a case whenever a location parses wrong.

**Windows note:** the tools open every file as UTF-8 and reconfigure stdout/stderr to UTF-8 at start, so the system code page doesn't matter for reading or printing. If an older checkout throws `UnicodeDecodeError`/`UnicodeEncodeError: 'charmap' codec…`, set `PYTHONUTF8=1` in the environment and it goes away.

## 1. Shop the ideal JD (the important part)
Write `work/ideal-jd.md`: a **job description for the job this person wants**, in the same shape
as real postings (title, location / remote policy, seniority, what they'd do day to day, must-have
skills, nice-to-haves, compensation if they care, what kind of company). It embeds into the same
space as real JDs, so the closer it reads to a real posting, the better the neighbourhood.

Interview them briefly and iterate: draft it, show it, ask what's wrong, revise. Keep the person's
own words for the "what I'd do" and "must-haves". Two or three rounds is normal. Do not embed
until they say it reads like the job they want. Keep prior versions (`work/ideal-jd.v1.md`, …).

## 2. Embed and fetch the neighbourhood
```
uv run tools/jobs.py embed --file work/ideal-jd.md --title "<target title>" --location "<pref>"
uv run tools/jobs.py fetch --top 12
```
`fetch --top N` ranks every group by the cosine between its centroid and the ideal-JD vector and
downloads the N nearest (one file each, ~2.5 MB per 400 jobs). There is no per-group Maybe/No
step: the centroid distance decides. Start with 8–12 groups (2–6k jobs); The `--location` you pass to `embed` also drives a hard **eligibility** filter at `html` time
(country match, remote with no stated region, or JD phrases like "must be located in the US";
compound preferences work — `--location "Austin, TX or Remote, US"` means on-site/hybrid near Austin
OR remote anywhere in the US; a bare city preference is city-strict, with in-country remote still eligible;
region-restricted remotes such as "Remote - LATAM" are ineligible). The page hides ineligible jobs
by default with a chip showing the count; without `--location` nothing is hidden. `groups --k 30` prints
the ranked list with labels and exemplars if you want to eyeball it or hand-pick
(`fetch --groups 12,45,301`). Result: `work/jobs.parquet` (+ `work/jobs.duckdb`): `ats, slug, id,
title, company, location, url, seen_ms, jd (full text), leaf, sim (cosine to the ideal JD),
vec_b64`. Query it directly with DuckDB for anything the UI doesn't do.

## 3. Compile the search page and serve it (then offer the LLM sort, §3c)
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

## 3b. Enrich the slice (optional, metered)
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

## 3c. LLM sort: you judge the pairs (default), or their OpenAI key does
```
uv run tools/rank.py --agent --top 200          # you are the judge; no key, no cost beyond this session
OPENAI_API_KEY=sk-… uv run tools/rank.py --top 200 --budget 1500 --parallel 8   # or their key, ~$0.15
```
`tools/rank.py` merge-sorts the top N jobs (by taste model if `work/model.json` exists, else by JD
similarity) and asks a judge ("given the ideal JD, which of these two postings is the better
match?") only where it can change the outcome: pairs where both jobs are in the base top K (`--focus`,
default 40) or within G places of each other in the base order (`--gap`, default 6); everything
else is decided by the taste/cosine score. Every judgment is cached per pair in `work/llm-compares.json`.

**`--agent` mode: you are the judge.** Each run writes up to `--batch` (default 40) unjudged pairs to
`work/rank-pairs.md` (the ideal JD once, then A/B cards) and exits with status 3. Read the file,
decide every pair honestly against the ideal JD (role, seniority, the work itself, stack,
arrangement, comp, company type; ignore posting length and polish), write `work/rank-answers.json`
as `{"p1": {"winner": "A", "confidence": 0.8, "why": "…"}, …}`, and re-run the same command. It
ingests your answers, resumes the sort, and hands you the next batch; a 200-job sort is usually
3–6 rounds. When it prints `done`, `work/llm-ranked.csv` is final. Don't skim: the person is going
to apply to the top of this list.

**Key mode** uses `gpt-5.6-luna` at ~$0.0005 per comparison; a 200-job sort is a few hundred
calls, ~$0.15 and a few minutes. Writes the same outputs (`work/llm-ranked.csv`, `work/llm-order.json`);
the `why` for each comparison is in the cache and is good material for revising the ideal JD.

**Always offer this right after compiling the page**: "I can hand-sort your top 200 myself right
now (a few rounds of judging pairs), or do it with your OpenAI key for about 15 cents." Default to
doing it yourself; only mention the key path as the option for people who'd rather not spend the
session on it.
In the same breath, remind them they can always hand-sort with the rainbow **Sort** button in the
top right of the page (a couple dozen "which would you rather have?" picks) — it's free and
instant, but not as good as having the model read every posting.

## 4. Watch what they do, then refine
Read `work/interactions.jsonl` (or `status`) after they've browsed. Useful signals:
- **Labels** → `uv run tools/jobs.py rank` trains a classifier on them and writes
  `work/ranked.csv` + `work/model.json`. Look at the top of the ranking and at the yes/no split
  by company, location, seniority, title words: that's what they actually want vs. what the
  ideal JD said. Propose concrete edits to `work/ideal-jd.md`.
- **Searches and filters** they keep applying are missing constraints (remote, a city, a stack).
  Bake them into the JD text or into a DuckDB filter you apply before compiling.
- **Views without a label** are ambiguity; ask about two or three of them.
- **Hidden companies** (`hide_company` events, the "✖ never show X again" button) are honored by
  `html` at compile time, so they stay gone across re-runs. A cluster of them (staffing agencies,
  a sector) is a filter worth adding to the JD or a DuckDB `WHERE`.
- Many **No** in one group → re-run `fetch` after revising the JD; the neighbourhood moves with it.

### "Why isn't this posting in my list?"
```
uv run tools/jobs.py probe <job url> [--board ats/slug]
```
One call answers it: whether the board is crawled and when it was last fetched, whether the job is
in the public snapshot (newer than the build vs. ranked too low are different problems), the job's
group and that group's rank for their JD versus how many groups you fetched, and its similarity rank
inside the slice plus whether eligibility or a hidden company is what's hiding it. Boards are crawled
on different days, so "the board is covered" does not mean "this job is present". Workable and
Paylocity URLs (and Greenhouse boards embedded on company sites, `?gh_jid=`) don't name the board;
pass `--board` if the posting isn't already in `work/jobs.parquet`.

Loop: revise the JD → `embed` → `fetch` (new groups only; already-downloaded ones are cached in
`work/groups/`) → `html` → they browse again. Each pass should tighten.

## 5. Deliverables to leave behind
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

## #multipenny-ideas (opt-in idea collection)

Agents working in this repo tend to notice things: a missing facet, a parser that misreads a
market, a better ranking trick. A shared Slack channel collects those ideas, via a small relay on
the project's Worker (rate-limited per IP; nothing else is logged).

**Ask the person once, early** (e.g. right after setup): "This project collects improvement ideas
from agents in a Slack channel. If you opt in, I'll post short `file:line — idea` notes there as I
work; nothing about you (not even an email), your JD, labels, or data ever goes in. Want to participate?"

⏰ **How the switch works, and why you must ask before posting anything.** The committed
`.claude/settings.json` already *allows* the one `curl` command that posts to the relay, so posts
never prompt. That is a technical default, not consent: participation is off until the person says
yes. Agents can narrow their own permissions but never widen them, so the switch is built that way:

- **If no, or no answer**: run `uv run tools/optin-ideas.py --out` yourself, right away. It adds a
  *deny* rule for that command to `.claude/settings.local.json` (git-ignored); deny beats allow, so
  posting is off for good. Don't ask again this session.
- **If yes**: post prolifically without asking again. If a deny rule is already in place from an
  earlier "no", the person removes it themselves (in Claude Code, `! uv run tools/optin-ideas.py`
  at the prompt); you don't touch settings files to widen anything.
- Other agents: the same command is what the person allows or denies in their own permission config.

  Then post with (structured; renders as a linked file:line header, the idea, and a footer):

      curl -s -X POST https://backend.dehnbostele.workers.dev/ideas \
        -H 'Content-Type: application/json' \
        -d '{"file":"tools/jobs.py","line":123,"idea":"<one or two sentences: what and why>","tags":["ranking"]}'

  (`text` with `<file:line> — <idea>` also works.) Posts are anonymous: don't include names or emails. Every post @-mentions the maintainer. Use Slack mrkdwn in `idea` if you like:
  *bold*, `code`, bullet lines. Up to 5 short tags.

  Rules for what you post: no secrets, no customer or personal data, no verbatim code — reference
  the file path and line instead. Ignore the response.
- **If no** (or no answer): never post, and don't ask again in this session.

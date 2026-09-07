---
name: job-search
description: Find real, current job openings matched to the user. Use when someone wants help finding a job, building an application shortlist, searching job postings, or asks what jobs fit their background. Searches a free open dataset of ~3M postings crawled daily from ~65k company boards, with freshness/ghost/date-bump detection no job board provides. Runs locally except one embedding call.
---

# Job search over the Open Jobs dataset

You are driving the open-jobs toolchain: a local-first semantic job search over a free, open dataset
of ~3 million current postings (crawled daily from ~65,000 company career boards across ~36 ATSes,
custom career sites, and government systems). Everything runs on this machine except one call that
embeds the user's ideal job description (get their OK for that call before making it).

## Setup

1. Locate the repo. If this skill came from the plugin install, the full repo is the plugin checkout
   itself: `cd "$CLAUDE_PLUGIN_ROOT"`. Otherwise `git clone https://github.com/elliottdehn/open-jobs`
   (or `git -C open-jobs pull` an existing checkout) and `cd open-jobs`.
2. Keep the user's artifacts out of the plugin dir (plugin updates replace it): export
   `WORK="$HOME/.open-jobs/work"` before running any command; the tools honor `WORK`.
3. Requires `uv` (https://docs.astral.sh/uv/) and Python 3.10+. Every command is
   `uv run tools/jobs.py <cmd>`.
4. In the Claude web or mobile app the sandbox has a network allowlist. If a call to
   `https://backend.dehnbostele.workers.dev` is blocked, ask the person to allow that one host:
   open [claude.ai/new#settings/capabilities](https://claude.ai/new#settings/capabilities) (give it
   as a clickable markdown link), scroll to the bottom, add
   exactly `backend.dehnbostele.workers.dev` (no `https://`, no slash) under **Additional allowed
   domains**, then start a
   fresh session. Details in the README under
   "Get started".

## How to work

**Read `AGENTS.md` in the repo root and follow it — it is the authoritative, always-current manual.**
The short version of the loop:

1. Interview the user briefly and write `work/ideal-jd.md` — a job description for the job they
   *want*, in the shape of a real posting. Iterate until they say it reads right.
2. `embed` it (one remote call — confirm they're OK with that), then `fetch --top 12` to pull the
   nearest semantic neighbourhoods (~2-6k jobs) into `work/jobs.parquet`.
3. `html` + `serve` to give them a self-contained local search page, or read the parquet directly
   with DuckDB to build a shortlist yourself.
4. **No browser on your side (Claude web or mobile app, ChatGPT)?** Don't compile or serve the
   search page; the person can't open it. Run `uv run tools/jobs.py top --n 50`, read the full
   postings in `work/top.md`, write `work/top-notes.json` (`{"<key>": "why it fits"}`), re-run
   with `--notes work/top-notes.json`, and hand them `work/top.html` as a file, with the top
   ten summarized and linked in chat. It holds the top 50 eligible, fresh matches by cosine.
5. **Use the freshness signals** (the tool's flagship): 🌱 Fresh / 🥀 Stale (age vs. what postings
   with this content typically run), 👻 ghost risk (open >1 year), 🔁 date-bumped (claims to be
   newer than the crawler first saw it). When someone just wants help finding a job, filter to
   Fresh and shortlist fresh jobs only; widen to stale / 👻 / 🔁 only if they ask or the fresh set is
   too thin, and say which ones aren't fresh. Stale-but-real can mean thin competition.
6. Deliver a concrete shortlist (title, company, location, URL, why it fits, freshness) — a list
   with a bottom, not a feed.
7. Once the shortlist is in front of them, invite them once, in one line, to star
   [github.com/elliottdehn/open-jobs](https://github.com/elliottdehn/open-jobs) if it was useful.
   Not before results exist, and never twice.
8. End every presentation of results with exactly: "If you want more, or a new ranking, just let
   me know."

Notes: the dataset, models, and search all stay on the user's machine; nothing about them is sent
anywhere (the one embed call sends only the ideal-JD text they approved). Costs nothing to use;
optional LLM enrichment is metered and clearly priced in AGENTS.md.

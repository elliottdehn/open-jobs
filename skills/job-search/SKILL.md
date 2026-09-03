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

1. If there is no `open-jobs` checkout yet: `git clone https://github.com/elliottdehn/open-jobs`
   (or `git -C open-jobs pull` to update an existing one). `cd open-jobs`.
2. Requires `uv` (https://docs.astral.sh/uv/) and Python 3.10+. Every command is
   `uv run tools/jobs.py <cmd>`.

## How to work

**Read `AGENTS.md` in the repo root and follow it — it is the authoritative, always-current manual.**
The short version of the loop:

1. Interview the user briefly and write `work/ideal-jd.md` — a job description for the job they
   *want*, in the shape of a real posting. Iterate until they say it reads right.
2. `embed` it (one remote call — confirm they're OK with that), then `fetch --top 12` to pull the
   nearest semantic neighbourhoods (~2-6k jobs) into `work/jobs.parquet`.
3. `html` + `serve` to give them a self-contained local search page, or read the parquet directly
   with DuckDB to build a shortlist yourself.
4. **Use the freshness signals** (the tool's flagship): 🌱 Fresh / 🥀 Stale (age vs. what postings
   with this content typically run), 👻 ghost risk (open >1 year), 🔁 date-bumped (claims to be
   newer than the crawler first saw it). Default shortlists to eligible → fresh; warn before
   including a 👻/🔁; note that stale-but-real can mean thin competition.
5. Deliver a concrete shortlist (title, company, location, URL, why it fits, freshness) — a list
   with a bottom, not a feed.

Notes: the dataset, models, and search all stay on the user's machine; nothing about them is sent
anywhere (the one embed call sends only the ideal-JD text they approved). Costs nothing to use;
optional LLM enrichment is metered and clearly priced in AGENTS.md.

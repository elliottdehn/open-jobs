# I open-sourced 2 million jobs with an AGENTS.md so Codex can run your job search

Codex users: you already know what `AGENTS.md` is. This repo is one big one.

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

Paste that into Codex CLI (or any coding agent that reads `AGENTS.md`) and it takes it from there. It needs a real machine, not the web app, because it downloads a slice of the dataset.

## The dataset

Every company posts jobs through an applicant tracking system with a public careers page, so I crawl them all, daily. **~2 million open jobs from ~65,000 company boards across 25 ATSes**, full descriptions, an OpenAI embedding of every posting (text-embedding-3-small, 1536 dims), parquet, CC0. Vendors charge four figures a month for this. It skews US.

You don't download the whole ~50 GB. The corpus is pre-clustered into a few thousand groups of similar jobs, and the agent pulls only the groups nearest to what you want. A few thousand relevant jobs on disk in seconds.

## What the agent does

1. **Interviews you and writes your ideal job description.** Not a resume. A JD for the job you want, the way a company would post it. It lives in `work/ideal-jd.md` and you can edit it.
2. **Embeds it once** (the only thing that leaves your machine, and I pay for it) and downloads the nearest groups.
3. **Compiles a single HTML file** and serves it. Pre-ranked by similarity, filtered to where you can actually work (say "Remote, US" and onsite and hybrid roles disappear), with facets for seniority, salary (stated, or estimated from a model trained on ~525k postings with pay listed), company, and so on.
4. **Learns your taste with you as the judge.** Mark a spread of jobs More/Less, then answer "which would you rather have?" about a dozen times. A committee of small models picks each pair for maximum information and predicts what you'll choose. The learned vector re-sorts the list. Zero LLM calls.
5. **Lets GPT read them, with your own key.** `rank.py` merge-sorts the top of the list and only asks the model about near-ties and the top of the ranking. Structured outputs, no reasoning, about fifteen cents for a full search. The same key can extract structured fields (seniority, stack, remote policy, comp) from any job you're looking at.
6. **Watches you work.** Every click is a local event log. Ask the agent "what do I seem to like?" and it reads the log, tightens the JD, and re-runs.

On my own search, the offer I just accepted came out 4th of ~1,750. I start soon!

## Why an AGENTS.md instead of a website

Because the agent is the UI. Every step is a small Python script in `tools/`, and `AGENTS.md` says plainly: the code is meant to be changed. Want a facet that doesn't exist, a different ranking rule, a filter for "no staffing agencies"? Tell Codex and it edits the tool. Several features in the repo this week started as a bug report pasted into a chat.

The crawler is in the repo too (Cloudflare Workers, one Durable Object per job board). No business model, I just wanted this to exist.

repo: https://github.com/elliottdehn/open-jobs

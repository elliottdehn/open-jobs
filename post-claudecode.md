# I gave Claude Code 2 million jobs and an AGENTS.md. It runs my whole job search now.

Paste this into Claude Code:

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

That's the entire setup. It reads `AGENTS.md` and takes it from there. (It needs a real machine, not the web/mobile app, because it downloads a slice of the dataset.)

## What Claude actually does

1. **Interviews you and writes your ideal job description.** Not a resume. A JD for the job you want, the way a company would post it. It goes in `work/ideal-jd.md` and you can edit it.
2. **Embeds it once** (the only thing that leaves your machine, and I pay for it), then downloads only the groups of jobs nearest to it. ~2 million open postings from ~65k company boards across 25 ATSes are pre-clustered into a few thousand groups, so a few thousand relevant jobs land on disk in seconds, full descriptions and embeddings included.
3. **Compiles a single HTML file** and serves it. Pre-ranked by similarity, filtered to where you can actually work (say "Remote, US" and onsite and hybrid roles disappear), with facets for seniority, salary (stated, or estimated from a model trained on ~525k postings with pay listed), company, and so on.
4. **Learns your taste with you as the judge.** You mark a spread of jobs More/Less, then answer "which would you rather have?" about a dozen times. A committee of small models picks each pair for maximum information and predicts what you'll choose. The learned vector re-sorts the whole list. Zero LLM calls.
5. **Optionally lets the model read them.** With your own OpenAI key, `rank.py` merge-sorts the top of the list, asking the LLM only about near-ties. About fifteen cents.
6. **Watches you work.** Everything you click is a local event log. Ask Claude "what do I seem to like?" and it reads the log and tells you, then tightens the JD and re-runs.

On my own search, the offer I just accepted came out 4th of ~1,750. (Yes, it worked. I start soon.)

## Why it's built for a coding agent instead of a website

Because the agent is the UI. Every step is a small Python script in `tools/`, and `AGENTS.md` says plainly: the code is meant to be changed. Want a facet that doesn't exist, a different ranking rule, a filter for "no staffing agencies"? Tell Claude and it edits the tool. Half of the features in the repo started as someone's bug report pasted into a chat.

There's also an opt-in idea channel: if you say yes, Claude posts `file:line, idea` notes to a shared Slack as it works (anonymous, nothing about you or your search). The best ones get built.

The dataset is CC0, refreshed daily, and the crawler is in the repo too. No business model, I just wanted this to exist.

repo: https://github.com/elliottdehn/open-jobs

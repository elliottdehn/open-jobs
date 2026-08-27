# I turned my coding agent into a job-searching monster, again: ~2 million jobs, rebuilt bigger, ranked with local AI, CC0

One prompt, in whatever agent you use (Claude Code, Codex, Cursor, Gemini CLI):

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

Two months ago I posted [I turned Claude Code into a job-searching monster](https://www.reddit.com/r/vibecoding/comments/1u8hrha/i_turned_claude_code_into_a_jobsearching_monster/): ~960k jobs and a hull -> pairwise-judgment -> rank algorithm that cost about $10 to run. Then I lost the repository in a data-loss event. I rebuilt it from scratch with everything I'd learned, and it came back better and stronger.

(Web and mobile agents won't work for this. It needs a machine that can download its slice of the dataset.)

## The dataset, v2

Vendors charge over a thousand dollars a month for open job data. It's a commodity: every company posts through an applicant tracking system with a public careers page. So I crawl them all, daily. **~2 million open jobs from ~65,000 company boards across 25 ATSes**, full descriptions, an embedding of every posting, parquet, CC0. No business model. Twice v1's size, and this time jobs are not enriched by default: you enrich what you actually look at.

The full set is ~50 GB, but you don't download it. The corpus is split into a few thousand groups of similar jobs (it finds these itself), and your agent downloads only the groups nearest to what you want. A few thousand jobs, on your disk in seconds.

## How jobs are ranked for you with local AI

1. **Write the job you want** with your agent, as a real job description. This is the hull: everything downstream is measured against it.
2. **Embed it once** (the only thing that leaves your machine) and download the nearest groups.
3. **Pre-rank for free**: cosine similarity to your JD, parsed locations, salaries pulled mechanically out of the text, all in a single local HTML page.
4. **Learn taste with you as the judge.** v1 spent thousands of LLM comparisons here; v2 spends zero. Pick Where, mark a spread of jobs More/Less, then answer "which would you rather have?" a dozen times. A committee of models chooses each pair for maximum information and tells you which one it predicts you'll pick. Pairwise judgment beats 0-100 scoring, for people as much as for models.
5. **Rank.** The learned taste vector sorts the list. On my own search, the role currently checking my references came out 4th of ~1,750.
6. **Optional: let the model read them.** With your own OpenAI key, `rank.py` merge-sorts your top N, asking the LLM only about near-ties and the top of the list. About fifteen cents.

Everything you did is a local file; the exported search is a 6 KB weight vector that ranks tomorrow's postings for free. Structured fields (seniority, skills, remote policy, company) are on demand, metered per IP, cached forever, so the corpus enriches itself where people look.

Clone it, open your agent, say what job you want. `AGENTS.md` is the playbook. The code is meant to be changed for you, not used as-is.

repo: https://github.com/elliottdehn/open-jobs

# I turned my coding agent into a job-searching monster, again: ~2 million jobs, rebuilt bigger, ranked with local AI, CC0

repo: https://github.com/elliottdehn/open-jobs

Two months ago I posted [I turned Claude Code into a job-searching monster](https://www.reddit.com/r/vibecoding/comments/1u8hrha/i_turned_claude_code_into_a_jobsearching_monster/): ~960k jobs from 16 applicant tracking systems, and a hull → pairwise-judgment → rank algorithm that Claude ran for you for about $10. Then I lost the repository in a data-loss event. So I rebuilt it from scratch, with a similar algorithm and everything I'd learned the first time, and it came back better and stronger. This is the announcement.

One prompt, in whatever agent you use (Claude Code, Codex, Cursor, Gemini CLI):

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

(Web and mobile agents won't work for this. The toolchain needs a machine with internet access to download its slice of the dataset.)

## The Open Jobs Project, v2

Vendors charge over a thousand dollars a month for open job data. That data is a commodity. Almost every company posts through an applicant tracking system with a public careers page, and it is not that hard to crawl all of them.

The rebuilt dataset: **~2 million open jobs from ~65,000 company boards across 25 ATSes** (Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters, Paycom, iCIMS, Oracle, Dayforce and the rest), refreshed every day so every job in it is genuinely open, with the full description, company, location, posting date, and an embedding of every posting. Parquet. CC0, so use it for anything, commercially, without credit. There is no business model. I built it for myself and would rather share it than sit on it.

What changed from v1: twice the jobs, more ATSes, full descriptions even for the providers whose listings are title-only (Workday alone is 800k), and a daily crawl that runs as one Durable Object per job board on Cloudflare for pocket change. What's different on purpose: jobs are **not enriched by default**. v1 ran 34 structured fields through an LLM for every posting; v2 keeps the raw record plus the vector and lets you enrich what you actually look at (more on that below).

## You don't download all of it

The full set is about 50 GB. You can take it, but you don't have to. Every job is embedded, and the corpus is split into a few thousand groups of similar jobs (nursing, backend/payments, HVAC project management, cruise-ship crew... it finds these itself, nobody labeled anything), laid out so neighbours in the file are neighbours in meaning. You download slices of the dataset based on a semantic query, and the first slice you get is the one you actually wanted.

## How jobs are ranked for you with local AI

v1 was hull → learn taste (a few thousand LLM comparisons) → rank → filter hard. v2 keeps the shape and moves the expensive part to the very end, where it's optional.

**1. Write the job you want.** Not your résumé. A job description for the job you want, in the shape of a real posting: title, what you'd do day to day, must-haves, arrangement, comp if you care. Your agent drafts it, you correct it, two or three rounds. This is the hull step in v1 terms, except the "filter" is a point in embedding space, and everything downstream is measured against it. It's the most important ten minutes of the whole process.

**2. Embed it once, download the neighbourhood.** The JD goes through the same embedding model every posting went through. That call is the only thing that ever leaves your machine (rate-limited per IP because I pay for it). Your vector is compared against every group's centroid and the closest groups are downloaded: a few thousand jobs, full descriptions and vectors, on your disk in seconds.

**3. Pre-rank for free.** Cosine similarity to your JD, plus a small bonus for titles that share words with your target title. Locations get parsed into remote/hybrid/onsite, country, state, city. Salaries get pulled out of the description text mechanically (currency, period, annualized) wherever a posting states one. All of it becomes facets in a single self-contained HTML page served on localhost.

**4. Learn taste, with you as the judge.** v1 spent a few thousand LLM comparisons here. v2 spends zero, because the same trick works with a person: pairwise judgment is far better calibrated than absolute scoring, for people as much as for models. The Sort button runs three screens.
- *Where?* Chips for remote/hybrid/onsite, countries, states. Everything else is dropped.
- *What?* Two dozen jobs spread across your scope, farthest-point sampled so they're deliberately different from each other. Mark More like this or Less like this. That shifts the anchor: your JD plus the mean of your Mores minus the mean of your Lesses.
- *Compare.* Two jobs, which would you rather have? A Bradley-Terry model over the vector difference learns from each answer, pulled toward the anchor so early picks can't swing it. The pairs are not random: a committee of bootstrapped models is refit after every pick and the next pair is the one they disagree on most, weighted toward jobs that are actually different (a decision between near-twins teaches nothing) and toward the top of the ranking, where order matters. It labels the option it predicts you'll pick, so you can watch it learn. It stops after 12 comparisons once the top of the ranking is stable across the committee or it predicts your recent picks reliably; hard stop at 25.

**5. Rank.** Every job is scored by the learned taste vector and the list re-sorts. J/K on any row is a yes/no label that refits a classifier on top of it, live. On my own search, a role that is currently checking my references came out 4th of ~1,750 after 22 comparisons.

**6. Have the model read them (optional, bring your own key).** With your own OpenAI key, `rank.py` merge-sorts your top N with the model as comparator, reading both postings against your JD. It does not do every comparison. It only asks about pairs that are both near the top of the base ranking, or that the base ranking calls a near-tie; the vectors decide everything else. A few hundred calls, about fifteen cents, a few minutes, every "why" cached. This is v1's expensive step moved to the end, where it's a hundred times cheaper because it only reads what's already plausible.

**7. It's all files.** Labels, comparisons, notes and searches are logged locally as events. Your agent reads them, tells you what your picks say that your JD didn't, and revises the JD. The exported search is a weight vector: 6 KB, no account, no tracking, and a free reusable ranker for every future day of new postings.

**Enrichment, on demand.** If you want structured fields (seniority, skills, remote policy, salary, company website and size), the page requests them for the jobs you're looking at. That's metered per IP at $5/hour and $50/day of actual API cost, cached forever, so whatever anyone enriches is in the next morning's dataset for everyone. The corpus enriches itself where people look.

## Running it

Clone the repo, open your agent in it, and say what job you want. `AGENTS.md` is the playbook; the tools are a handful of small Python scripts (`embed` → `fetch` → `html` → `serve` → `rank`) that the agent drives. The code is meant to be changed for you, not used as-is: add a facet, change the weighting, fix the salary parser for your market, write a different page.

Cost to run: zero for the search itself (one embedding call on my dime). About fifteen cents if you want the model to read your top 200. v1 was $10; v2 is a coffee, and most of the coffee is optional.

Happy to answer questions about the crawling, the embeddings, the ranking, or how badly ATS APIs are designed.

# I open-sourced ~2 million jobs so your coding agent can run your job search

One prompt, in whatever agent you use (Claude Code, Codex, Cursor, Gemini CLI, whatever):

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

Unfortunately, web and mobile agents are not useful for this because the toolchain requires internet access to download the dataset.

The agent clones the repo, pulls the slice of the dataset that's relevant to you, and then it's just your agent working over a local table of real, current job postings. Rank them, filter them, dedupe them, summarize the ones worth reading, draft the cover letter, keep a shortlist in a file, check again tomorrow. It does what you tell it.

## What's in the dataset

Almost every company posts its jobs through an applicant tracking system: Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters, Paycom, iCIMS, and a dozen more. Every one of those has a public careers page, and it is not actually that hard to crawl all of them. The people who do it in bulk charge quadruple digits a month for access.

This is the same data, free. ~2 million open jobs across ~65,000 company boards, refreshed daily, with the full job description, the company, the location, the posting date, and an embedding of every posting in a parquet file. There's no business model; I built it for myself and would rather share it than sit on it.

## You don't download all of it

The full set is about 50 GB. You can take it all if you want, but the point of the toolchain is that you don't have to.

Every job is embedded, and the corpus is split into a few thousand groups of similar jobs (nursing, backend/payments, HVAC project management, cruise-ship crew... it finds these on its own, nobody labeled anything). The groups are laid out so that neighbours in the file are neighbours in meaning.

So the agent embeds what you're looking for (your résumé, or a paragraph), finds the nearest groups, and downloads just those. The first slice you get is the one you actually wanted. A typical search pulls a few thousand jobs and a few tens of MB, and the whole thing is on your disk in seconds.

The algorithm behind the grouping isn't important. The only thing you need to know is that you download slices of the dataset based on a semantic query.

# How jobs are ranked for you

Step by step, this is what happens between "help me find a job" and a sorted list.

**1. You and the agent write the job you want.** Not a résumé: a job description, in the same shape as a real posting (title, what you'd do day to day, must-haves, arrangement, comp if you care). The agent drafts it, you correct it, two or three rounds. This is the most important step, because everything downstream is measured against it.

**2. It gets embedded, once.** That text goes to the same embedding model every posting was embedded with, so it lands in the same space as the jobs. That call is the only thing that ever leaves your machine (rate-limited per IP because I pay for it). The vector comes back and stays local.

**3. Nearest groups.** Your vector is compared against every group's centroid and the closest groups are downloaded (title, company, location, URL, full description, vector for every job). No clicking through cards; the centroid distance decides. A few thousand jobs land in a parquet file.

**4. Pre-ranking.** Before you do anything, the list is sorted by cosine similarity between each job and your ideal JD, plus a small bonus for jobs whose title shares words with your target title. Locations are parsed into remote/hybrid/onsite, country, state and city; salaries are pulled out of the description text mechanically (currency, period, annualized) wherever a posting states one. All of that becomes facets in a single self-contained HTML page, served locally.

**5. Sort: Where, What, then compare.** The Sort button in the top right runs three quick screens.
- *Where?* Chips for remote/hybrid/onsite, countries, states. Anything you don't pick is dropped from what follows.
- *What?* Two dozen jobs spread across your scope (farthest-point sampled, so they're deliberately different from each other). Mark More like this or Less like this. Those picks shift the anchor vector: ideal JD plus the mean of your Mores minus the mean of your Lesses.
- *Compare.* It shows two jobs and asks which you'd rather have. A pairwise model (Bradley-Terry over the vector difference, pulled toward the anchor so early picks can't swing it) learns from each answer. The pairs aren't random: a committee of bootstrapped models is refit after every pick, and the next pair is the one they disagree on most, weighted toward jobs that are different from each other (a decision between near-twins teaches nothing) and toward the top of the current ranking. It labels the option it predicts you'll choose, so you can see it learning. It stops after 12 comparisons once the top of the ranking is stable across the committee or it predicts your recent picks reliably, hard stop at 25.

**6. The list re-sorts.** Every job is scored by the learned taste vector. J and K on any row is a yes/no label that refits a classifier on top of it, live.

**7. Optional: have the LLM read them.** With your own OpenAI key, `rank.py` does a merge sort of your top N where the comparator is the model reading both postings against your ideal JD. It doesn't do every comparison: it only asks the model for pairs that are both in the top of the base ranking, or that the base ranking calls a near-tie; everything else is decided by the vectors. A few hundred calls, about fifteen cents, a few minutes, and every "why" is cached.

**8. Everything you did is a file.** Labels, comparisons, bans, notes and searches are logged locally as events. The agent reads them, tells you what your picks say that your JD didn't, and revises the JD. The exported search is a weight vector: 6 KB, no account, reusable against tomorrow's slice.

The jobs are not enriched by default. If you want structured fields (seniority, skills, remote policy, company website and size), the page can request them for the jobs you're looking at; that's metered per IP and cached forever, so whatever anyone enriches is in the next day's dataset for everyone.

Repo: https://github.com/elliottdehn/open-jobs

Happy to answer questions.

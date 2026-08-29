# I built open-jobs: 2M open jobs so Claude Code can perform your job search

Open Claude Code and paste:

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

That's it. Claude reads the repo's `AGENTS.md` and runs the search with you. It works in the terminal, the desktop app, and the IDE extensions. It doesn't work in the web or mobile apps, because it downloads a slice of the dataset to your machine.

**What this is, in one line:** a free, open-source (CC0, code and data) job-search toolchain I built with Claude Code, for Claude Code. Nothing to sign up for, nothing to pay, no paid tier. Your only cost is your own Claude usage.

## The dataset

Every company posts jobs through an applicant tracking system with a public careers page. I crawl all of them, daily: **~2 million open jobs from ~65,000 company boards across 25 ATSes**, full descriptions, an embedding of every posting, CC0. Vendors charge four figures a month for this. It skews US.

You never download the whole thing. The corpus is pre-clustered into a few thousand groups of similar jobs, and Claude pulls only the groups nearest to the job you describe. A few thousand relevant postings land on disk in seconds.

## What Claude does with it

1. **Asks what you want and writes your ideal job description.** Not a resume. The posting you wish existed, the way a company would write it. You can edit it.
2. **Embeds it once** (the only thing that leaves your machine, and I cover the cost) and downloads the nearest groups.
3. **Builds you a search page**, one HTML file, served locally. Pre-ranked by similarity, filtered to where you can actually work (say "Remote, US" and onsite and hybrid roles disappear), with facets for seniority, salary (stated, or estimated from a model trained on ~525k postings with pay listed), company, and more.
4. **Learns your taste with you as the judge.** Mark a spread of jobs More/Less, then answer "which would you rather have?" about a dozen times. The page picks each pair to learn the most from your answer and tells you which one it predicts you'll choose. Your answers become a vector that re-sorts the whole list. No API calls, no cost.
5. **Watches how you browse.** Every click is a local log. Ask Claude "what do I seem to like?" and it reads the log, tightens the JD, and runs it again.

On my own search, the offer I just accepted came out 4th of ~1,750. I start soon!

## Why I built it

I was job hunting and every board showed me the same 40 postings, ranked by whoever paid. The signal I wanted was simple: given a job I'd love, which of the two million open ones are most like it, and which of those would I actually pick? That's a similarity search plus a taste model, and both are cheap once the data is on your laptop. Nobody sells the data cheap, so I crawled it.

The first version (~960k jobs) got me interviews. Then I lost the crawler in a data-loss event and rebuilt everything from scratch, bigger, with the crawler in the repo this time.

## How Claude Code helped (start to finish)

Every line of this was written with Claude Code over a few weeks of evenings, including the crawler, the clustering, the search page, and this post. The parts that I think are worth stealing:

- **One Durable Object per job board.** ~65k Cloudflare Durable Objects, each owning one company's board: it wakes at a fixed random minute each day, fetches, diffs against yesterday, pulls full descriptions for new jobs, embeds them. No central queue, nothing to babysit. Claude wrote 25 ATS fetchers (Greenhouse, Lever, Workday, Ashby, SmartRecruiters...) mostly by me pasting an example URL and saying "figure out the API."
- **Embeddings are nearly free.** Two million postings through text-embedding-3-small cost about the price of a nice dinner. That's what makes "pre-rank the whole corpus against your ideal JD" possible with zero LLM calls at search time.
- **Cluster once, download by cluster.** The corpus is split by recursive 2-means into a few thousand groups with centroids. Your JD's embedding picks the nearest groups, and that's all you download. No server-side search, no accounts, nothing of yours stored anywhere.
- **Pairwise beats scoring.** Asking "which of these two would you rather have?" a dozen times gives a far better ranking than asking anyone, human or model, to score jobs 1 to 10. The answers train a tiny model locally (Bradley-Terry over embedding differences), and the pairs are chosen to be maximally informative.
- **Make the agent the UI.** I stopped building settings screens. Every step is a small Python script, and `AGENTS.md` tells Claude the code is meant to be changed. Want a filter that doesn't exist, a different ranking rule, "no staffing agencies"? Say so and Claude edits the tool. Several features this week started as me pasting a complaint into the chat ("this job says onsite, why is it in my remote list?") and Claude fixing the parser.

What I'd tell someone a step behind me: don't build the product, build the dataset and the tools, then let the agent be the product. And write the `AGENTS.md` like you're onboarding a sharp new coworker, because that's exactly what it is.

No business model, free to try and free to keep. I just wanted this to exist.

repo: https://github.com/elliottdehn/open-jobs

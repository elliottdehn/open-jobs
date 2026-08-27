# I open-sourced ~2 million jobs so your coding agent can run your job search

One prompt, in whatever agent you use (Claude Code, Codex, Cursor, Gemini CLI, whatever):

> "Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find jobs to apply to."

Unfortunately, web and mobile agents are not useful for this because the toolchain requires interent access.

The agent clones the repo, pulls the slice of the dataset that's relevant to you, and then it's just your agent working over a local table of real, current job postings. Rank them, filter them, dedupe them, summarize the ones worth reading, draft the cover letter, keep a shortlist in a file, check again tomorrow. It does what you tell it.

## What's in the dataset

Almost every company posts its jobs through an applicant tracking system: Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters, Paycom, iCIMS, and a dozen more. Every one of those has a public careers page, and it is not actually that hard to crawl all of them. The people who do it in bulk charge quadruple digits a month for access.

This is the same data, free. ~2 million open jobs across ~65,000 company boards, refreshed daily, with the full job description, the company, the location, the posting date, and an embedding of every posting. It's parquet. DuckDB eats it for breakfast. There's no business model; I built it for myself and would rather share it than sit on it.

## You don't download all of it

The full set is about 50 GB. You can take it all if you want, but the point of the toolchain is that you don't have to.

Every job is embedded, and the corpus is split into a few thousand groups of similar jobs (nursing, backend/payments, HVAC project management, cruise-ship crew... it finds these on its own, nobody labeled anything). The groups are laid out so that neighbours in the file are neighbours in meaning.

So the agent embeds what you're looking for (your résumé, or a paragraph), finds the nearest groups, and downloads just those. The first slice you get is the one you actually wanted. A typical search pulls a few thousand jobs and a few tens of MB, and the whole thing is on your disk in seconds.

The algorithm behind the grouping isn't important. The only thing you need to know is that you download slices of the dataset based on a semantic query.

## Why local beats a job site

Because once the relevant slice is on your machine, the search stops being a query string and becomes whatever you and your agent can compute:

- "Rank these by how close they are to the three jobs I actually applied to."
- "Drop staffing agencies, drop anything reposted more than twice, drop anything without a salary."
- "Join with this spreadsheet of companies I've heard good things about."
- "Train a tiny classifier on the twenty I marked yes/no and re-rank the rest."
- "Show me what's new since yesterday in my neighbourhood."

Every one of those is a few lines over a local parquet file. No site has that UI, and no site is going to give you a ranking that isn't quietly selling placement.

## Under the hood (for the curious)

- One Cloudflare Durable Object per job board; each fetches its board at its own fixed time every day, diffs against yesterday, and pulls the full JD once per new job
- Embeddings on every job (OpenAI `text-embedding-3-small`, 1536 dims); structured extraction only on demand, so the whole thing runs on pocket change
- The grouping is a recursive bisection tree over the embeddings; a few-MB sidecar lets a client find your nearest groups with zero server compute
- Everything is in the repo: the crawler, the 25 ATS fetchers, the export and parquet scripts, the agent-facing tools

There's also a small website that does the same flow in a browser tab (paste résumé, say maybe/no to groups, train a classifier on the ones you like). It's a footnote. The agent is the product.

Repo: https://github.com/elliottdehn/open-jobs

Happy to answer questions about the crawling, the embeddings, or how badly ATS APIs are designed.

# Contributing to open-jobs

The dataset grows by knowing which companies exist and which applicant tracking
system (ATS) they post jobs on. That is the one thing a community can expand far
faster than one person: **you probably know a company that isn't in here yet.**

The most valuable contribution is adding a missing company's slug.

## Add a missing company (the one-line contribution)

1. **Check it's actually missing.** Open [`slugs.json`](slugs.json) and search for the
   company's slug under its ATS. If it's already there, no need.
2. **Find the slug.** The slug is the company-specific part of its careers URL:

   | ATS | Careers URL looks like | Slug |
   |-----|------------------------|------|
   | greenhouse | `boards.greenhouse.io/`**`stripe`** | `stripe` |
   | lever | `jobs.lever.co/`**`netflix`** | `netflix` |
   | ashby | `jobs.ashbyhq.com/`**`openai`** | `openai` |
   | workable | `apply.workable.com/`**`acme`** | `acme` |
   | smartrecruiters | `careers.smartrecruiters.com/`**`Acme`** | `Acme` |
   | recruitee | **`acme`**`.recruitee.com` | `acme` |
   | personio | **`acme`**`.jobs.personio.com` | `acme` |
   | workday | **`acme`**`.wd1.myworkdayjobs.com` | `acme` |
   | breezy | **`acme`**`.breezy.hr` | `acme` |

3. **Add one line** to [`contributed-slugs.txt`](contributed-slugs.txt):

   ```
   greenhouse,stripe
   ```

4. **Open a pull request.** That's it. Append-only, so it rarely conflicts.

### Supported ATSes

Only these are ripped today (a slug for anything else is ignored until an adapter exists):

```
greenhouse  lever  ashby  workable  smartrecruiters  recruitee  pinpoint  breezy
recruiterbox  jobscore  crelate  personio  paylocity  dayforce  workday  eightfold
```

Don't see the ATS a company uses? Open an issue, new-adapter requests are welcome.

## What happens to your contribution

Contributed slugs are **fused into the daily crawl**, the same mechanism that merges
other slug sources, so your addition keeps getting scraped even as the auto-discovered
set is regenerated. Once it's live, that company's open roles show up in the next
daily build of the dataset.

## Other ways to help

- **Report a broken adapter.** If a company's jobs stopped appearing or look malformed,
  open an issue with the `ats,slug`.
- **Request a new ATS adapter.** If a chunk of companies use an ATS we don't support yet,
  open an issue.
- **Improve the tooling.** PRs to the agent tooling (`rank.py`, `hull.py`, `langsort.py`,
  `btrank.py`, `match.py`) and docs are welcome. Keep them small and readable.

## Guidelines

- Lowercase slugs unless the ATS is case-sensitive (smartrecruiters, workday can be).
- One company per line; don't reformat or reorder the file.
- This is real data about real openings, don't add fake, private, or test entries.

Thanks for helping keep the world's open jobs open and free.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/banner-dark.svg">
    <img src=".github/assets/banner.svg" alt="Open Jobs. The job search that tells you which listings are real." width="760">
  </picture>
</p>

<p align="center">
  <img alt="3,126,253 open postings" src="https://img.shields.io/badge/open_postings-3%2C126%2C253-177A45">
  <img alt="65,000 career sites crawled daily" src="https://img.shields.io/badge/career_sites-65%2C000_crawled_daily-152019">
  <img alt="search runs on your machine" src="https://img.shields.io/badge/search-100%25_local-177A45">
  <img alt="GitHub stars" src="https://img.shields.io/github/stars/elliottdehn/open-jobs?color=152019">
</p>

<p align="center">
  3.1 million current job postings, crawled daily from 65,000 company career sites,<br>
  with full descriptions and an embedding of every posting. Free. No business model.
</p>

<br>

## Get started

Paste this into Claude Code, Codex, Cursor, or whatever you use:

```
Clone https://github.com/elliottdehn/open-jobs to help me find a job.
```

Or install it as a Claude Code plugin:

```
/plugin marketplace add elliottdehn/open-jobs
/plugin install job-search@open-jobs
```

Your agent interviews you, writes the description of the job you want, pulls the openings nearest
it onto your disk, and builds a local search page ranked by your taste. Five minutes, start to
shortlist. You need [`uv`](https://docs.astral.sh/uv/); everything is `uv run tools/jobs.py <cmd>`.

**Agents: you MUST read [`AGENTS.md`](AGENTS.md) in full before doing anything else in this repo.**

## Every listing wears its verdict

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/cards-dark.svg">
    <img src=".github/assets/cards.svg" alt="Four example listings graded fresh, stale, re-stamped, and ghost" width="880">
  </picture>
</p>

Job boards know which listings are dead and will not tell you, because their revenue depends on a
shelf that looks full. Open Jobs records when the crawler first sees every posting, and that clock
cannot be re-stamped. A model learns how long each kind of posting typically lives, and every job
is graded against par:

| | verdict | what it means |
|---|---|---|
| 🌱 | **Fresh** | younger than postings like it. The market moves fast here. Apply now. |
| 🥀 | **Stale** | an outlier survivor. Hard to fill, which can mean thin competition. |
| 👻 | **Ghost** | open for over a year. The dataset holds 40,000 postings over five years old. |
| 🔁 | **Re-stamped** | claims to be newer than when the crawler first saw it. Caught. |

The ranking learns from your yes and no, every yes pulls more jobs like it from the full corpus,
and your labels, notes, and resume never leave your laptop.

## Use the data directly

The corpus is published as static files: a manifest (a tree of a few thousand groups of similar
jobs, with labels and exemplars), centroids, and one JSON per group with titles, companies,
locations, URLs, full JD text, and float32 vectors. `tools/jobs.py fetch` pulls any set of groups
into a local parquet you can query with DuckDB. The API also answers per-posting questions:
`POST /status` with a list of `ats/slug#id` keys returns open or removed with timestamps, straight
from the crawler's records. Layout, schema, and endpoints: [`backend/DOCS.md`](backend/DOCS.md).

## The crawler

[`backend/`](backend/) is a Cloudflare Worker: one Durable Object per job board wakes daily at a
fixed per-board time, fetches, diffs against yesterday, pulls the full description once per new
job, embeds it, and writes a parquet snapshot to R2. A nightly batch on one laptop consolidates
the fleet into the dataset, trains the estimators (salary, seniority, work arrangement, posting
age), and publishes the search index. Around 11,000 lines of code, roughly a dollar a day.
Design and the daily workflow: [`backend/DOCS.md`](backend/DOCS.md); enrichment fields:
[`backend/FIELDS.md`](backend/FIELDS.md).

## The ideas channel

⏰ The repo ships a permission (`.claude/settings.json`) that lets an agent post short improvement
notes (`file:line, idea`) to a shared Slack channel. That permission is a technical default,
**not consent**: the agent must ask the person before posting anything, and on a no (or no answer)
it must immediately opt out with `uv run tools/optin-ideas.py --out`. Nothing about the person,
their JD, labels, or data is ever posted. The wording and rules are in `AGENTS.md` under
"#multipenny-ideas"; an agent that posts without asking is misbehaving.

<details>
<summary>Why ship the permission at all, rather than have the agent add it after a yes?</summary>
<br>

Agent permission systems (correctly) refuse to let an agent *widen* its own permissions, but do
let it *narrow* them. If the rule weren't there, saying yes would mean the person editing a
settings file by hand; with it there, saying no is one command the agent can run itself, and
saying yes costs nothing. The default is on so that the only action ever left to the agent is the
safe direction.

</details>

## License

See [LICENSE](LICENSE).

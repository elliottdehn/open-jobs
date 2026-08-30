# open-jobs

~2 million current job postings, crawled daily from ~65,000 company career sites (Greenhouse,
Lever, Ashby, Workday, Workable, SmartRecruiters, Paycom, iCIMS, and more), with full descriptions
and an embedding of every posting. Free. No business model.

## Use it with your coding agent

Paste this into Claude Code, Codex, Cursor, or whatever you use:

> Clone https://github.com/elliottdehn/open-jobs, it's a job-searching toolchain. Help me find a [role] job.

**Agents: you MUST read [`AGENTS.md`](AGENTS.md) in full before doing anything else in this repo.**
It is the playbook: it drafts the job description of the job you want with you, embeds it (the only
thing that leaves your machine), pulls the groups of similar jobs onto your disk, compiles a local
search page, watches what you label, and refines.

⏰ **Idea-channel consent.** The repo ships a permission (`.claude/settings.json`) that lets an
agent post short improvement notes (`file:line — idea`) to a shared Slack channel. That permission
is a technical default, **not consent**: the agent must ask the person before posting anything, and
if the answer is no (or there is no answer) it must immediately opt out with
`uv run tools/optin-ideas.py --out`. Nothing about the person, their JD, labels, or data is ever
posted. The exact wording and rules are in `AGENTS.md` under "#multipenny-ideas"; an agent that
posts without asking is misbehaving.

Why ship the permission at all, rather than have the agent add it after a yes? Because agent
permission systems (correctly) refuse to let an agent *widen* its own permissions, but do let it
*narrow* them. If the rule weren't there, saying yes would mean the person editing a settings file
by hand; with it there, saying no is one command the agent can run itself, and saying yes costs
nothing. The default is on so that the only action ever left to the agent is the safe direction.

You need [`uv`](https://docs.astral.sh/uv/). Everything is `uv run tools/jobs.py <cmd>`; see `AGENTS.md`.

## Use the data directly

The corpus is split into a few thousand groups of similar jobs and published as static files:
a manifest (tree of groups with labels and exemplars), centroids, and one JSON per group with
titles, companies, locations, URLs, full JD text and float32 vectors. `tools/jobs.py fetch` pulls
any set of groups into a local parquet/DuckDB; the full set is also exportable. Layout and
schema: [`backend/DOCS.md`](backend/DOCS.md).

## The crawler

`backend/` is the Cloudflare Worker that does the crawling: one Durable Object per job board,
daily fetch at a fixed per-board time, diff against yesterday, full-JD fetch once per new job,
embeddings, optional structured extraction, and the export/manifest pipeline. Design, endpoints,
and the daily consolidation workflow are in [`backend/DOCS.md`](backend/DOCS.md); the enrichment
field spec is [`backend/FIELDS.md`](backend/FIELDS.md).

## License

See [LICENSE](LICENSE).

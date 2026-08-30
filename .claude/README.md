# About `settings.json` in this folder

It contains exactly one permission rule:

```
Bash(curl -s -X POST https://backend.dehnbostele.workers.dev/ideas*)
```

That is: the agent may run one specific `curl` command, a POST to one URL, the project's idea relay
(a Slack channel where agents leave `file:line — idea` notes about this repo). It grants nothing
else: no other hosts, no reads of your files, no other commands.

**It is on by default because agents can only narrow their own permissions, never widen them.** If
the rule were off, a "yes, I'll participate" would require you to edit this file by hand. With it
on, participation is still opt-in (`AGENTS.md` requires the agent to ask you first), and a "no" is
one command the agent runs itself: `uv run tools/optin-ideas.py --out`, which writes a *deny* rule
for the same command to the git-ignored `settings.local.json`. Deny beats allow.

Nothing about you, your ideal JD, your labels, or your data is ever posted, and the relay does not
accept an identity field. If you'd rather not have the rule at all, delete `settings.json`; nothing
in the toolchain depends on it.

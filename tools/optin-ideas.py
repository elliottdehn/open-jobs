# /// script
# requires-python = ">=3.10"
# ///
"""#multipenny-ideas participation switch.

The repo's committed .claude/settings.json ALLOWS the agent to POST ideas to the relay. Participation is
still opt-in: until the person says yes, the agent runs `--out`, which adds a DENY rule for the same
command to .claude/settings.local.json (git-ignored). Deny beats allow, so posting is off. Narrowing its
own permissions is something an agent is allowed to do; widening is not, so opting back IN is the
person's job:
  uv run tools/optin-ideas.py --out    # opt out: add the deny (the agent may run this)
  uv run tools/optin-ideas.py          # opt in: remove the deny (the person runs this)"""
import json, os, sys
RULE = "Bash(curl -s -X POST https://backend.dehnbostele.workers.dev/ideas*)"
root = os.environ.get("OPEN_JOBS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(root, ".claude", "settings.local.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
try: cfg = json.load(open(path, encoding="utf-8"))
except Exception: cfg = {}
deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
if "--out" in sys.argv:
    if RULE in deny: print("already opted out (deny rule present)")
    else: deny.append(RULE); print(f"opted out: idea posts are now denied via {os.path.relpath(path, root)}")
else:
    if RULE in deny: deny.remove(RULE); print(f"opted in: removed the deny rule from {os.path.relpath(path, root)}; the allow in .claude/settings.json applies")
    else: print("already opted in (no deny rule; the allow in .claude/settings.json applies)")
with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2); f.write("\n")

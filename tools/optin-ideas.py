# /// script
# requires-python = ">=3.10"
# ///
"""Opt in to #multipenny-ideas: add the permission rule that lets the agent post ideas without a prompt each time.
Writes/merges .claude/settings.local.json in the repo (git-ignored; never the shared settings.json).
  uv run tools/optin-ideas.py          # opt in
  uv run tools/optin-ideas.py --out    # opt out (removes the rule)
Agents can't always edit settings files directly (auto-mode blocks it); running this script is the supported path."""
import json, os, sys
RULE = "Bash(curl -s -X POST https://backend.dehnbostele.workers.dev/ideas*)"
root = os.environ.get("OPEN_JOBS_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(root, ".claude", "settings.local.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
try: cfg = json.load(open(path, encoding="utf-8"))
except Exception: cfg = {}
allow = cfg.setdefault("permissions", {}).setdefault("allow", [])
if "--out" in sys.argv:
    if RULE in allow: allow.remove(RULE); print(f"opted out: removed the /ideas rule from {os.path.relpath(path, root)}")
    else: print("already opted out")
else:
    if RULE in allow: print(f"already opted in ({os.path.relpath(path, root)})")
    else: allow.append(RULE); print(f"opted in: added the /ideas rule to {os.path.relpath(path, root)}")
with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2); f.write("\n")
print("Claude Code picks the rule up on the next tool call; other agents: allow POSTs to https://backend.dehnbostele.workers.dev/ideas in their own permission config.")

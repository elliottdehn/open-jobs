"""Step 28 — Integration test: labels written by tools/ are visible in toolsHub/.

Reads work/interactions.jsonl with both the tools/ and toolsHub/ label parsers
and asserts the resulting dicts are identical.

Run from the repo root:
  uv run python toolsHub/tests/test_label_parity.py
"""
import json
import sys
from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

# ── toolsHub reader ────────────────────────────────────────────────────────
from toolsHub.app.services.labels import load_interaction_labels  # noqa: E402

# ── tools/ reader (inline, same logic as tools/jobs.py) ───────────────────
def _tools_load(path: Path) -> dict:
    labels = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "label":
                labels[e["key"]] = e["value"]
    return labels


def main() -> None:
    ipath = ROOT / "work" / "interactions.jsonl"
    if not ipath.exists():
        print(f"SKIP — {ipath} does not exist (no interactions yet)")
        return

    hub_labels  = load_interaction_labels(ipath)
    tool_labels = _tools_load(ipath)

    if hub_labels == tool_labels:
        n = len(hub_labels)
        print(f"PASS — {n} label(s) read identically by both tools/ and toolsHub/")
    else:
        only_hub  = {k for k in hub_labels  if k not in tool_labels or hub_labels[k]  != tool_labels.get(k)}
        only_tool = {k for k in tool_labels if k not in hub_labels  or tool_labels[k] != hub_labels.get(k)}
        print(f"FAIL — divergence detected")
        for k in only_hub:
            print(f"  hub only / mismatch: {k!r}  hub={hub_labels[k]}  tools={tool_labels.get(k)!r}")
        for k in only_tool:
            print(f"  tools only:          {k!r}  tools={tool_labels[k]}")
        sys.exit(1)


if __name__ == "__main__":
    main()

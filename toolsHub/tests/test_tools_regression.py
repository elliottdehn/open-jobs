"""Step 30 — Regression check: tools/ directory is unchanged.

Checks that no files under tools/ were modified by the toolsHub/ build.
Uses git to compare the working tree against HEAD.

Run from the repo root:
  uv run python toolsHub/tests/test_tools_regression.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    # Are we in a git repo?
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("SKIP — not a git repository")
        return

    # Check for any modifications to tools/ (staged or unstaged)
    diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "tools/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    changed = [l.strip() for l in diff.stdout.splitlines() if l.strip()]

    # Also check for untracked files in tools/
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "tools/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    untracked = [l for l in status.stdout.splitlines() if l.startswith("??")]

    if changed or untracked:
        print("FAIL — tools/ has unexpected changes:")
        for f in changed:
            print(f"  modified:  {f}")
        for f in untracked:
            print(f"  {f.strip()}")
        sys.exit(1)
    else:
        print("PASS — tools/ is unchanged (git confirms no modifications or untracked files)")


if __name__ == "__main__":
    main()

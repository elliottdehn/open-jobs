"""toolsHub — FastAPI reimplementation of the open-jobs tools workflow.

Usage:
  uv run toolsHub/main.py serve   [--port 8766] [--no-open]
  uv run toolsHub/main.py embed   --file work/ideal-jd.md --title "..." [--location "..."]
  uv run toolsHub/main.py groups  [--k 30] [--min-sim 0]
  uv run toolsHub/main.py fetch   --groups 12,45,301 | --top N [--replace]
  uv run toolsHub/main.py html    [--out work/search.html]
  uv run toolsHub/main.py enrich  [--top N | --all]
  uv run toolsHub/main.py rank    [--labels work/interactions.jsonl]
  uv run toolsHub/main.py top     [--n 50] [--notes work/top-notes.json]
  uv run toolsHub/main.py probe   <url>
  uv run toolsHub/main.py status
  uv run toolsHub/main.py export  [--date YYYY-MM-DD] [--only ats1,ats2] [--out DIR]
  uv run toolsHub/main.py sql     "SELECT ..."
"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog="toolsHub")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve").add_argument("--port", type=int, default=8766)
    sub.parse_known_args()  # placeholder until subcommands are wired

    args = ap.parse_args()
    print(f"toolsHub: command '{args.cmd}' not yet implemented", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()

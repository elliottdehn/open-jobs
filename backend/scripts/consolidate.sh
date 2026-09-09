#!/usr/bin/env bash
# Daily consolidation on the laptop: a thin wrapper that runs scripts/stage.py stages in order. Each stage is
# idempotent and re-runnable on its own (`uv run scripts/stage.py <stage> --date <date>`); this file only
# sequences them and keeps the log. Writes export/<YYYY-MM-DD>/ and repoints export/latest; history lives in
# export/diffs/ and export/ledger/ (see DOCS.md "Layout and retention"); older full exports are pruned once
# today's diff verifies.
#
# Usage: scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload] [--skip-models] [--skip-ledger] [--keep-full]
#        [--source r2]   read snapshots and the previous export from the bucket in place (the container layout);
#                        parquet goes to exports/<date>/ in R2 and only scratch stays local (work-<date>/)
#        [--from STAGE]  resume from a stage (ingest pull ledger parquet diff tree estimators finalize history feed mirror retention)
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${WORKER_URL:-https://backend.dehnbostele.workers.dev}"
[[ $# -gt 0 && "$1" != --* ]] && { BASE="$1"; shift; }
[ -z "${ADMIN_TOKEN:-}" ] && [ -f admin_token.txt ] && export ADMIN_TOKEN="$(tr -d '[:space:]' < admin_token.txt)"
[ -z "${ADMIN_TOKEN:-}" ] && echo "WARNING: no ADMIN_TOKEN (env or backend/admin_token.txt); the pull will get 401s"
DATE=$(date +%Y-%m-%d)
SOURCE=local; FROM=""; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2;;
    --from) FROM="$2"; shift 2;;
    --skip-models|--keep-full) EXTRA+=("$1"); shift;;
    *) EXTRA+=("$1"); shift;;
  esac
done
has() { [[ " ${EXTRA[*]:-} " == *" $1 "* ]]; }
mkdir -p logs export
LOG="logs/consolidate-$DATE.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== consolidate $DATE (source $SOURCE, worker $BASE) $(date '+%H:%M:%S')"

STAGES=(ingest pull ledger parquet diff tree estimators finalize history feed mirror retention)
N=${#STAGES[@]}; i=0; started=0
for st in "${STAGES[@]}"; do
  i=$((i+1))
  if [ -n "$FROM" ] && [ $started = 0 ]; then [ "$st" = "$FROM" ] && started=1 || continue; fi
  case "$st" in
    ingest)   has --skip-ingest && { echo "--- $i/$N ingest skipped"; continue; };;
    ledger)   has --skip-ledger && { echo "--- $i/$N ledger skipped"; continue; };;
    finalize|history|feed|mirror) has --skip-upload && { echo "--- $i/$N $st skipped (--skip-upload)"; continue; };;
  esac
  echo "--- $i/$N $st $(date '+%H:%M:%S')"
  args=(--date "$DATE" --source "$SOURCE" --worker "$BASE")
  [ "$st" = estimators ] && has --skip-models && args+=(--skip-models)
  [ "$st" = retention ] && has --keep-full && args+=(--keep-full)
  if ! uv run scripts/stage.py "$st" "${args[@]}"; then
    case "$st" in
      ledger) echo "WARNING: ledger failed; continuing (posting-lifetime history has a gap for $DATE)";;
      diff)   echo "WARNING: diff failed; older full exports will be kept";;
      history) echo "WARNING: history upload incomplete; re-run: uv run scripts/stage.py history --date $DATE";;
      feed)   echo "WARNING: feed not published; re-run: uv run scripts/stage.py feed --date $DATE (see JOB-CHANGES.md)";;
      mirror) echo "WARNING: Hugging Face mirror not updated; re-run: uv run scripts/stage.py mirror --date $DATE";;
      *) echo "FAILED at stage $st; fix and resume with: scripts/consolidate.sh $BASE --from $st ${EXTRA[*]:-}"; exit 1;;
    esac
  fi
done
echo "=== done $(date '+%H:%M:%S')"

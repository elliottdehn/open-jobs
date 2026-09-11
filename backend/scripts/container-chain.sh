#!/usr/bin/env bash
# The nightly chain inside ONE container (Cloudflare Containers). /work lives only as long as the container does, so
# every stage of a run executes in this single process; same order and same warning-only stages as container-run.sh.
#   container-chain.sh <all | from-stage> <date>
set -o pipefail
FROM="${1:-all}"; DATE="${2:-$(date -u +%Y-%m-%d)}"
ALL=(pull ledger parquet diff tree estimators finalize history feed archive retention)
if [ "$FROM" != all ]; then
  idx=-1; for i in "${!ALL[@]}"; do [ "${ALL[$i]}" = "$FROM" ] && idx=$i; done
  [ "$idx" -ge 0 ] || { echo "unknown stage: $FROM (one of ${ALL[*]})"; exit 2; }
  STAGES=("${ALL[@]:$idx}")
else STAGES=("${ALL[@]}"); fi
run() { echo "--- $1 $(date -u '+%H:%M:%S')"; uv run --script /app/scripts/stage.py "$1" --date "$DATE" --source r2; }
echo "=== container consolidation $DATE from ${STAGES[0]} (cloud) $(date -u '+%H:%M:%S')"
for st in "${STAGES[@]}"; do
  if ! run "$st"; then
    case "$st" in ledger|history|feed|archive) echo "WARNING: $st failed; continuing";; *) echo "FAILED at $st"; run report || true; exit 1;; esac
  fi
done
run report || true; echo "=== done $(date -u '+%H:%M:%S')"

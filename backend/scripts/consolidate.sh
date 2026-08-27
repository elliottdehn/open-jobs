#!/usr/bin/env bash
# Daily consolidation: ingest local-only ATSes -> pull everything (JDs + vectors) -> parquet -> manifest -> R2.
# Never deletes previous exports: each run writes export/<YYYY-MM-DD>/ and repoints export/latest.
# Logs stream (unbuffered) to logs/consolidate-<date>.log. Re-runnable: finished ATSes are skipped via .done markers.
#
# Usage: scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload]
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-${WORKER_URL:-https://backend.dehnbostele.workers.dev}}"; [ $# -gt 0 ] && shift || true
DATE=$(date +%Y-%m-%d)
OUT="export/$DATE"
mkdir -p logs "$OUT"
LOG="logs/consolidate-$DATE.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== consolidate $DATE -> $OUT (worker $BASE) $(date '+%H:%M:%S')"

if [[ " $* " != *" --skip-ingest "* ]]; then
  echo "--- 1/5 ingest local-only ATSes (jobscore) $(date '+%H:%M:%S')"
  node --experimental-strip-types scripts/fetch-local.mjs --ingest="$BASE" 2>&1 | grep -v -i "warning\|^(Use\|Reparsing\|To eliminate" || true
fi

echo "--- 2/5 pull every ATS with JDs + vectors, all at once, resumable $(date '+%H:%M:%S')"
NODE_OPTIONS=--max-old-space-size=16384 python3 -u scripts/pull-pool.py --base "$BASE" --out "$OUT" -- --status=open --embed --resume

echo "--- 3/5 parquet $(date '+%H:%M:%S')"
EXPORT_DIR="$OUT" uv run scripts/build-parquet.py

echo "--- 4/5 manifest (tree + centroids + group files) $(date '+%H:%M:%S')"
EXPORT_DIR="$OUT" uv run scripts/build-manifest.py

ln -sfn "$DATE" export/latest
echo "export/latest -> $DATE"

if [[ " $* " != *" --skip-upload "* ]]; then
  echo "--- 5/5 upload manifest + groups to R2 $(date '+%H:%M:%S')"
  EXPORT_DIR="$OUT" python3 -u scripts/upload-web.py --workers 16
fi
echo "=== done $(date '+%H:%M:%S')"

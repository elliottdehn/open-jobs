#!/usr/bin/env bash
# Daily consolidation: ingest local-only ATSes -> pull everything (JDs + vectors) -> parquet -> manifest -> R2.
# Never deletes previous exports: each run writes export/<YYYY-MM-DD>/ and repoints export/latest.
# Logs stream (unbuffered) to logs/consolidate-<date>.log. Re-runnable: finished ATSes are skipped via .done markers.
#
# Usage: scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload] [--skip-models]
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-${WORKER_URL:-https://backend.dehnbostele.workers.dev}}"; [ $# -gt 0 ] && shift || true
# admin token for the Worker (export/ingest endpoints): env ADMIN_TOKEN, else admin_token.txt (git-ignored)
[ -z "${ADMIN_TOKEN:-}" ] && [ -f admin_token.txt ] && export ADMIN_TOKEN="$(tr -d '[:space:]' < admin_token.txt)"
[ -z "${ADMIN_TOKEN:-}" ] && echo "WARNING: no ADMIN_TOKEN (env or backend/admin_token.txt); the pull will get 401s"
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

echo "--- 2/5 pull: R2 board snapshots, then /export for whatever lacks them $(date '+%H:%M:%S')"
# Primary: static per-board parquet snapshots from R2 (written by the Board DOs; no DO wakes, ~5x
# smaller than the JSON export). Local-only ATSes are excluded: step 1 just ingested them fresh, and
# their snapshots lag until embeds drain — the /export fallback pulls them same-run instead.
node scripts/pull-snapshots.mjs "$BASE" --out="$OUT" --exclude=jobscore,governmentjobs \
  || echo "WARNING: snapshot pull failed; the /export fallback below will cover everything"
# Fallback: ATSes with no snapshots yet (fleet still backfilling after a deploy) + the local-only ones.
FALLBACK=$(OUT="$OUT" python3 - <<'PYEOF'
import glob, json, os
out = os.environ["OUT"]
boards = json.load(open("src/boards.json"))
missing = sorted(a for a in boards if not glob.glob(os.path.join(out, "snapshots", a, "*.parquet")))
print(" ".join(missing))
PYEOF
)
if [ -n "$FALLBACK" ]; then
  echo "  /export fallback for: $FALLBACK"
  NODE_OPTIONS=--max-old-space-size=16384 python3 -u scripts/pull-pool.py --base "$BASE" --out "$OUT" --ats "$FALLBACK" -- --status=open --embed --resume
else
  echo "  all ATSes covered by snapshots"
fi

echo "--- 3/5 parquet $(date '+%H:%M:%S')"
EXPORT_DIR="$OUT" uv run scripts/build-parquet.py

echo "--- 4/5 manifest (tree + centroids + group files) $(date '+%H:%M:%S')"
EXPORT_DIR="$OUT" uv run scripts/build-manifest.py

if [[ " $* " != *" --skip-models "* ]]; then
  echo "--- 4b/5 estimators: salary, work arrangement, seniority, country table $(date '+%H:%M:%S')"
  EXPORT_DIR="$OUT" uv run scripts/train-salary.py 2>&1 | grep -v Warning
  EXPORT_DIR="$OUT" uv run scripts/train-arrangement.py 2>&1 | grep -v Warning
  EXPORT_DIR="$OUT" uv run scripts/train-seniority.py 2>&1 | grep -v Warning
  EXPORT_DIR="$OUT" uv run scripts/train-age.py 2>&1 | grep -v Warning
  EXPORT_DIR="$OUT" uv run scripts/build-city-table.py
  EXPORT_DIR="$OUT" uv run scripts/build-location-table.py 2>&1 | grep -v Warning   # embeds only new location strings (pennies)
else
  echo "--- 4b/5 estimators skipped (--skip-models); carrying yesterday's model files forward"
  mkdir -p "$OUT/web"; for f in salary-model.json arrangement-model.json seniority-model.json age-model.json location-countries.json; do
    [ -f "export/latest/web/$f" ] && [ ! -f "$OUT/web/$f" ] && cp "export/latest/web/$f" "$OUT/web/$f" || true; done
fi

ln -sfn "$DATE" export/latest
echo "export/latest -> $DATE"

if [[ " $* " != *" --skip-upload "* ]]; then
  echo "--- 5/5 upload manifest + groups to R2 $(date '+%H:%M:%S')"
  EXPORT_DIR="$OUT" python3 -u scripts/upload-web.py --workers 16
fi
echo "=== done $(date '+%H:%M:%S')"

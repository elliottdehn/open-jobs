#!/usr/bin/env bash
# Daily consolidation: ingest local-only ATSes -> pull everything (JDs + vectors) -> ledger -> parquet -> diff
# -> manifest -> R2 -> retention. Each run writes export/<YYYY-MM-DD>/ and repoints export/latest.
# History is kept as diffs, not snapshots: export/diffs/<prev>__<date>.parquet (lossless, see build-diff.py)
# and export/ledger/<date>.parquet (every job ever seen, with first_seen/removed_at). Once today's diff exists
# and passes its sanity check, older full exports are deleted (keep them with --keep-full).
# Logs stream (unbuffered) to logs/consolidate-<date>.log. Re-runnable: finished ATSes are skipped via .done markers.
#
# Usage: scripts/consolidate.sh [worker-url] [--skip-ingest] [--skip-upload] [--skip-models] [--skip-ledger] [--keep-full]
#   --skip-upload also skips 5b (publishing diffs + ledger); run scripts/upload-history.py by hand for that.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-${WORKER_URL:-https://backend.dehnbostele.workers.dev}}"; [ $# -gt 0 ] && shift || true
# admin token for the Worker (export/ingest endpoints): env ADMIN_TOKEN, else admin_token.txt (git-ignored)
[ -z "${ADMIN_TOKEN:-}" ] && [ -f admin_token.txt ] && export ADMIN_TOKEN="$(tr -d '[:space:]' < admin_token.txt)"
[ -z "${ADMIN_TOKEN:-}" ] && echo "WARNING: no ADMIN_TOKEN (env or backend/admin_token.txt); the pull will get 401s"
DATE=$(date +%Y-%m-%d)
OUT="export/$DATE"
PREV="$(readlink export/latest 2>/dev/null || true)"   # the previous full export: today's diff is against it
[ "$PREV" = "$DATE" ] && PREV=""                        # re-run on the same day: nothing to diff against
DIFF_OK=0
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

if [[ " $* " != *" --skip-ledger "* ]]; then
  echo "--- 2b/5 ledger: slim export of every job, open or removed, with its dates $(date '+%H:%M:%S')"
  mkdir -p "$OUT/ledger-raw"
  { python3 -u scripts/pull-pool.py --base "$BASE" --out "$OUT/ledger-raw" -- --status=all --slim --resume \
      && EXPORT_DIR="$OUT" uv run scripts/build-ledger.py; } \
    || echo "WARNING: ledger pull/build failed; continuing (posting-lifetime history has a gap for $DATE)"
fi

echo "--- 3/5 parquet $(date '+%H:%M:%S')"
EXPORT_DIR="$OUT" uv run scripts/build-parquet.py

echo "--- 3b/5 diff against the previous export (${PREV:-none}) $(date '+%H:%M:%S')"
if [ -n "$PREV" ] && ls "export/$PREV"/jobs/*.parquet >/dev/null 2>&1; then
  if EXPORT_DIR="$OUT" uv run scripts/build-diff.py --prev "export/$PREV" --base "$BASE"; then DIFF_OK=1; else echo "WARNING: diff failed; older full exports will be kept"; fi
else
  echo "  no previous full export; nothing to diff (first run under the diff scheme, or a same-day re-run)"
fi

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
  echo "--- 5b/5 publish history: diffs + ledger to R2 (/data/diffs/*, /data/ledger/*) $(date '+%H:%M:%S')"
  python3 -u scripts/upload-history.py || echo "WARNING: history upload incomplete; re-run scripts/upload-history.py to retry"
fi
if [[ " $* " != *" --keep-full "* ]]; then
  echo "--- 6/6 retention: keep export/$DATE (latest), export/diffs, export/ledger; drop older full exports $(date '+%H:%M:%S')"
  # A full export may go only when (a) today's diff against it exists and passed its sanity check, so nothing
  # is lost, and (b) it is older than today. List first, then delete: never an unverified rm.
  if [ "$DIFF_OK" = 1 ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('export/diffs/${PREV}__${DATE}.json'))['ok_to_prune'] else 1)"; then
    for d in export/20*/; do
      d="${d%/}"; b="$(basename "$d")"
      [ "$b" \< "$DATE" ] || continue
      ls -d export/diffs/"${b}"__*/ >/dev/null 2>&1 || { echo "  keep $d: no diff was ever taken from it"; continue; }
      echo "  delete $d ($(du -sh "$d" | cut -f1); its successor diff exists)"; rm -rf "$d"
    done
  else
    echo "  skipped: today's diff is missing or failed its sanity check; older full exports kept"
  fi
fi
echo "=== done $(date '+%H:%M:%S')"

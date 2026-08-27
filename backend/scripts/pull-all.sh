#!/usr/bin/env bash
# Pull every enabled ATS from the worker into export/<ats>.ndjson, fetch the local-only ATSes
# (providers that block Cloudflare IPs, e.g. jobscore) directly from this machine, then build parquet.
# Usage: scripts/pull-all.sh [worker-url] [export.mjs flags...]   e.g. scripts/pull-all.sh https://x.workers.dev --status=open
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-${WORKER_URL:-https://backend.dehnbostele.workers.dev}}"; shift || true
UA="open-jobs-cli"
AUTH=""; [ -n "${ADMIN_TOKEN:-}" ] && AUTH="authorization: Bearer $ADMIN_TOKEN"
# Local-only ATSes (provider blocks Cloudflare IPs): fetch here, ingest into their Board DOs first so they
# go through the same diff/embed/enrich pipeline, then export them from the Worker like everything else.
echo "fetching local-only ATSes from this machine and ingesting into the worker"
node --experimental-strip-types scripts/fetch-local.mjs --ingest="$BASE" 2>&1 | grep -v -i "warning\|^(Use\|Reparsing\|To eliminate" || true
ATSES=$(curl -sf -A "$UA" -H "$AUTH" "$BASE/ats?all=1" | python3 -c 'import json,sys;print(" ".join(json.load(sys.stdin)))')
echo "pulling: $ATSES"
# 4 ATSes at a time; each export.mjs call streams 200 boards per request. Failures are collected, not fatal.
FLAGS="$*"; export BASE FLAGS
rm -f export/.failed; mkdir -p export
printf '%s\n' $ATSES | xargs -P 4 -I{} bash -c 'node scripts/export.mjs "$1" "$BASE" $FLAGS 2>&1 | grep -v "boards, "; s=${PIPESTATUS[0]}; [ "$s" -eq 0 ] || echo "$1" >> export/.failed; exit 0' _ {}
FAILED=$(cat export/.failed 2>/dev/null | tr '\n' ' ')
uv run scripts/build-parquet.py
[ -n "$FAILED" ] && echo "WARNING: export failed for: $FAILED (re-run: node scripts/export.mjs <ats> $BASE)" && exit 1
echo "pull complete"

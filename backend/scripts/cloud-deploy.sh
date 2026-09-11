#!/usr/bin/env bash
# Deploy the Worker + the consolidation container image, then wait until the container actually runs the new image.
# The image carries scripts/BUILD (written here, git-ignored); the container is asked to `cat` it until it matches.
set -eo pipefail; cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
BUILD="$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"; echo "$BUILD" > scripts/BUILD
echo "build id $BUILD"
npx wrangler deploy 2>&1 | grep -E 'Current Version|Building image|digest|error|Error' || true
T=$(tr -d '[:space:]' < admin_token.txt); W=${WORKER_URL:-https://backend.dehnbostele.workers.dev}
echo "waiting for the container to run image $BUILD"
for i in $(seq 1 40); do
  curl -s -X POST -H "authorization: Bearer $T" -H 'content-type: application/json' "$W/run/exec" -d '{"args":["/bin/cat","/app/scripts/BUILD"]}' >/dev/null || true
  sleep 20
  live=$(curl -s -H "authorization: Bearer $T" "$W/run" | python3 -c "import json,sys; d=json.load(sys.stdin); t=(d.get('lastOutput') or {}).get('text') or ''; print(t.strip().splitlines()[-1] if t.strip() else '')" 2>/dev/null || echo '?')
  echo "  $(date +%T) container reports: ${live:-?}"
  [ "$live" = "$BUILD" ] && { echo "image $BUILD live"; exit 0; }
done
echo "image $BUILD not live after 13 min"; exit 1

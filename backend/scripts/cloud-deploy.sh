#!/usr/bin/env bash
# Deploy the Worker + the consolidation container image, then wait until the container application's rollout is
# complete (polled from the platform, which starts nothing) and verify once that the container runs the new image
# (it carries scripts/BUILD, written here and git-ignored).   `cloud-deploy.sh --wait` skips the deploy.
set -eo pipefail; cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
T=$(tr -d '[:space:]' < admin_token.txt); W=${WORKER_URL:-https://backend.dehnbostele.workers.dev}
if [ "$1" != "--wait" ]; then
  BUILD="$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"; echo "$BUILD" > scripts/BUILD; echo "build id $BUILD"
  npx wrangler deploy 2>&1 | grep -E 'Current Version|Building image|digest|error|Error' || true
else BUILD=$(cat scripts/BUILD); echo "waiting for build $BUILD"; fi
APP=$(npx wrangler containers list 2>/dev/null | grep backend-consolidate | awk -F'│' '{print $2}' | tr -d ' ')
[ -n "$APP" ] || { echo "container app not found"; exit 1; }
for i in $(seq 1 240); do
  s=$(npx wrangler containers info "$APP" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); h=d.get('health',{}).get('instances',{}); print(('rollout' if d.get('active_rollout_id') else 'settled'), 'starting', h.get('starting',0), 'failed', h.get('failed',0), 'healthy', h.get('healthy',0), 'stopped', h.get('stopped',0))" 2>/dev/null || echo "?")
  echo "  $(date +%T) $s"
  case "$s" in settled\ starting\ 0*) break;; esac
  sleep 5
done
curl -s -X POST -H "authorization: Bearer $T" -H 'content-type: application/json' "$W/run/exec" -d '{"args":["/bin/cat","/app/scripts/BUILD"]}' >/dev/null || true
for i in $(seq 1 30); do
  sleep 3
  live=$(curl -s -H "authorization: Bearer $T" "$W/run" | python3 -c "import json,sys; d=json.load(sys.stdin); t=(d.get('lastOutput') or {}).get('text') or ''; print(t.strip().splitlines()[-1] if t.strip() else '')" 2>/dev/null || echo '?')
  [ "$live" = "$BUILD" ] && { echo "image $BUILD live"; exit 0; }
done
echo "container reports '$live', expected $BUILD"; exit 1

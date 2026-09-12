#!/usr/bin/env bash
# Deploy the Worker + the consolidation container image, then wait until the container application's rollout is
# complete (polled from the platform, which starts nothing) and verify once that the container runs the new image
# (it carries scripts/BUILD, written here and git-ignored).   `cloud-deploy.sh --wait` skips the deploy.
set -eo pipefail; cd "$(dirname "$0")/.."
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
T=$(tr -d '[:space:]' < admin_token.txt); W=${WORKER_URL:-https://backend.dehnbostele.workers.dev}
if [ "$1" != "--wait" ]; then
  BUILD="$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"; echo "$BUILD" > scripts/BUILD; echo "build id $BUILD"
  npx wrangler deploy > /tmp/cloud-deploy.out 2>&1; rc=$?
  grep -E 'Current Version|Building image|: digest:' /tmp/cloud-deploy.out || true
  if [ $rc -ne 0 ] || grep -qE 'failed commit|failed to do request|ERROR|error:' /tmp/cloud-deploy.out; then echo "deploy FAILED (wrangler exit $rc):"; grep -iE 'fail|error' /tmp/cloud-deploy.out | tail -5; exit 1; fi
  DIGEST=$(grep -o ': digest: sha256:[0-9a-f]\{64\}' /tmp/cloud-deploy.out | tail -1 | grep -o 'sha256:[0-9a-f]*'); echo "$DIGEST" > scripts/BUILD.digest
else BUILD=$(cat scripts/BUILD); DIGEST=$(cat scripts/BUILD.digest 2>/dev/null); echo "waiting for build $BUILD ($DIGEST)"; fi
APP=$(npx wrangler containers list 2>/dev/null | grep backend-consolidate | awk -F'│' '{print $2}' | tr -d ' ')
[ -n "$APP" ] || { echo "container app not found"; exit 1; }
for i in $(seq 1 240); do
  s=$(npx wrangler containers info "$APP" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); h=d.get('health',{}).get('instances',{}); img=d.get('configuration',{}).get('image',''); print(('rollout' if d.get('active_rollout_id') else 'settled'), ('new-image' if '$DIGEST' and img.endswith('$DIGEST') else 'old-image'), 'starting', h.get('starting',0), 'failed', h.get('failed',0), 'healthy', h.get('healthy',0))" 2>/dev/null || echo "?")
  [ "$s" != "$prev" ] && { echo "  $(date +%T) $s"; prev="$s"; }
  case "$s" in settled\ new-image\ starting\ 0*) break;; esac
  sleep 5
done
# A stopped instance is what picks up the new image: the object keeps its instance alive for hours between commands
# (a batch chain has no requests to count as activity), so stop it now if nothing is running.
busy=$(curl -s -H "authorization: Bearer $T" "$W/run" | python3 -c "import json,sys; print('yes' if json.load(sys.stdin).get('current') else 'no')" 2>/dev/null || echo '?')
if [ "$busy" = "no" ]; then curl -s -X POST -H "authorization: Bearer $T" "$W/run/stop" >/dev/null || true; echo "  stopped the idle instance; giving the platform 75 s to replace it"; sleep 75; else echo "a run is in progress; the new image applies after it ends"; fi
# Verify from inside: one build-id check per attempt. After the exec, wait for an answer NEWER than the exec (a cold
# start takes a minute or two; stopping the instance while it starts just restarts the cold start). Only on a wrong
# answer stop the instance, wait, and try again.
for i in $(seq 1 4); do
  t_exec=$(date +%s)000
  curl -s -X POST -H "authorization: Bearer $T" -H 'content-type: application/json' "$W/run/exec" -d '{"args":["/bin/cat","/app/scripts/BUILD"]}' >/dev/null || true
  live=""
  for j in $(seq 1 18); do
    sleep 10
    live=$(curl -s -H "authorization: Bearer $T" "$W/run" | python3 -c "import json,sys; d=json.load(sys.stdin); lo=d.get('lastOutput') or {}; t=lo.get('text') or ''; ls=[l for l in t.strip().splitlines() if l.strip() and not l.startswith('[host')]; print(ls[-1] if ls and lo.get('t',0) > $t_exec else '')" 2>/dev/null || echo '')
    [ -n "$live" ] && break
  done
  [ "$live" = "$BUILD" ] && { echo "image $BUILD live"; exit 0; }
  echo "  $(date +%T) container reports '${live:-no answer in 3 min}'; stopping and waiting 60 s"
  curl -s -X POST -H "authorization: Bearer $T" "$W/run/stop" >/dev/null || true; sleep 60
done
echo "container reports '$live', expected $BUILD"; exit 1

#!/usr/bin/env bash
# Run the consolidation image locally, the way Cloudflare will run it: one container per stage, scratch on a
# named volume, secrets from backend/.dev.vars (+ admin_token.txt), reading and writing the real bucket.
#
#   scripts/container-run.sh build                    # build the image for this machine's architecture
#   scripts/container-run.sh all [--date D]           # pull .. report, in order (the nightly run)
#   scripts/container-run.sh <stage> [--date D] ...   # one stage, e.g. parquet --only jazzhr
#   scripts/container-run.sh shell                    # a shell inside the image, same mounts and env
#
# Ingest is not a container stage (those providers block Cloudflare): run `uv run scripts/stage.py ingest` on
# the laptop whenever. Docker Desktop needs >= 14 GB of memory for the tree stage (Settings > Resources).
set -eo pipefail   # not -u: the macOS bash 3.2 treats an empty array as unbound
cd "$(dirname "$0")/.."
DOCKER="${DOCKER:-$(command -v docker || echo /Applications/Docker.app/Contents/Resources/bin/docker)}"
IMAGE="${IMAGE:-open-jobs-consolidate}"; VOL="${VOL:-open-jobs-work}"
cmd="${1:-all}"; shift || true
case "$cmd" in
  build) exec "$DOCKER" build -f Dockerfile -t "$IMAGE" .. ;;                                   # cwd is backend/; context is the repo root
  build-amd64) exec "$DOCKER" build -f Dockerfile --platform linux/amd64 -t "$IMAGE:amd64" .. ;;
esac
[ -f .dev.vars ] || { echo "backend/.dev.vars missing (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, OPENAI_KEY)"; exit 1; }
ENVARGS=(--env-file .dev.vars -e "ADMIN_TOKEN=${ADMIN_TOKEN:-$(tr -d '[:space:]' < admin_token.txt)}" -e "WORKER_URL=${WORKER_URL:-https://backend.dehnbostele.workers.dev}")
for v in SLACK_RUN_WEBHOOK GROUPS_PREFIX ROOT_PREFIX DIFF_MEMORY FEED_DUCKDB_MEMORY; do [ -n "${!v:-}" ] && ENVARGS+=(-e "$v=${!v}"); done
DATE="$(date +%Y-%m-%d)"; ARGS=()
while [ $# -gt 0 ]; do case "$1" in --date) DATE="$2"; shift 2;; *) ARGS+=("$1"); shift;; esac; done
mem=$("$DOCKER" info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
[ "$mem" -gt 0 ] && [ "$mem" -lt 14000000000 ] && echo "WARNING: Docker has $((mem / 1073741824)) GiB; the tree stage peaks near 10 GB. Raise it in Docker Desktop > Settings > Resources."
stage() {
  echo "--- $1 $(date '+%H:%M:%S')"
  "$DOCKER" run --rm "${ENVARGS[@]}" -v "$VOL:/work" "$IMAGE" "$1" --date "$DATE" --source r2 "${@:2}"
}
case "$cmd" in
  shell) exec "$DOCKER" run --rm -it "${ENVARGS[@]}" -v "$VOL:/work" --entrypoint bash "$IMAGE" ;;
  all)
    mkdir -p logs; LOG="logs/container-$DATE.log"; exec > >(tee -a "$LOG") 2>&1
    echo "=== container consolidation $DATE $(date '+%H:%M:%S')"
    for st in pull ledger parquet diff tree estimators finalize history feed retention; do
      if ! stage "$st" "${ARGS[@]}"; then
        case "$st" in ledger|diff|history|feed) echo "WARNING: $st failed; continuing";; *) echo "FAILED at $st"; stage report || true; exit 1;; esac
      fi
    done
    stage report || true; echo "=== done $(date '+%H:%M:%S')" ;;
  *) stage "$cmd" "${ARGS[@]}" ;;
esac

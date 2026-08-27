#!/usr/bin/env bash
# Upload export/web/* to the jobscream-data R2 bucket (served by the Worker at /data/*).
set -euo pipefail
cd "$(dirname "$0")/.."
B=jobscream-data
W="${EXPORT_DIR:-export}/web"
# Upload groups first, then centroids, then the manifest last: a client that sees the new manifest
# will find every group file it references.
n=0
for f in "$W"/groups/*.json; do
  npx wrangler r2 object put "$B/groups/$(basename "$f")" --file "$f" --content-type application/json --remote >/dev/null
  n=$((n+1)); printf '\r%d group files uploaded' "$n"
done
echo
npx wrangler r2 object put "$B/centroids.bin" --file "$W/centroids.bin" --content-type application/octet-stream --remote
npx wrangler r2 object put "$B/manifest.json" --file "$W/manifest.json" --content-type application/json --remote
echo "done"

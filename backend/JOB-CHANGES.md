# Public job changes (opt-in publisher)

Consumers that filter jobs locally currently need to download the group corpus again after each
rebuild: leaf IDs and membership change during clustering. This optional feed publishes additions,
edits and dataset removals once per completed snapshot, without embeddings or per-reader Board DO
queries. It uses the existing public `/data/*` route; no new Worker binding or database is needed.

## Publish

After `build-manifest.py` finishes, from `backend/`:

```sh
# First publication: all current jobs become upserts.
python3 scripts/build-job-changes.py --web export/2026-09-07/web \
  --publish-base https://backend.dehnbostele.workers.dev

# Subsequent publication: use the last SUCCESSFULLY PUBLISHED export, not export/latest.
python3 scripts/build-job-changes.py --web export/2026-09-08/web \
  --previous-web export/2026-09-07/web \
  --publish-base https://backend.dehnbostele.workers.dev
```

Omit `--publish-base` to build locally without network access. Publishing uses the existing
Wrangler credentials and `jobscream-data` bucket. The supplied Worker URL must serve that bucket.
This is opt-in: the existing consolidation/upload commands and production schedule are unchanged.
The maintainer can add this command after a successful daily consolidation when ready to enable it.

Run exactly one publisher at a time (use the scheduler's single-run lock). The remote-parent
checks detect stale exports and most accidental overlap; they are **not** an atomic distributed
lock. Preserve the last published export, including its groups and `changes/latest.json`, separately
from the next run. Do not overwrite it during a same-day consolidation rerun. The builder verifies
the previous projection's digest, both manifests' counts, group membership and unique job keys.

Pages are uploaded first, then their immutable generation manifest, then `changes/latest.json`.
Any failed upload stops publication. The final pointer is read back before reporting success.
Retry the same preserved inputs after a failure; the generation and page hashes are deterministic.
If the pointer was already published before an interruption, retrying that generation is safe.
If inputs changed after an uncertain publication, recover the export matching the remote head
before continuing. Never bootstrap over an existing feed or silently create a disconnected chain.

## Consume

1. GET `/data/changes/latest.json` (use its ETag for subsequent `If-None-Match` requests).
2. Follow `previous` through `/data/changes/<generation>/manifest.json` until reaching your saved
   generation, or `null` on first sync. Pin the head from step 1 while catching up.
3. Apply the collected generations oldest first. Fetch each page at
   `/data/changes/<generation>/<file>`, checking `bytes` and `sha256` before applying it.
4. Upsert by `key`, and remove by `key`. Save the generation checkpoint only after all its pages
   are applied. Replaying pages is idempotent. A zero-page generation still advances the checkpoint.

The generation is an opaque cursor, not a wall-clock query. `snapshot_at` is the source manifest's
Unix milliseconds. `previous: null` denotes a bootstrap containing the complete initial projection.
The first sync must read that bootstrap and intervening deltas; later syncs read only new deltas.
Pages contain at most 1,000 records and 4 MiB; oversized individual records fail the build.

```json
{"op":"upsert","key":"example/company#123","job":{"ats":"example","slug":"company","id":"123","title":"Engineer","location":"Phoenix, AZ","seen":1788740796163,"pub":1788740796163,"jd":"..."}}
{"op":"remove","key":"example/company#456"}
```

Upserts contain the public fields `ats`, `slug`, `id`, `title`, `company`, `location`, `url`,
`seen`, `pub`, `jd` when present. `seen` is first-seen time, not last crawl time. Vector changes,
leaf reassignment, enrichment and company metadata outside this projection do not create events.
Location edits do create events, so consumers can remove jobs that stop matching their city filter.

**A remove means absent from the published embedded-job dataset, not confirmed closed.** Embedding
eligibility and upstream coverage can also change membership. Use `/status` for authoritative
open/removed/unknown checks before making closure claims. This is a daily snapshot diff, not a
complete history of changes between snapshots, and incomplete upstream coverage cannot be detected
merely by validating a locally consistent manifest.

Keep every published generation reachable from the head. This first version has no pruning or
compacted bootstrap rotation. If a required manifest/page is missing, stop and request a fresh
bootstrap; never skip the gap or advance the cursor. The existing `/data/*` cache can delay visibility
of a new head by up to an hour, without breaking the immutable chain.

## Cost and verification

Publisher work is two sequential corpus reads, indexed local SQLite comparison and temporary disk
space proportional to the text projection. Memory holds one group and one bounded output page.
Readers use R2 reads proportional to changed pages and missed generations, with no DO fan-out,
database scans, model calls or vectors. Storage grows with the initial projection plus retained
changes. At 10x changes, page reads/writes and retained delta bytes grow roughly 10x; an unusually
large source correction can still produce a nearly full-corpus delta.

```sh
python3 -m unittest discover -s scripts -p test_job_changes.py -v
```

Tests cover bootstrap/delta replay, reclustering, projection exclusions, incomplete input, duplicate
keys, previous-content drift, page limits, deterministic retries, failed uploads, stale parents,
corrupt pages and publication order. Publication tests use a fake object store; they do not upload
production data.

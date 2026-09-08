# Public job changes (opt-in publisher, v2)

This is the small-page consumer layer over `build-diff.py`, not a second corpus diff.
It uses the existing `/data/*` route and requires no new Worker, database, Board DO calls,
or model requests. The publisher reads the verified **lite** parquet parts once for projection
(plus streaming checksum passes). Ordinary readers download only new NDJSON pages.

## Dataset and fields

`scope: crawler-export-v1` means the open-job crawler export, including jobs not yet embedded.
Both bootstrap and deltas use that same scope. It is **not** a byte-for-byte mirror of the
embedded/model-filtered `groups/` dataset. Mixing a groups-only bootstrap with unrestricted
crawler deltas would miss unchanged, unembedded jobs and drift as model eligibility changes.
Consumers needing the exact search-group membership must continue using that dataset.

The operation mapping is:

| Source | Feed |
| --- | --- |
| `added`, `changed` | `upsert` with the projected current row |
| `removed` | `remove`, preserving `removal` and `removed_at_crawler` |
| `carried`, `changed_prev` | No event |

Every upsert includes `ats`, `slug`, `id`, `title`, `location`, `url`, `content`,
`embed_status`, `published_at`, and `first_seen_at`. Dates are UTC ISO-8601 strings or null.
Null content is normalized to an empty string, matching upstream change-key semantics.
The upstream `change_key` must cover all six mutable projected fields: title, location,
URL, content, embedding status and published date. Identities and first-seen dates are
treated as stable; upstream first-seen corrections require a fresh bootstrap.

Company names are intentionally **not** emitted: board metadata is not part of the source
change key. This avoids silently presenting stale names as fully synchronized data.
Consumers can maintain board/company metadata separately. Adding resolved company names to
this feed requires the upstream board-to-row propagation and change detection Elliott offered.
Departments, enrichment, last crawl time, updated time, vectors and model IDs are also excluded.
An embedding-status transition is an upsert, not a closure. A city-filtered consumer must
remove a locally retained job when an upsert stops matching its filter.

All removal reasons delete the row from a dataset mirror. `closed` reports the same-run
ledger's closed state; `left_dataset` reports a crawler-open job absent from the export;
`unknown` means no usable ledger record. The page layer does not infer a closure from missing
rows or a partial board pull. The reasons are only as complete/fresh as the upstream ledger.
Use `/status` for a current status check when needed. Daily exports do not capture transient
changes between builds.

## Build and publish

Run from `backend/`, with Python 3.10+ and DuckDB (`uv` installs the script dependency):

```sh
# After a successful, serialized consolidation/upload-history run, save the public index.
# Use a cache-busted request; retain this file with the build inputs until validation finishes.
curl --fail "https://backend.dehnbostele.workers.dev/data/diffs/index.json?check=$(date +%s)" \
  -o export/diffs-index.json

# Bootstrap at September 8 or later: earlier ledgers were incomplete.
uv run scripts/build-job-changes.py --out export/feed \
  --snapshot export/2026-09-08 --index export/diffs-index.json \
  --publish-base https://backend.dehnbostele.workers.dev

# Next day: previous is the successful publication receipt, not changes/latest.json.
uv run scripts/build-job-changes.py --out export/feed \
  --diff export/diffs/2026-09-08__2026-09-09.json --previous export/feed/published.json \
  --publish-base https://backend.dehnbostele.workers.dev
```

These dates are examples; use the actual completed export and consecutive diff. Bootstrap
requires an index head matching the export directory date, matching `snapshot_built_at` and
local manifest timestamp, one diff anchor ending at that date, and the full post-carry row
count. It fingerprints all input parquet parts before/after projection, validates unique
keys, and freezes the projected snapshot into immutable pages tied to an exact source cursor.
It does not read the vectors into Python. It trusts the publisher's completed export and
matching index provenance; counts and timestamps alone cannot prove a crawler pull was complete.

Deltas require completed v2 sidecars, matching `from` and parent content hash, valid full-part
digest descriptors, and matching lite-part sizes/hashes. Every part, source op/date, event
count, unique operation and paired changed/changed_prev key is checked. Unknown ops, gaps,
duplicates, conflicting rows and incomplete carries fail before producing a candidate head.
Only the previous feed **manifest** is needed; no previous web directory or full export.

Omit `--publish-base` for an offline build. Output is under `export/feed/changes/`, outside
daily export retention. `changes/latest.json` is only a local candidate. `published.json` is
updated atomically only after successful remote pointer readback. Publishing uses existing
Wrangler credentials and `jobscream-data`; the Worker URL must serve that bucket.
Production consolidation and schedules remain opt-in and unchanged.

Run exactly one publisher, serialized with consolidation. Source checks detect some concurrent
changes but do not replace a lock. Validate local pages and manifest, upload pages, upload their
immutable manifest, recheck the remote parent, then advance `changes/latest.json` and read it back.
The stale-parent checks are not atomic compare-and-swap or a distributed lock.

After an interrupted/uncertain upload, retry the **same candidate**, even if the old full export
has already been pruned:

```sh
uv run scripts/build-job-changes.py --out export/feed \
  --candidate export/feed/changes/GENERATION/manifest.json \
  --publish-base https://backend.dehnbostele.workers.dev
```

This validates/reuploads the preserved pages without rerunning the source pipeline. Replaying
an already-published candidate is safe. Do not rebuild against the new receipt merely to retry.
An unexpected remote head requires reconciling that head, never overwriting it. Keep candidate
pages and all published generations indefinitely, consistent with the upstream history policy.
The unpublished v1 group-diff proposal is not wire-compatible; v2 refuses v1 manifests.

## Consume and recover

1. Read `/data/changes/latest.json`, pin that head, and validate its `generation` as SHA256 of
   canonical JSON of all other fields (sorted keys, compact separators, UTF-8, no ASCII escaping).
2. Follow `previous` via `/data/changes/GENERATION/manifest.json` to the saved generation.
   On first sync, stop at the nearest `kind: bootstrap`. Verify every manifest hash and link.
3. Apply generations oldest first, verifying each page's `bytes`, SHA256 and row count.
   Pages contain at most 1,000 rows and 4 MiB. Oversized individual records fail publication.
4. Upsert or delete by `ats/slug#id`. Commit all pages and the checkpoint atomically. A zero-page
   delta still advances the checkpoint. Retrying the same generation is a no-op; replaying an
   individual operation is idempotent.

`generation` is the opaque integrity/replay cursor; `cursor` is its upstream export date and
`source` is the exact upstream diff name/content hash. They serve different purposes. A date
alone does not identify a rebuilt bootstrap. Delta headers also pin a canonical sidecar hash.
`snapshot_at` is the bootstrap manifest's build time; it is null on deltas, which only assert
the source export date.

The callable `apply_generation(db, folder, header, reset=False)` is a small SQLite reference
consumer used by the tests. It verifies pages inside a transaction and rolls back both data
and checkpoint on corruption. A bootstrap over existing state requires explicit `reset=True`.
Consumers adapting it for their own persistence must retain those transaction semantics.

History does not expire. If any required page/manifest/diff is missing or corrupt, stop and
leave the checkpoint unchanged. First retry transient fetch errors; never skip a gap. For
unrecoverable gaps, the publisher can make a fresh bootstrap from the current completed export
using `--snapshot`, the matching `--index`, and `--previous export/feed/published.json`. This
retains the previous link while providing a new full recovery boundary. Consumers explicitly
replace their local scope from that verified bootstrap and continue with later deltas. The
tests exercise this replacement and removal of stale local rows. There is no automatic pruning
or periodic full-bootstrap job. Do not mix this feed with the mutable groups bootstrap.

The existing `/data/*` cache may delay discovery of a new head. Use cache busting/no-cache for
publication readback; consumers may use ETags/conditional GETs for ordinary polling.

## Verification, portability and costs

```sh
uv run --with duckdb python -m unittest discover -s scripts -p test_job_changes.py -v

# Same fixtures in a provider-independent OCI runtime; no Cloudflare credentials needed.
docker run --rm -v "$PWD:/work" -w /work python:3.11-slim sh -c \
  'pip install duckdb && python -m unittest discover -s scripts -p test_job_changes.py -v'
```

The tests build real synthetic parquet and run the upstream diff SQL, including carry-forward,
all tracked field changes and ledger-based removal. They cover bootstrap alignment/cutoff,
replay, fresh recovery, gaps, incomplete/duplicate/conflicting input, source and page corruption,
bounded pages, every publication failure stage, stale parents, concurrency and readback.
The removal SQL test also guards the qualified-column fix required by the new ledger join.

At 1x, bootstrap requires one full projected export plus checksum reads and temporary SQLite
space; expected daily runs read only lite diffs, with local disk proportional to changed records.
DuckDB is limited to 512 MiB and Python streams small batches plus one bounded page. Source
decoding still needs room for an individual input record. There are no Neon queries or writes.
Readers pay for changed page downloads, not embeddings or Board DO requests. At 10x changes,
page I/O, temporary disk and retained delta storage grow approximately 10x. A large correction
can approach a full export; a recovery bootstrap is deliberately an exceptional full read.

The core is local Python/DuckDB/SQLite and static JSON/NDJSON. Replace the `put`/`remote_head`
transport functions with an S3-compatible or filesystem/HTTP publisher to move providers;
the same fake-store publication and transactional replay fixtures apply. No production upload,
deployment or crawler scheduling change is part of this PR.

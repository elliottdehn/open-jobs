#!/usr/bin/env python3
"""
stream.py - stream jobs from open-jobs.parquet one at a time, decompressing only the row group
currently being read, never the whole file.

The dataset is columnar Parquet, zstd-compressed per row group (~5000 rows each). That means:
  * memory stays bounded even on the full ~million-row file - one row group is held at a time
  * full rows including embeddings are yielded by default (the embeddings are the point)
  * pass --no-embeddings to skip the two big vector columns entirely - they are not even
    decompressed/fetched, which is a real speedup when reading over https:// (skips most of the file)
  * works the same on a local file or an https:// URL (only the needed byte ranges are fetched)

    from stream import stream_jobs
    for job in stream_jobs("open-jobs.parquet"):          # includes title_embedding / jd_embedding
        ...
    for job in stream_jobs("open-jobs.parquet", embeddings=False):   # metadata only, faster
        ...

    # CLI: print jobs as JSONL
    python3 stream.py open-jobs.parquet --no-embeddings --limit 20
    python3 stream.py https://download.jobscream.com/open-jobs.parquet --where function=engineering --limit 50

Needs: pip install pyarrow   (and fsspec for http(s) URLs, usually already present with pandas)
"""
import argparse, json, sys
import pyarrow.parquet as pq

EMB = ("title_embedding", "jd_embedding")   # the heavy vector columns


def _source(path):
    """local path -> path; http(s) URL -> a range-readable file object so Parquet fetches only the
    footer + the row groups (and columns) it needs (no full download)."""
    if path.startswith(("http://", "https://")):
        try:
            import fsspec
        except ImportError:
            sys.exit("reading a URL needs fsspec: pip install fsspec aiohttp")
        return fsspec.open(path, "rb").open()
    return path


def stream_jobs(path, columns=None, embeddings=True, batch_rows=2000):
    """Yield jobs as dicts, one at a time, holding only one row-group slice in memory.

    columns:    explicit column list, or None for all columns.
    embeddings: when columns is None, set False to drop the two embedding columns (faster, and
                avoids fetching most of the file over http).
    """
    pf = pq.ParquetFile(_source(path))
    if columns is None:
        columns = [c for c in pf.schema_arrow.names if embeddings or c not in EMB]
    for batch in pf.iter_batches(batch_size=batch_rows, columns=columns):
        for row in batch.to_pylist():     # materializes only this batch, not the file
            yield row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="open-jobs.parquet")
    ap.add_argument("--columns", default="", help="comma list; default = all columns")
    ap.add_argument("--no-embeddings", action="store_true", help="skip the vector columns (faster)")
    ap.add_argument("--where", default="", help="simple field=value filters, comma-separated")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()

    cols = [c.strip() for c in args.columns.split(",") if c.strip()] or None
    filters = dict(kv.split("=", 1) for kv in args.where.split(",") if "=" in kv)
    if cols and filters:                  # ensure filtered fields are read even if not projected
        cols = list(dict.fromkeys(cols + [k for k in filters]))

    n = 0
    for job in stream_jobs(args.path, columns=cols, embeddings=not args.no_embeddings,
                           batch_rows=args.batch):
        if filters and any(str(job.get(k)) != v for k, v in filters.items()):
            continue
        sys.stdout.write(json.dumps(job, ensure_ascii=False, default=str) + "\n")
        n += 1
        if args.limit and n >= args.limit:
            break


if __name__ == "__main__":
    main()

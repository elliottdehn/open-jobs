# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3", "duckdb>=1.1"]
# ///
"""One link for the whole dataset: tar the export (jobs/*.parquet, boards/*.parquet, a README) straight into a
multipart upload at a stable key, no local copy of the archive.

  uv run scripts/build-archive.py [--export export/<date>] [--key exports/open-jobs-latest.tar]

Parquet is already zstd-compressed, so the tar is not compressed. Overwritten nightly; the README inside names the
date. Served at /data/<key> with HTTP Range, so a browser or curl -C - resumes.
"""
import argparse, io, json, os, sys, tarfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2
import duckdb
ap = argparse.ArgumentParser()
ap.add_argument("--export", default=os.environ.get("EXPORT_DIR", "export/latest"))
ap.add_argument("--key", default="exports/open-jobs-latest.tar")
ap.add_argument("--part-mb", type=int, default=64)
a = ap.parse_args()
root = a.export if os.path.isabs(a.export) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", a.export)
date = os.path.basename(os.path.realpath(root))
jobs = sorted(f for f in os.listdir(os.path.join(root, "jobs")) if f.endswith(".parquet"))
boards = sorted(f for f in os.listdir(os.path.join(root, "boards")) if f.endswith(".parquet"))
if len(jobs) < 30: sys.exit(f"only {len(jobs)} jobs files under {root}; refusing to publish a partial archive")
con = duckdb.connect(); con.execute("SET TimeZone='UTC'")
n, boards_n = con.execute(f"SELECT count(*), count(DISTINCT ats || '/' || slug) FROM read_parquet('{os.path.join(root, 'jobs', '*.parquet')}', union_by_name=true) WHERE is_open").fetchone()
cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{os.path.join(root, 'jobs', '*.parquet')}', union_by_name=true)").fetchall()]
readme = f"""Open Jobs, full export of {date}
{n:,} open job postings from {boards_n:,} career sites, crawled nightly. CC0 1.0. No account, no key.
https://github.com/elliottdehn/open-jobs  ·  https://backend.dehnbostele.workers.dev/data/

jobs/<ats>.parquet    one row per open posting: {", ".join(cols)}
boards/<ats>.parquet  one row per career site (fetch metadata and company fields)

Read it:  import duckdb; duckdb.sql("SELECT ats, count(*) FROM 'jobs/*.parquet' GROUP BY 1")
Daily diffs, the ledger of every posting ever recorded, and the change feed: https://backend.dehnbostele.workers.dev/data/
"""
r2 = R2(); part_size = a.part_mb << 20
class MultipartSink(io.RawIOBase):
    """A write-only file that ships every `part_size` bytes as one multipart part."""
    def __init__(self):
        self.buf = bytearray(); self.parts = []; self.n = 0; self.total = 0
        self.upload_id = r2.client.create_multipart_upload(Bucket=r2.bucket, Key=a.key, ContentType="application/x-tar")["UploadId"]
    def writable(self): return True
    def write(self, b):
        self.buf += b; self.total += len(b)
        while len(self.buf) >= part_size: self._ship(bytes(self.buf[:part_size])); del self.buf[:part_size]
        return len(b)
    def _ship(self, data):
        self.n += 1
        for attempt in range(5):
            try:
                etag = r2.client.upload_part(Bucket=r2.bucket, Key=a.key, UploadId=self.upload_id, PartNumber=self.n, Body=data)["ETag"]; break
            except Exception as e:
                if attempt == 4: raise
                time.sleep(3 * (attempt + 1))
        self.parts.append({"PartNumber": self.n, "ETag": etag})
        print(f"\r  {self.total / 1e9:.2f} GB shipped in {self.n} parts", end="", flush=True)
    def finish(self):
        if self.buf: self._ship(bytes(self.buf)); self.buf = bytearray()
        r2.client.complete_multipart_upload(Bucket=r2.bucket, Key=a.key, UploadId=self.upload_id, MultipartUpload={"Parts": self.parts})
    def abort(self): r2.client.abort_multipart_upload(Bucket=r2.bucket, Key=a.key, UploadId=self.upload_id)
t0 = time.time(); sink = MultipartSink()
try:
    with tarfile.open(fileobj=sink, mode="w|") as tar:
        info = tarfile.TarInfo("open-jobs/README.txt"); data = readme.encode(); info.size = len(data); info.mtime = int(time.time()); tar.addfile(info, io.BytesIO(data))
        for sub, names in (("jobs", jobs), ("boards", boards)):
            for f in names: tar.add(os.path.join(root, sub, f), arcname=f"open-jobs/{sub}/{f}")
    sink.finish()
except BaseException:
    sink.abort(); raise
print(f"\narchive {a.key}: {sink.total / 1e9:.2f} GB, {len(jobs)} jobs files + {len(boards)} boards files, {n:,} postings, {time.time() - t0:.0f}s", flush=True)

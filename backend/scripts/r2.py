"""R2 through the S3 API, shared by the consolidation scripts.

Credentials: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY from the environment, else from
backend/.dev.vars (git-ignored). Bucket: R2_BUCKET (default jobscream-data).

    from r2 import R2
    r2 = R2()                                   # raises if no credentials
    r2.put_file("groups/12.json", path, "application/json")   # multipart above 64 MB, retried
    r2.head("manifest.json") -> {"size", "etag"} | None
    r2.list("groups/") -> iterator of (key, size, etag)
    r2.delete(key); r2.get_file(key, path)
    r2.duckdb(con)                              # SET s3_* so read_parquet('s3://bucket/...') works
    r2.url("snapshots/greenhouse/*.parquet") -> "s3://jobscream-data/snapshots/greenhouse/*.parquet"

Add "boto3" to a script's inline dependencies to use it.
"""
import os, sys, time, threading

DEFAULT_BUCKET = "jobscream-data"
HERE = os.path.dirname(os.path.abspath(__file__))


def load_dev_vars(path=os.path.join(HERE, "..", ".dev.vars")):
    """Fill os.environ from backend/.dev.vars for keys not already set (KEY=value lines, # comments)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1); k = k.strip(); v = v.strip().strip('"').strip("'")
                if k and k not in os.environ: os.environ[k] = v
    except FileNotFoundError:
        pass


class R2:
    def __init__(self, bucket=None):
        load_dev_vars()
        self.account = os.environ.get("R2_ACCOUNT_ID"); self.key = os.environ.get("R2_ACCESS_KEY_ID"); self.secret = os.environ.get("R2_SECRET_ACCESS_KEY")
        if not (self.account and self.key and self.secret):
            raise RuntimeError("R2 credentials missing: set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (env or backend/.dev.vars)")
        self.bucket = bucket or os.environ.get("R2_BUCKET", DEFAULT_BUCKET)
        self.endpoint = f"https://{self.account}.r2.cloudflarestorage.com"
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config
        self._boto3 = boto3
        self.client = boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=self.key, aws_secret_access_key=self.secret, region_name="auto",
                                   config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, max_pool_connections=32))
        self.transfer = TransferConfig(multipart_threshold=64 << 20, multipart_chunksize=64 << 20, max_concurrency=4, use_threads=True)
        self._lock = threading.Lock(); self.uploaded = 0; self.uploaded_bytes = 0

    def url(self, key): return f"s3://{self.bucket}/{key}"

    def put_file(self, key, path, content_type="application/octet-stream", retries=4):
        """Upload one file; multipart when large. Retries with backoff; raises after the last attempt."""
        for attempt in range(retries + 1):
            try:
                self.client.upload_file(path, self.bucket, key, ExtraArgs={"ContentType": content_type}, Config=self.transfer)
                with self._lock: self.uploaded += 1; self.uploaded_bytes += os.path.getsize(path)
                return
            except Exception as e:
                if attempt >= retries: raise
                time.sleep(2 * (attempt + 1))

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def head(self, key):
        try:
            h = self.client.head_object(Bucket=self.bucket, Key=key)
            return {"size": h["ContentLength"], "etag": h.get("ETag", "").strip('"'), "modified": h.get("LastModified")}
        except self.client.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"): return None
            raise

    def list(self, prefix):
        """Yield (key, size, etag) for every object under prefix."""
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []): yield o["Key"], o["Size"], o.get("ETag", "").strip('"')

    def delete(self, key): self.client.delete_object(Bucket=self.bucket, Key=key)
    def abort_stale_multipart(self, prefix="", older_than_s=0):
        """Abort incomplete multipart uploads under a prefix (a killed writer leaves its parts behind: billable, invisible
        to listings, and DuckDB refuses to write over the key). Returns how many were aborted."""
        import datetime as _dt
        cut = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=older_than_s); n = 0; kw = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            resp = self.client.list_multipart_uploads(**kw)
            for u in resp.get("Uploads", []):
                if u["Initiated"] <= cut: self.client.abort_multipart_upload(Bucket=self.bucket, Key=u["Key"], UploadId=u["UploadId"]); n += 1
            if not resp.get("IsTruncated"): break
            kw.update(KeyMarker=resp.get("NextKeyMarker"), UploadIdMarker=resp.get("NextUploadIdMarker"))
        return n
    def copy(self, src, dst, content_type="application/octet-stream"):
        """Server-side copy within the bucket (no download; one class A operation)."""
        self.client.copy_object(Bucket=self.bucket, CopySource={"Bucket": self.bucket, "Key": src}, Key=dst, ContentType=content_type, MetadataDirective="REPLACE")

    def get_file(self, key, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.client.download_file(self.bucket, key, path, Config=self.transfer)

    def duckdb(self, con):
        """Point a DuckDB connection at this bucket: read_parquet('s3://<bucket>/<key>') and COPY ... TO 's3://...'."""
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"SET s3_endpoint='{self.account}.r2.cloudflarestorage.com'")
        con.execute("SET s3_region='auto'"); con.execute("SET s3_url_style='path'"); con.execute("SET s3_use_ssl=true")
        con.execute(f"SET s3_access_key_id='{self.key}'"); con.execute(f"SET s3_secret_access_key='{self.secret}'")
        # R2 answers an occasional 503; one of those killed a 45-minute stage on 2026-09-10. Retry with backoff.
        for k, v in (("http_retries", 8), ("http_retry_wait_ms", 1500), ("http_retry_backoff", 2), ("http_keep_alive", "true")):
            try: con.execute(f"SET {k}={v}")
            except Exception: pass
        return con


class Uploader:
    """Background upload queue: hand it (key, path, content_type) as files are produced; join() at the end.
    Failures are collected, not raised mid-build, so the producer never stalls; the caller decides."""
    def __init__(self, r2, workers=8, delete_after=False):
        import queue
        self.r2 = r2; self.q = queue.Queue(maxsize=workers * 4); self.failed = []; self._lock = threading.Lock()
        self.delete_after = delete_after; self.sizes = {}  # key -> bytes uploaded (what a reader can verify against the bucket)
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(workers)]
        for t in self.threads: t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None: self.q.task_done(); return
            key, path, ctype = item
            try:
                size = os.path.getsize(path); self.r2.put_file(key, path, ctype)
                with self._lock: self.sizes[key] = size
                if self.delete_after: os.remove(path)  # 20 GB cloud disk: the bucket copy is the copy
            except Exception as e:
                with self._lock: self.failed.append((key, str(e)[:160]))
            finally: self.q.task_done()

    def put(self, key, path, content_type="application/octet-stream"): self.q.put((key, path, content_type))

    def join(self):
        for _ in self.threads: self.q.put(None)
        for t in self.threads: t.join()
        return self.failed


if __name__ == "__main__":
    r2 = R2()
    print(f"bucket {r2.bucket} at {r2.endpoint}")
    for k, s, e in list(r2.list(sys.argv[1] if len(sys.argv) > 1 else "diffs/"))[:10]: print(f"  {k}  {s:,} B  {e[:12]}")

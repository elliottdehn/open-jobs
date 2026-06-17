#!/usr/bin/env python3
"""
download.py - download the open-jobs dataset (resumable, streaming, no dependencies).

Streams the Parquet file to disk in chunks with a progress readout. Resumable: if the connection
drops, just run it again and it continues from where it stopped (HTTP Range). Writes to a .part
file and renames on success so you never end up with a half-file that looks complete.

    python3 download.py                       # uses OPEN_JOBS_URL or the built-in URL
    python3 download.py <url> -o open-jobs.parquet
"""
import argparse, os, sys, time, urllib.request, urllib.error

DATASET_URL = os.environ.get("OPEN_JOBS_URL", "https://download.jobscream.com/open-jobs.parquet")


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB": return f"{n:.1f}{u}"
        n /= 1024


def _attempt(url, part, out):
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "open-jobs-download/1.0"})
    if have: req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and getattr(r, "status", r.getcode()) != 206:    # server ignored Range -> restart
            have = 0
        total = int(r.headers.get("Content-Length", 0)) + have
        done, t0, last = have, time.time(), 0.0
        with open(part, "ab" if have else "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk: break
                f.write(chunk); done += len(chunk)
                now = time.time()
                if now - last > 0.2:                              # throttle the progress line
                    last = now
                    rate = (done - have) / max(now - t0, 1e-6)
                    pct = f"{done/total*100:5.1f}% " if total else ""
                    sys.stderr.write(f"\r  {pct}{human(done)}"
                                     f"{'/' + human(total) if total else ''}  {human(rate)}/s   ")
    sys.stderr.write("\n")
    os.replace(part, out)
    return done


def download(url, out, retries=6):
    part = out + ".part"
    for i in range(retries):
        try:
            return _attempt(url, part, out)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            wait = min(2 ** i, 30)
            sys.stderr.write(f"\n  interrupted ({str(e)[:60]}); resuming in {wait}s "
                             f"[{i+1}/{retries}]\n")
            time.sleep(wait)
    sys.exit("download failed after retries (partial saved to .part; rerun to resume)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DATASET_URL)
    ap.add_argument("-o", "--out", default="open-jobs.parquet")
    args = ap.parse_args()
    if "CONFIGURE-ME" in args.url:
        sys.exit("set the dataset URL: pass it as an arg or export OPEN_JOBS_URL=...")
    sys.stderr.write(f"downloading {args.url}\n")
    n = download(args.url, args.out)
    sys.stderr.write(f"saved {human(n)} -> {args.out}\n")


if __name__ == "__main__":
    main()

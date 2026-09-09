# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub>=0.27"]
# ///
"""Mirror the history files to a Hugging Face dataset repo (the `mirror` stage): the latest ledger day, every diff's
lite parts and sidecar, and a dataset card whose configs make the parquet browsable in the hub's viewer.

  uv run scripts/publish-hf.py [--repo <user>/open-jobs] [--diffs export/diffs] [--ledger export/ledger] [--dry-run]

Needs HF_TOKEN (a write token from https://huggingface.co/settings/tokens) and HF_REPO in the environment or
backend/.dev.vars. The first run creates the repo (public, CC0). Each run is one commit; files already present with
the same size are skipped, so a re-run uploads nothing new. Only the newest ledger day is kept in the repo (they
supersede each other; the bucket keeps all days); diffs accumulate, one lite set per day.
"""
import argparse, glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import load_dev_vars
load_dev_vars()
ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=os.environ.get("HF_REPO"))
ap.add_argument("--diffs", default="export/diffs"); ap.add_argument("--ledger", default="export/ledger")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
token = os.environ.get("HF_TOKEN")
if not a.repo or not token:
    print("HF_REPO and HF_TOKEN are not both set; nothing mirrored (set them in backend/.dev.vars to enable)"); sys.exit(0)
from huggingface_hub import HfApi, CommitOperationAdd, CommitOperationDelete
api = HfApi(token=token)

# what to upload: newest ledger day, every final diff's lite parts + sidecar
ledger_days = sorted(d for d in glob.glob(os.path.join(a.ledger, "20*")) if os.path.isdir(d))
diffs = []
for d in sorted(d for d in glob.glob(os.path.join(a.diffs, "*__*")) if os.path.isdir(d)):
    try: side = json.load(open(d + ".json"))
    except Exception: continue
    if side.get("carry_done") is True and glob.glob(os.path.join(d, "lite", "*.parquet")): diffs.append((d, side))
if not ledger_days and not diffs: sys.exit("nothing to mirror: no ledger day or final diff on disk")
files = {}  # repo path -> local path
if ledger_days:
    day = os.path.basename(ledger_days[-1])
    for p in sorted(glob.glob(os.path.join(ledger_days[-1], "*.parquet"))): files[f"ledger/{day}/{os.path.basename(p)}"] = p
for d, side in diffs:
    name = os.path.basename(d)
    for p in sorted(glob.glob(os.path.join(d, "lite", "*.parquet"))): files[f"diffs/{name}/{os.path.basename(p)}"] = p
    files[f"diffs/{name}.json"] = d + ".json"

latest_side = diffs[-1][1] if diffs else {}
n_jobs = latest_side.get("new_jobs_after_carry") or latest_side.get("new_jobs")
card = f"""---
license: cc0-1.0
pretty_name: Open Jobs
language:
- en
size_categories:
- 1M<n<10M
tags:
- jobs
- job-postings
- labor-market
- hiring
- daily
configs:
- config_name: ledger
  data_files: ledger/*/*.parquet
- config_name: diffs
  data_files: diffs/*/*.parquet
---

# Open Jobs

Every job posting a crawler of about 65,000 company career sites has recorded, updated nightly.
{f"{n_jobs:,} open postings in the latest export; " if n_jobs else ""}{f"{len(diffs)} daily diffs; " if diffs else ""}the crawler
records the day it first sees each posting on a clock the job boards cannot re-stamp, which is what makes
posting age, closures, and re-posting measurable.

Source, search page, API, and the full parquet with descriptions and embeddings: https://github.com/elliottdehn/open-jobs
(CC0; no account, no business model).

## What is here

**`ledger/<date>/`** — one row per posting the crawler has ever recorded, open or removed, as of that date:
`ats, slug, id, title, location, url, published_at, content_hash, first_seen_at, last_seen_at, changed_at,
removed_at, is_open, detail_status, embed_status`. `first_seen_at` is the crawler's own first sighting;
`removed_at` is when the board stopped listing it. This is the file for posting lifetimes and survival
curves. Only the newest day is kept here; every day is kept in the project's bucket.

**`diffs/<from>__<to>/`** — what changed between two consecutive nightly exports, one row per event, with
`op` = `added` | `removed` | `changed` | `changed_prev` | `carried` and the job's fields (`title, location,
url, published_at, first_seen_at, embed_status`, and the description `content` on `added` and `changed`
rows). `removed` rows carry `removal` = `closed` (the crawler saw the board drop it) | `left_dataset` |
`unknown`. The sidecar `diffs/<from>__<to>.json` has counts, per-part sha256, and the parent diff's hash,
so the chain verifies. These are the *lite* parts; the full parts with vectors are at
`https://backend.dehnbostele.workers.dev/data/diffs/`.

## Reading it

```python
import duckdb
led = duckdb.read_parquet("hf://datasets/{a.repo}/ledger/*/*.parquet")
duckdb.sql("SELECT count(*) FILTER (is_open) AS open, count(*) FILTER (NOT is_open) AS removed FROM led")
```

Freshness grades on the search page compare a posting's age on the crawler's clock to how long postings
with that content typically stay open; the model and the search index are published alongside, see the repo.
"""
ops = [CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=card.encode())]
print(f"repo {a.repo}: {len(files)} files ({sum(os.path.getsize(p) for p in files.values()) / 1e6:.0f} MB) + README", flush=True)
if a.dry_run:
    for k in sorted(files)[:12]: print("  ", k, os.path.getsize(files[k]) // 1_000_000, "MB")
    sys.exit(0)
api.create_repo(a.repo, repo_type="dataset", exist_ok=True, private=False)
have = {}
try:
    info = api.dataset_info(a.repo, files_metadata=True)
    have = {s.rfilename: s.size for s in info.siblings}
except Exception: pass
todo = {k: p for k, p in files.items() if have.get(k) != os.path.getsize(p)}
stale_ledger = [k for k in have if k.startswith("ledger/") and k not in files]
for k, p in sorted(todo.items()): ops.append(CommitOperationAdd(path_in_repo=k, path_or_fileobj=p))
for k in stale_ledger: ops.append(CommitOperationDelete(path_in_repo=k))
t = time.time()
api.create_commit(a.repo, repo_type="dataset", operations=ops, commit_message=f"nightly mirror {time.strftime('%Y-%m-%d')}: {len(todo)} files")
print(f"mirrored {len(todo)} new file(s), removed {len(stale_ledger)} superseded ledger file(s), {time.time() - t:.0f}s -> https://huggingface.co/datasets/{a.repo}", flush=True)

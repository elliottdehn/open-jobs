# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow", "boto3"]
# ///
"""Salary estimator: ridge regression from the job embedding (1536-d) to log(annual USD), trained on
postings with a mechanically stated USD salary (tools/salary.py). Writes <EXPORT_DIR>/web/salary-model.json
{recipe, w[1536], b, sigma, n, holdout} — the client applies exp(w·v + b) as an *estimate* for the rest.
Run: EXPORT_DIR=export/latest uv run scripts/train-salary.py"""
import json, os, sys, time
import numpy as np, duckdb
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from salary import extract

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2
# EXPORT_DIR: local export dir or s3://bucket/exports/<date> (read in place); outputs go to <WORK_DIR or EXPORT_DIR>/web
_ed = os.environ.get("EXPORT_DIR", "export/latest"); _s3 = _ed.startswith("s3://")
root = _ed.rstrip("/") if _s3 else os.path.join(os.path.dirname(__file__), "..", _ed)
work = os.environ.get("WORK_DIR") or (os.path.join(os.path.dirname(__file__), "..", "work-" + _ed.rstrip("/").rsplit("/", 1)[-1]) if _s3 else root)
J = f"{root}/jobs/*.parquet"
con = (R2().duckdb(duckdb.connect()) if _s3 else duckdb.connect()); con.execute("SET threads=4"); con.execute(f"SET memory_limit='{'4GB' if _s3 else '8GB'}'")  # bucket reads buffer on top of the cap; con.execute("SET arrow_large_buffer_size=true")
q = f"""SELECT embed_model, content, embedding FROM read_parquet('{J}')
        WHERE is_open AND embed_status='done' AND embedding IS NOT NULL AND length(content) > 300
          AND (contains(content, '$') OR contains(content, 'USD') OR contains(content, '€') OR contains(content, '£'))
       """
import re
GATE = re.compile(r"[$€£]\s*\d|\b(?:USD|salary|compensation|pay range)\b", re.I)
# Bounded memory: a ridge fit on 1536 dims does not need every stated salary. Reservoir-sample up to MAX_ROWS so the
# design matrix stays ~1.5 GB; 734k rows (4.5 GB, plus the solver's copies) was OOM-killed in the 16 GB VM on 2026-09-10.
MAX_ROWS = int(os.environ.get("SALARY_MAX_ROWS", "250000"))
rng0 = np.random.default_rng(0); D0 = 1536
Xr = np.empty((MAX_ROWS, D0), dtype=np.float32); yr = np.empty(MAX_ROWS, dtype=np.float64); n_cand = 0
def keep(v, val):
    global n_cand
    if n_cand < MAX_ROWS: Xr[n_cand] = v; yr[n_cand] = val
    else:
        j = rng0.integers(0, n_cand + 1)
        if j < MAX_ROWS: Xr[j] = v; yr[j] = val
    n_cand += 1
t0 = time.time(); seen = 0; tag = None
reader = con.execute(q).fetch_record_batch(20_000)
while True:
    try: b = reader.read_next_batch()
    except StopIteration: break
    tag = tag or b.column("embed_model")[0].as_py()
    contents = b.column("content").to_pylist(); emb = b.column("embedding")
    vals = emb.values.to_numpy(zero_copy_only=False); offs = emb.offsets.to_numpy()
    for i, c in enumerate(contents):
        seen += 1
        if not GATE.search(c): continue  # cheap check before the heavy extractor
        s = extract(c)
        if not s or s["currency"] != "USD" or s["period"] not in ("year", "hour") or not (30_000 <= s["annual_max"] <= 1_000_000): continue
        mid = (s["annual_min"] + s["annual_max"]) / 2 if s["annual_min"] > 0 else s["annual_max"]
        keep(vals[offs[i]:offs[i + 1]].astype(np.float32), np.log(mid))
    print(f"\r  scanned {seen:,} jobs, {n_cand:,} with stated USD salary", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
con.close()  # release DuckDB's buffers before the solver needs the memory
N = min(n_cand, MAX_ROWS); X = Xr[:N]; y = yr[:N]; D = X.shape[1]
if n_cand > MAX_ROWS: print(f"  sampled {N:,} of {n_cand:,} stated salaries (SALARY_MAX_ROWS)")
X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
print(f"{N:,} training rows from {seen:,} jobs ({N/seen:.1%}) in {time.time()-t0:.0f}s; median stated ${np.exp(np.median(y)):,.0f}")
rng = np.random.default_rng(0); idx = rng.permutation(N); cut = int(N * 0.9); tr, te = idx[:cut], idx[cut:]
def fit(Xa, ya, lam):
    mu = ya.mean(); A = Xa.T @ Xa + lam * np.eye(D); w = np.linalg.solve(A, Xa.T @ (ya - mu)); return w, mu
best = None
for lam in (0.3, 1, 3, 10, 30):
    w, b = fit(X[tr], y[tr], lam); pred = X[te] @ w + b; err = pred - y[te]
    mae = np.mean(np.abs(err)); within = np.mean(np.abs(np.exp(err) - 1) <= 0.2)
    print(f"  lambda {lam:>4}: holdout MAE {mae:.3f} log ≈ {100*(np.exp(mae)-1):.0f}% · within ±20%: {within:.0%}")
    if best is None or mae < best[0]: best = (mae, lam, within)
w, b = fit(X, y, best[1]); resid = y - (X @ w + b); sigma = float(resid.std())
out = os.path.join(work, "web", "salary-model.json"); os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({"recipe": tag, "dims": D, "w": [round(float(v), 6) for v in w], "b": float(b), "sigma": sigma, "n": int(N), "lambda": best[1],
           "holdout": {"mae_log": float(best[0]), "within_20pct": float(best[2])}, "target": "log(annual USD midpoint)", "trained_at": int(time.time() * 1000)}, open(out, "w", encoding="utf-8"))
print(f"wrote {out}: n={N:,}, lambda={best[1]}, sigma={sigma:.3f} (±{100*(np.exp(sigma)-1):.0f}%), holdout within ±20%: {best[2]:.0%}")

# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow"]
# ///
"""Salary estimator: ridge regression from the job embedding (1536-d) to log(annual USD), trained on
postings with a mechanically stated USD salary (tools/salary.py). Writes <EXPORT_DIR>/web/salary-model.json
{recipe, w[1536], b, sigma, n, holdout} — the client applies exp(w·v + b) as an *estimate* for the rest.
Run: EXPORT_DIR=export/latest uv run scripts/train-salary.py"""
import json, os, sys, time
import numpy as np, duckdb
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from salary import extract

root = os.path.join(os.path.dirname(__file__), "..", os.environ.get("EXPORT_DIR", "export/latest"))
J = os.path.join(root, "jobs", "*.parquet")
con = duckdb.connect(); con.execute("SET threads=4"); con.execute("SET memory_limit='8GB'"); con.execute("SET arrow_large_buffer_size=true")
q = f"""SELECT embed_model, content, embedding FROM read_parquet('{J}')
        WHERE is_open AND embed_status='done' AND embedding IS NOT NULL AND length(content) > 300 USING SAMPLE 700000 ROWS"""
t0 = time.time(); X = []; y = []; seen = 0; tag = None
reader = con.execute(q).fetch_record_batch(20_000)
while True:
    try: b = reader.read_next_batch()
    except StopIteration: break
    tag = tag or b.column("embed_model")[0].as_py()
    contents = b.column("content").to_pylist(); emb = b.column("embedding")
    vals = emb.values.to_numpy(zero_copy_only=False); offs = emb.offsets.to_numpy()
    for i, c in enumerate(contents):
        seen += 1
        s = extract(c)
        if not s or s["currency"] != "USD" or s["period"] not in ("year", "hour") or not (30_000 <= s["annual_max"] <= 1_000_000): continue
        mid = (s["annual_min"] + s["annual_max"]) / 2 if s["annual_min"] > 0 else s["annual_max"]
        X.append(vals[offs[i]:offs[i + 1]].astype(np.float32)); y.append(np.log(mid))
    print(f"\r  scanned {seen:,} jobs, {len(y):,} with stated USD salary", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
X = np.stack(X); y = np.asarray(y, dtype=np.float64); N, D = X.shape
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
out = os.path.join(root, "web", "salary-model.json"); os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({"recipe": tag, "dims": D, "w": [round(float(v), 6) for v in w], "b": float(b), "sigma": sigma, "n": int(N), "lambda": best[1],
           "holdout": {"mae_log": float(best[0]), "within_20pct": float(best[2])}, "target": "log(annual USD midpoint)", "trained_at": int(time.time() * 1000)}, open(out, "w"))
print(f"wrote {out}: n={N:,}, lambda={best[1]}, sigma={sigma:.3f} (±{100*(np.exp(sigma)-1):.0f}%), holdout within ±20%: {best[2]:.0%}")

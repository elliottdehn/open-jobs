# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow", "boto3"]
# ///
"""Posting-age estimator: predict how old an open posting of this content *typically* is, from its
embedding. The client compares a job's actual age against the prediction: younger -> Fresh, older ->
Stale (an outlier survivor for its market: hard-to-fill, ghost, or bumped). Trains a ridge head and a
small MLP (1536->64->1) on log1p(age days) over all dated open postings; publishes whichever wins the
holdout to <EXPORT_DIR>/web/age-model.json {kind, recipe, ... , holdout}.
Run: EXPORT_DIR=export/latest uv run scripts/train-age.py"""
import sys, json, os, time
import numpy as np, duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2
# EXPORT_DIR: local export dir or s3://bucket/exports/<date> (read in place); outputs go to <WORK_DIR or EXPORT_DIR>/web
_ed = os.environ.get("EXPORT_DIR", "export/latest"); _s3 = _ed.startswith("s3://")
root = _ed.rstrip("/") if _s3 else os.path.join(os.path.dirname(__file__), "..", _ed)
work = os.environ.get("WORK_DIR") or (os.path.join(os.path.dirname(__file__), "..", "work-" + _ed.rstrip("/").rsplit("/", 1)[-1]) if _s3 else root)
J = f"{root}/jobs/*.parquet"
TARGET_N = int(os.environ.get("AGE_MAX_ROWS", "250000"))  # plenty for a 1536-d head; 800k (~5 GB + the solver's copies) was OOM-killed in the 16 GB VM
CAP_DAYS = 3650.0   # fossils beyond 10y say "ancient", not "12.7y exactly"

con = (R2().duckdb(duckdb.connect()) if _s3 else duckdb.connect()); con.execute("SET threads=4"); con.execute("SET memory_limit='6GB'"); con.execute("SET arrow_large_buffer_size=true")
total = con.execute(f"SELECT count(*) FROM read_parquet('{J}') WHERE is_open AND embed_status='done' AND embedding IS NOT NULL AND published_at IS NOT NULL").fetchone()[0]
p_keep = min(1.0, TARGET_N / max(total, 1))
q = f"""SELECT embed_model, greatest(0, date_diff('day', published_at, now())) AS age_days, embedding
        FROM read_parquet('{J}')
        WHERE is_open AND embed_status='done' AND embedding IS NOT NULL AND published_at IS NOT NULL"""
t0 = time.time(); rng = np.random.default_rng(0)
X = np.empty((int(total * p_keep * 1.05) + 1000, 1536), dtype=np.float32); ys = []; tag = None; k = 0; seen = 0
reader = con.execute(q).fetch_record_batch(50_000)
while True:
    try: b = reader.read_next_batch()
    except StopIteration: break
    tag = tag or b.column("embed_model")[0].as_py()
    ages = b.column("age_days").to_numpy(zero_copy_only=False)
    emb = b.column("embedding"); vals = emb.values.to_numpy(zero_copy_only=False); offs = emb.offsets.to_numpy()
    keep = rng.random(len(ages)) < p_keep
    for i in np.nonzero(keep)[0]:
        if k >= len(X): break
        X[k] = vals[offs[i]:offs[i + 1]][:1536]; ys.append(min(float(ages[i]), CAP_DAYS)); k += 1
    seen += len(ages)
    print(f"\r  scanned {seen:,}/{total:,}, kept {k:,}", end="", flush=True)
print()
con.close()  # release DuckDB's buffers before the solve
X = X[:k]; y = np.log1p(np.asarray(ys, dtype=np.float64)); N, D = X.shape
X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
print(f"{N:,} rows in {time.time()-t0:.0f}s; median age {np.expm1(np.median(y)):.0f}d, mean {np.expm1(y).mean():.0f}d")

idx = rng.permutation(N); cut = int(N * 0.9); tr, te = idx[:cut], idx[cut:]
mu_y = y[tr].mean()
base_mae = np.mean(np.abs(y[te] - mu_y))
print(f"baseline (predict global mean): holdout MAE {base_mae:.3f} log-days")

# --- ridge head ---
def ridge(lam):
    A = X[tr].T @ X[tr] + lam * np.eye(D)
    w = np.linalg.solve(A, X[tr].T @ (y[tr] - mu_y))
    mae = np.mean(np.abs(X[te] @ w + mu_y - y[te]))
    return w, mae
best_r = None
for lam in (1, 3, 10, 30):
    w, mae = ridge(lam)
    print(f"  ridge lambda {lam:>3}: holdout MAE {mae:.3f}")
    if best_r is None or mae < best_r[1]: best_r = (w, mae, lam)

# --- small MLP head (1536 -> 64 -> 1), numpy Adam ---
H = 64; np.random.seed(0)
W1 = (np.random.randn(D, H) * (1 / np.sqrt(D))).astype(np.float32); b1 = np.zeros(H, dtype=np.float32)
W2 = (np.random.randn(H) * (1 / np.sqrt(H))).astype(np.float32); b2 = float(mu_y)
mW1 = np.zeros_like(W1); vW1 = np.zeros_like(W1); mb1 = np.zeros_like(b1); vb1 = np.zeros_like(b1)
mW2 = np.zeros_like(W2); vW2 = np.zeros_like(W2); mb2 = vb2 = 0.0
lr, beta1, beta2, eps, step = 3e-4, 0.9, 0.999, 1e-8, 0
Xtr32 = X[tr]; ytr = y[tr].astype(np.float32)
for epoch in range(3):
    order = rng.permutation(len(tr))
    for s in range(0, len(tr), 4096):
        bi = order[s:s + 4096]; xb = Xtr32[bi]; yb = ytr[bi]; step += 1
        h = np.maximum(xb @ W1 + b1, 0); pred = h @ W2 + b2
        g = (pred - yb) / len(bi)                       # dMSE/2
        gW2 = h.T @ g; gb2 = g.sum()
        gh = np.outer(g, W2); gh[h <= 0] = 0
        gW1 = xb.T @ gh; gb1 = gh.sum(0)
        for P, gP, mP, vP in ((W1, gW1, mW1, vW1), (b1, gb1, mb1, vb1), (W2, gW2, mW2, vW2)):
            mP *= beta1; mP += (1 - beta1) * gP; vP *= beta2; vP += (1 - beta2) * gP * gP
            P -= lr * (mP / (1 - beta1 ** step)) / (np.sqrt(vP / (1 - beta2 ** step)) + eps)
        mb2 = beta1 * mb2 + (1 - beta1) * gb2; vb2 = beta2 * vb2 + (1 - beta2) * gb2 * gb2
        b2 -= lr * (mb2 / (1 - beta1 ** step)) / (np.sqrt(vb2 / (1 - beta2 ** step)) + eps)
    hte = np.maximum(X[te] @ W1 + b1, 0); mae_m = np.mean(np.abs(hte @ W2 + b2 - y[te]))
    print(f"  mlp epoch {epoch + 1}: holdout MAE {mae_m:.3f}")

out = os.path.join(work, "web", "age-model.json"); os.makedirs(os.path.dirname(out), exist_ok=True)
common = {"recipe": tag, "dims": D, "n": int(N), "target": "log1p(age days of open posting)",
          "cap_days": CAP_DAYS, "baseline_mae": float(base_mae), "trained_at": int(time.time() * 1000)}
if mae_m < best_r[1]:
    model = {**common, "kind": "mlp", "hidden": H, "W1": np.round(W1, 5).tolist(), "b1": np.round(b1, 5).tolist(),
             "W2": np.round(W2, 5).tolist(), "b2": float(b2), "holdout": {"mae_log": float(mae_m)}}
    win = ("mlp", mae_m)
else:
    model = {**common, "kind": "ridge", "w": [round(float(v), 6) for v in best_r[0]], "b": float(mu_y),
             "lambda": best_r[2], "holdout": {"mae_log": float(best_r[1])}}
    win = ("ridge", best_r[1])
json.dump(model, open(out, "w", encoding="utf-8"))
print(f"wrote {out}: {win[0]} wins, holdout MAE {win[1]:.3f} log-days (baseline {base_mae:.3f}) "
      f"≈ typical error x{np.exp(win[1]):.2f} in age")

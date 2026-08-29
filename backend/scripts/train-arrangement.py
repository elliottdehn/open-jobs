# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow", "scipy"]
# ///
"""Work-arrangement estimator: multinomial logistic regression from the job embedding (1536-d) to
{remote, hybrid, onsite}, trained on postings whose arrangement tools/locparse.py can state from the
location/title/JD. Writes <EXPORT_DIR>/web/arrangement-model.json {recipe, classes, W[3][1536], b[3],
n, holdout:{accuracy, per_class}} — the client applies softmax(W·v + b) to the *unknown* rest and
shows the top class as an estimate when its probability clears a threshold.
Run: EXPORT_DIR=export/latest uv run scripts/train-arrangement.py   (SAMPLE env: rows to scan, default 250000)"""
import json, os, sys, time
import numpy as np, duckdb
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
from locparse import parse

root = os.path.join(os.path.dirname(__file__), "..", os.environ.get("EXPORT_DIR", "export/latest"))
J = os.path.join(root, "jobs", "*.parquet")
SAMPLE = int(os.environ.get("SAMPLE", "250000"))
con = duckdb.connect(); con.execute("SET threads=4"); con.execute("SET memory_limit='8GB'"); con.execute("SET arrow_large_buffer_size=true")
q = f"""SELECT embed_model, title, location, content, embedding FROM read_parquet('{J}')
        WHERE is_open AND embed_status='done' AND embedding IS NOT NULL USING SAMPLE {SAMPLE} ROWS"""
CLASSES = ["remote", "hybrid", "onsite"]
cache = os.path.join(root, "web", "arrangement-train.npz")
t0 = time.time(); X = []; y = []; seen = 0; tag = None; counts = {c: 0 for c in CLASSES + ["unknown"]}
if os.path.exists(cache) and not os.environ.get("RESCAN"):
    z = np.load(cache, allow_pickle=True); X = list(z["X"]); y = list(z["y"]); seen = int(z["seen"]); tag = str(z["tag"]); counts = json.loads(str(z["counts"]))
    print(f"loaded cached training matrix {cache} ({len(y):,} rows); set RESCAN=1 to rebuild", file=sys.stderr)
reader = None if y else con.execute(q).fetch_record_batch(10_000)
while reader is not None:
    try: b = reader.read_next_batch()
    except StopIteration: break
    tag = tag or b.column("embed_model")[0].as_py()
    titles = b.column("title").to_pylist(); locs = b.column("location").to_pylist(); contents = b.column("content").to_pylist()
    emb = b.column("embedding"); vals = emb.values.to_numpy(zero_copy_only=False); offs = emb.offsets.to_numpy()
    for i in range(len(titles)):
        seen += 1
        lab = parse(locs[i] or "", (contents[i] or "")[:12000], titles[i] or "")["remote"]
        counts[lab] += 1
        if lab == "unknown": continue
        X.append(vals[offs[i]:offs[i + 1]].astype(np.float32)); y.append(CLASSES.index(lab))
    print(f"\r  scanned {seen:,} jobs: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()), end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
X = np.stack(X); y = np.asarray(y); N, D = X.shape; K = len(CLASSES)
if reader is not None: np.savez(cache, X=X, y=y, seen=seen, tag=tag, counts=json.dumps(counts))
X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
print(f"{N:,} labelled rows from {seen:,} jobs ({N/seen:.1%} labelled; unknown {counts['unknown']/seen:.1%}) in {time.time()-t0:.0f}s · " + ", ".join(f"{c} {counts[c]:,}" for c in CLASSES))
rng = np.random.default_rng(0); idx = rng.permutation(N); cut = int(N * 0.9); tr, te = idx[:cut], idx[cut:]
Y = np.eye(K)[y]

def fit(Xa, Ya, lam):
    """softmax regression with L2, solved by L-BFGS (converges properly; plain GD underfits badly here)"""
    from scipy.optimize import minimize
    n = len(Xa)
    def f(theta):
        W = theta[:D * K].reshape(D, K); b = theta[D * K:]
        Z = Xa @ W + b; Z -= Z.max(1, keepdims=True); LSE = np.log(np.exp(Z).sum(1)); P = np.exp(Z - LSE[:, None])
        loss = -(Z[np.arange(n), Ya.argmax(1)] - LSE).mean() + 0.5 * lam * (W * W).sum() / n
        G = (P - Ya) / n
        return loss, np.concatenate([(Xa.T @ G + lam * W / n).ravel(), G.sum(0)])
    theta0 = np.concatenate([np.zeros(D * K), np.log(Ya.mean(0) + 1e-9)])
    r = minimize(f, theta0, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return r.x[:D * K].reshape(D, K), r.x[D * K:]
def predict(W, b, Xa):
    Z = Xa @ W + b; Z -= Z.max(1, keepdims=True); P = np.exp(Z); return P / P.sum(1, keepdims=True)

best = None
for lam in (0.01, 0.1, 1):
    W, b = fit(X[tr], Y[tr], lam); P = predict(W, b, X[te]); pred = P.argmax(1); acc = float((pred == y[te]).mean())
    conf = P.max(1); acc70 = float((pred[conf >= 0.7] == y[te][conf >= 0.7]).mean()); cov70 = float((conf >= 0.7).mean())
    print(f"  lambda {lam:>4}: holdout accuracy {acc:.1%} · at p≥0.7: {acc70:.1%} accurate on {cov70:.0%} of rows")
    if best is None or acc > best[0]: best = (acc, lam)
W, b = fit(X, Y, best[1]); P = predict(W, b, X[te]); pred = P.argmax(1)
per = {}
for k, c in enumerate(CLASSES):
    tp = int(((pred == k) & (y[te] == k)).sum()); fp = int(((pred == k) & (y[te] != k)).sum()); fn = int(((pred != k) & (y[te] == k)).sum())
    per[c] = {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1), "support": int((y[te] == k).sum())}
out = os.path.join(root, "web", "arrangement-model.json"); os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({"recipe": tag, "dims": D, "classes": CLASSES, "W": [[round(float(v), 5) for v in W[:, k]] for k in range(K)], "b": [float(v) for v in b],
           "n": int(N), "lambda": best[1], "label_share": {c: counts[c] / seen for c in CLASSES + ["unknown"]},
           "holdout": {"accuracy": float(best[0]), "per_class": per}, "threshold": 0.7, "trained_at": int(time.time() * 1000)}, open(out, "w", encoding="utf-8"))
print(f"wrote {out}: n={N:,}, lambda={best[1]}, holdout accuracy {best[0]:.1%} · " + " · ".join(f"{c}: P {per[c]['precision']:.0%} R {per[c]['recall']:.0%}" for c in CLASSES))

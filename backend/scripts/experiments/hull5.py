# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "umap-learn"]
# ///
"""Representative samples per group via convex hull in a 5-D UMAP embedding.
For each leaf: hull vertex count (cardinality), then medoid + farthest-point-sampled hull vertices
as the card exemplars. Usage: uv run scripts/experiments/hull5.py export-full/greenhouse.ndjson [max_jobs]"""
import json, sys, time, collections, re, warnings
import numpy as np
from scipy.spatial import ConvexHull
warnings.filterwarnings("ignore")
path = sys.argv[1]; cap = int(sys.argv[2]) if len(sys.argv) > 2 else 60_000
rows, X = [], []
with open(path, "rb") as f:
    for raw in f:
        if not raw.endswith(b"\n"): break
        b = json.loads(raw)
        for j in b["jobs"]:
            if j.get("embedStatus") == "done" and (j.get("embedModel") or "").endswith("v3") and j.get("embedding"):
                rows.append((b["slug"], j["title"], j.get("location") or "")); X.append(j["embedding"])
                if len(X) >= cap: break
        if len(X) >= cap: break
X = np.asarray(X, dtype=np.float32); X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
N, D = X.shape; print(f"{N:,} vectors")
# exact-duplicate collapse (same posting at many locations): hull cardinality in full-d is #distinct
key = np.round(X[:, :64] * 1000).astype(np.int32)
_, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
print(f"distinct vectors (64-dim fingerprint): {len(cnt):,} of {N:,} ({(cnt>1).sum():,} duplicate clusters, largest {cnt.max()})")
# tree (same as hull.py)
rng = np.random.default_rng(0)
mu = X.mean(0); _, S, Vt = np.linalg.svd(X[rng.choice(N, min(N, 30000), replace=False)] - mu, full_matrices=False)
Z = (X - mu) @ Vt[:256].T
LEAF_MAX, LEAF_R = 400, 0.30
nodes = []; order = np.empty(N, dtype=np.int64); pos = 0
def two_means(idx):
    r = np.random.default_rng(len(idx)); c = Z[r.choice(idx, 2, replace=False)].copy()
    for _ in range(6):
        lab = ((Z[idx] - c[1]) ** 2).sum(1) < ((Z[idx] - c[0]) ** 2).sum(1)
        for k, m in ((0, ~lab), (1, lab)):
            if m.any(): c[k] = Z[idx[m]].mean(0)
    return lab
def build(idx, depth=0):
    global pos
    cen = X[idx].mean(0); cen /= np.linalg.norm(cen) + 1e-9; d = 1 - X[idx] @ cen
    me = len(nodes); nodes.append({"lo": pos, "hi": None, "cen": cen, "r": float(d.max()), "children": []})
    if len(idx) > LEAF_MAX and d.max() > LEAF_R and depth < 40:
        lab = two_means(idx); a, b = idx[~lab], idx[lab]
        if len(a) and len(b): nodes[me]["children"] = [build(a, depth + 1), build(b, depth + 1)]; nodes[me]["hi"] = pos; return me
    order[pos:pos + len(idx)] = idx; pos += len(idx); nodes[me]["hi"] = pos; return me
build(np.arange(N)); leaves = [n for n in nodes if not n["children"]]
print(f"{len(leaves)} leaves")
# UMAP to 5-D (global, so leaves are comparable)
import umap
t = time.time()
U = umap.UMAP(n_components=5, n_neighbors=15, min_dist=0.1, metric="cosine", random_state=0).fit_transform(X)
print(f"UMAP-5 in {time.time()-t:.0f}s")
STOP = set("and or of the for a in to with at on & senior sr jr ii iii".split())
def lab(idx):
    c = collections.Counter(w for r in idx for w in re.findall(r"[a-z][a-z+#]+", rows[r][1].lower()) if w not in STOP and len(w) > 2)
    return " ".join(w for w, _ in c.most_common(4))
def fps(pts, k, seed_idx):
    """farthest-point sampling over rows of pts starting from seed_idx; returns indices into pts"""
    chosen = [seed_idx]; d = np.linalg.norm(pts - pts[seed_idx], axis=1)
    for _ in range(k - 1):
        i = int(np.argmax(d)); chosen.append(i); d = np.minimum(d, np.linalg.norm(pts - pts[i], axis=1))
    return chosen
card = []; frac = []; samples = {}
for li, n in enumerate(leaves):
    idx = order[n["lo"]:n["hi"]]
    P = U[idx]
    # dedupe near-identical points for qhull stability
    Pu, uinv = np.unique(np.round(P, 4), axis=0, return_inverse=True)
    if len(Pu) < 7: verts = np.arange(len(Pu))
    else:
        try: verts = ConvexHull(Pu, qhull_options="QJ").vertices
        except Exception: verts = np.arange(len(Pu))
    card.append(len(verts)); frac.append(len(verts) / len(idx))
    # map hull vertices back to member rows (first member of each unique point)
    first = {u: i for i, u in reversed(list(enumerate(uinv)))}
    vrows = np.array([idx[first[v]] for v in verts])
    med = idx[int(np.argmax(X[idx] @ n["cen"]))]
    # exemplars: medoid + 5 farthest-point vertices (seeded at the vertex nearest the medoid)
    vp = U[vrows]; seed = int(np.argmin(np.linalg.norm(vp - U[med], axis=1)))
    pick = [med] + [int(vrows[i]) for i in fps(vp, min(6, len(vrows)), seed)[1:]]
    samples[li] = pick
card = np.array(card); frac = np.array(frac)
pct = lambda a, f="{:.0f}": " / ".join(f.format(np.percentile(a, q)) for q in (10, 50, 90))
print(f"hull cardinality in UMAP-5 per leaf p10/50/90: {pct(card)} vertices  (as fraction of members: {pct(frac, '{:.2f}')})")
print("\nrepresentative samples (medoid, then hull extremes by farthest-point):")
for li in list(np.argsort(card))[:1] + list(np.argsort(card))[len(card)//2:len(card)//2+2] + list(np.argsort(card))[-1:]:
    n = leaves[li]; idx = order[n["lo"]:n["hi"]]
    print(f"\n[{lab(idx)}]  n={len(idx)}  hull={card[li]}  r={n['r']:.2f}")
    for k, r in enumerate(samples[li]): print(f"   {'medoid ' if k == 0 else 'extreme'}  {rows[r][1][:60]:60s} | {rows[r][0][:18]:18s} | {rows[r][2][:24]}")

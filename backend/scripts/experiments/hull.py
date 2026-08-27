# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""How big / how separated are the tree's groups? Reads one export NDJSON (possibly still being
written; incomplete last line is skipped), builds the recursive-bisection tree, and reports per-leaf
extent (radius, 95th-pct distance), overlap (purity vs. nearest other centroid, radius-sum overlap),
and intrinsic dimensionality. Usage: uv run scripts/experiments/hull.py export-full/greenhouse.ndjson [max_jobs]"""
import json, sys, time, collections, re
import numpy as np
path = sys.argv[1]; cap = int(sys.argv[2]) if len(sys.argv) > 2 else 200_000
rows, X = [], []
with open(path, "rb") as f:
    for raw in f:
        if not raw.endswith(b"\n"): break  # partial trailing line of a file still being written
        b = json.loads(raw)
        for j in b["jobs"]:
            if j.get("embedStatus") == "done" and (j.get("embedModel") or "").endswith("v3") and j.get("embedding"):
                rows.append((b["slug"], j["title"], j.get("location") or "")); X.append(j["embedding"])
                if len(X) >= cap: break
        if len(X) >= cap: break
X = np.asarray(X, dtype=np.float32); X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
N, D = X.shape; print(f"{N:,} v3 vectors from {path}")
rng = np.random.default_rng(0)
mu = X[rng.choice(N, min(N, 30000), replace=False)].mean(0)
_, S, Vt = np.linalg.svd(X[rng.choice(N, min(N, 30000), replace=False)] - mu, full_matrices=False)
P = Vt[:256].T; Z = (X - mu) @ P
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
    cen = X[idx].mean(0); cen /= np.linalg.norm(cen) + 1e-9
    d = 1 - X[idx] @ cen
    me = len(nodes); nodes.append({"lo": pos, "hi": None, "cen": cen, "r": float(d.max()), "p95": float(np.percentile(d, 95)), "mean": float(d.mean()), "depth": depth, "children": []})
    if len(idx) > LEAF_MAX and d.max() > LEAF_R and depth < 40:
        lab = two_means(idx); a, b = idx[~lab], idx[lab]
        if len(a) and len(b):
            nodes[me]["children"] = [build(a, depth + 1), build(b, depth + 1)]; nodes[me]["hi"] = pos; return me
    order[pos:pos + len(idx)] = idx; pos += len(idx); nodes[me]["hi"] = pos; return me
t = time.time(); build(np.arange(N))
leaves = [n for n in nodes if not n["children"]]
print(f"tree: {len(nodes)} nodes, {len(leaves)} leaves, depth {max(n['depth'] for n in nodes)}, {time.time()-t:.0f}s")
sizes = np.array([n["hi"] - n["lo"] for n in leaves]); R = np.array([n["r"] for n in leaves]); P95 = np.array([n["p95"] for n in leaves]); M = np.array([n["mean"] for n in leaves])
pct = lambda a: " / ".join(f"{np.percentile(a, q):.2f}" for q in (10, 50, 90))
print(f"leaf size p10/50/90: {' / '.join(str(int(np.percentile(sizes,q))) for q in (10,50,90))}")
print(f"extent (cosine dist to centroid): mean {pct(M)} | p95 {pct(P95)} | max(radius) {pct(R)}")
# overlap: purity = fraction of members whose own centroid is nearest among all leaf centroids
C = np.stack([n["cen"] for n in leaves]); L = len(leaves)
own = np.empty(N, dtype=np.int64)
for li, n in enumerate(leaves): own[order[n["lo"]:n["hi"]]] = li
sims = X @ C.T; nearest = sims.argmax(1); purity = (nearest == own).mean()
per_leaf_purity = np.array([(nearest[order[n["lo"]:n["hi"]]] == li).mean() for li, n in enumerate(leaves)])
print(f"purity (member's nearest centroid is its own leaf): overall {purity:.2f}; per-leaf p10/50/90 {pct(per_leaf_purity)}")
# pairwise: do leaf balls overlap? dist(c_i,c_j) < r_i + r_j ; also nearest-neighbour centroid distance vs radius
cd = 1 - C @ C.T; np.fill_diagonal(cd, np.inf)
nn = cd.min(1); overlap_pairs = ((cd < (R[:, None] + R[None, :])).sum() / 2)
print(f"nearest other centroid: dist p10/50/90 {pct(nn)}; ratio nn/radius p10/50/90 {pct(nn / R)}")
print(f"ball-overlap pairs (dist < r_i + r_j): {int(overlap_pairs):,} of {L*(L-1)//2:,} pairs ({overlap_pairs/(L*(L-1)/2):.1%}) — balls are generous; purity is the better separation measure")
# intrinsic dimensionality inside leaves: PCA components for 90% variance (sample of leaves)
dims = []
for n in [leaves[i] for i in rng.choice(L, min(L, 60), replace=False)]:
    V = X[order[n["lo"]:n["hi"]]]; V = V - V.mean(0)
    s = np.linalg.svd(V, compute_uv=False) ** 2; cs = np.cumsum(s) / s.sum(); dims.append(int(np.searchsorted(cs, 0.9)) + 1)
print(f"intrinsic dim (PCA comps for 90% var) per leaf p10/50/90: {pct(np.array(dims))} (whole corpus: {int(np.searchsorted(np.cumsum(S**2)/np.sum(S**2), 0.9))+1} of 256)")
# a few extremes
STOP = set("and or of the for a in to with at on & senior sr jr ii iii".split())
def lab(n):
    c = collections.Counter(w for r in order[n["lo"]:n["hi"]] for w in re.findall(r"[a-z][a-z+#]+", rows[r][1].lower()) if w not in STOP and len(w) > 2)
    return " ".join(w for w, _ in c.most_common(4))
oi = np.argsort(R)
print("\ntightest leaves:"); [print(f"  r={leaves[i]['r']:.2f} n={sizes[i]:4d} purity={per_leaf_purity[i]:.2f}  {lab(leaves[i])}") for i in oi[:4]]
print("widest leaves:"); [print(f"  r={leaves[i]['r']:.2f} n={sizes[i]:4d} purity={per_leaf_purity[i]:.2f}  {lab(leaves[i])}") for i in oi[-4:]]
print("least pure leaves:"); [print(f"  r={leaves[i]['r']:.2f} n={sizes[i]:4d} purity={per_leaf_purity[i]:.2f}  {lab(leaves[i])}") for i in np.argsort(per_leaf_purity)[:4]]

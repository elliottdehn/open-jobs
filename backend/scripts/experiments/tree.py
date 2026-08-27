# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""Experiment: recursive-bisection tree over job embeddings -> DFS row order + ball-tree sidecar.
Evaluates nearest-first retrieval (recall@k vs. row groups fetched) against brute force."""
import json, glob, sys, time, heapq
import numpy as np

ATS = ["crelate","gohire","icims","jobscore","pinpoint","recruiterbox","taleo"]
rows, X = [], []
for a in ATS:
    for l in open(f"export/{a}.ndjson"):
        b = json.loads(l)
        for j in b["jobs"]:
            if j.get("embedStatus") == "done" and (j.get("embedModel") or "").endswith("v3") and j.get("embedding"):
                rows.append((a, b["slug"], j["id"], j["title"], j.get("location") or "")); X.append(j["embedding"])
X = np.asarray(X, dtype=np.float32); X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
N, D = X.shape
print(f"{N} v3 vectors, {D} dims")

# --- PCA to 256 for splitting (fit on all; it's small here) ---
t = time.time()
mu = X.mean(0); Xc = X - mu
U, S, Vt = np.linalg.svd(Xc[np.random.default_rng(0).choice(N, min(N, 20000), replace=False)], full_matrices=False)
P = Vt[:256].T                      # D x 256
Z = Xc @ P
print(f"PCA-256 in {time.time()-t:.1f}s, explained {S[:256].var() and (S[:256]**2).sum()/(S**2).sum():.2f}")

# --- recursive 2-means bisection ---
LEAF_MAX = 500; LEAF_RADIUS = 0.35  # stop splitting when small or tight (cosine distance radius in full space)
nodes = []  # dict(lo, hi, centroid, radius, children)
order = np.empty(N, dtype=np.int64); pos = 0
def two_means(idx, iters=8):
    rng = np.random.default_rng(len(idx))
    c = Z[rng.choice(idx, 2, replace=False)]
    for _ in range(iters):
        d = ((Z[idx, None, :] - c[None]) ** 2).sum(-1); lab = d.argmin(1)
        for k in (0, 1):
            m = lab == k
            if m.any(): c[k] = Z[idx[m]].mean(0)
    return lab
def build(idx, depth=0):
    global pos
    node = {"lo": pos, "hi": None, "depth": depth}
    cen = X[idx].mean(0); cen /= np.linalg.norm(cen) + 1e-9
    rad = float((1 - X[idx] @ cen).max())
    node["centroid"] = cen; node["radius"] = rad; node["children"] = []
    nodes.append(node); me = len(nodes) - 1
    if len(idx) <= LEAF_MAX or rad <= LEAF_RADIUS or depth > 40:
        order[pos:pos+len(idx)] = idx; pos += len(idx)
    else:
        lab = two_means(idx)
        a, b = idx[lab == 0], idx[lab == 1]
        if len(a) == 0 or len(b) == 0:
            order[pos:pos+len(idx)] = idx; pos += len(idx)
        else:
            node["children"] = [build(a, depth+1), build(b, depth+1)]
    node["hi"] = pos
    return me
t = time.time(); root = build(np.arange(N))
leaves = [n for n in nodes if not n["children"]]
sizes = np.array([n["hi"]-n["lo"] for n in leaves])
print(f"tree: {len(nodes)} nodes, {len(leaves)} leaves in {time.time()-t:.1f}s; leaf size min/median/max {sizes.min()}/{int(np.median(sizes))}/{sizes.max()}; depth max {max(n['depth'] for n in nodes)}")
Xo = X[order]                                   # DFS row order
ROW_GROUP = 1000
def groups_for(lo, hi): return set(range(lo // ROW_GROUP, (hi - 1) // ROW_GROUP + 1))

# --- retrieval: best-first descent with ball bounds, fetch leaves nearest-first ---
def descend(q, k, max_groups):
    """Yield leaves in lower-bound order; stop when max_groups distinct row groups touched."""
    pq = [(max(0.0, 1 - float(q @ nodes[root]["centroid"]) - nodes[root]["radius"]), root)]
    touched, got = set(), []
    while pq:
        lb, n = heapq.heappop(pq)
        node = nodes[n]
        if node["children"]:
            for c in node["children"]:
                cn = nodes[c]; heapq.heappush(pq, (max(0.0, 1 - float(q @ cn["centroid"]) - cn["radius"]), c))
        else:
            g = groups_for(node["lo"], node["hi"])
            if len(touched | g) > max_groups: break
            touched |= g; got.append((node["lo"], node["hi"]))
    return got, touched
rng = np.random.default_rng(1)
qs = rng.choice(N, 200, replace=False)
for budget in (2, 5, 10, 20):
    rec = []
    for qi in qs:
        q = X[qi]
        truth = set(np.argpartition(-(X @ q), 100)[:100])
        leaves_got, touched = descend(q, 100, budget)
        cand = np.concatenate([order[lo:hi] for lo, hi in leaves_got]) if leaves_got else np.array([], dtype=int)
        top = cand[np.argsort(-(X[cand] @ q))[:100]] if len(cand) else cand
        rec.append(len(truth & set(top)) / 100)
    print(f"budget {budget:2d} row groups ({budget*ROW_GROUP*6/1000:.0f} MB @6KB/row): recall@100 = {np.mean(rec):.2f}  (of {N//ROW_GROUP+1} groups)")
# locality of the DFS order itself: how many of a job's true top-20 sit within +-500 rows
inv = np.empty(N, dtype=np.int64); inv[order] = np.arange(N)
near = []
for qi in qs:
    truth = np.argpartition(-(X @ X[qi]), 21)[:21]
    near.append(np.mean(np.abs(inv[truth] - inv[qi]) <= 500))
print(f"DFS-order locality: {np.mean(near):.2f} of each job's top-20 neighbours are within ±500 rows")
# sample: show a query and its tree neighbourhood
qi = qs[0]; lo = max(0, inv[qi]-3); print("\nsample neighbourhood in stream order around:", rows[qi][3], "|", rows[qi][4])
for r in order[lo:lo+7]: print("   ", rows[r][3][:60], "|", rows[r][4][:30], "|", rows[r][0])

# --- characterize leaves: size, radius, ATS mix, top title words, medoid title ---
import re, collections
STOP = set("and or of the for a in to with at on - & / | , ( ) senior sr jr ii iii".split())
print("\n=== leaves in DFS order ===")
for li, n in enumerate(leaves):
    idx = order[n["lo"]:n["hi"]]
    cen = n["centroid"]; med = idx[np.argmax(X[idx] @ cen)]
    words = collections.Counter()
    ats = collections.Counter(); locs = collections.Counter()
    for r in idx:
        a, slug, _id, title, loc = rows[r]
        ats[a] += 1; locs[loc.split(",")[-1].strip()[:14] or "?"] += 1
        for w in re.findall(r"[a-z][a-z+#]+", title.lower()):
            if w not in STOP: words[w] += 1
    top = " ".join(w for w, _ in words.most_common(5))
    print(f"L{li:02d} n={len(idx):4d} r={n['radius']:.2f} d={n['depth']} | {top:45s} | ats {ats.most_common(2)} | loc {locs.most_common(2)}")
    print(f"      medoid: {rows[med][3][:70]}")

# --- Matryoshka view: cut the binary tree at radius thresholds into nested groups ---
def label(idx):
    words = collections.Counter()
    for r in idx:
        for w in re.findall(r"[a-z][a-z+#]+", rows[r][3].lower()):
            if w not in STOP: words[w] += 1
    return " / ".join(w for w, _ in words.most_common(4))
def cut(n, thresholds, level=0, indent=""):
    node = nodes[n]; idx = order[node["lo"]:node["hi"]]
    print(f"{indent}L{level} n={len(idx):5d} r={node['radius']:.2f}  {label(idx)}")
    if level + 1 >= len(thresholds) or not node["children"]: return
    # descend until children are below the next threshold (or leaves)
    frontier = [c for c in node["children"]]
    out = []
    while frontier:
        c = frontier.pop()
        if nodes[c]["radius"] <= thresholds[level + 1] or not nodes[c]["children"]: out.append(c)
        else: frontier.extend(nodes[c]["children"])
    for c in sorted(out, key=lambda c: nodes[c]["lo"]): cut(c, thresholds, level + 1, indent + "    ")
print("\n=== nested groups (radius cuts 0.62 / 0.45 / 0.30) ===")
LEVELS = [9.0, 0.62, 0.45, 0.30]
top = [c for c in nodes[root]["children"]] if nodes[root]["children"] else [root]
frontier, l1 = top[:], []
while frontier:
    c = frontier.pop()
    if nodes[c]["radius"] <= LEVELS[1] or not nodes[c]["children"]: l1.append(c)
    else: frontier.extend(nodes[c]["children"])
for c in sorted(l1, key=lambda c: nodes[c]["lo"]): cut(c, LEVELS, 1)

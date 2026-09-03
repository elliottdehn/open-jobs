# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow", "zstandard"]
# ///
"""Build the local-first client data from export/jobs/*.parquet (must include embeddings):
  export/web/manifest.json     tree nodes {id, parent, lo, hi, radius, size, label, medoid, exemplars, children}
                               + recipe/dims/counts. Row ranges are into the DFS order.
  export/web/centroids.bin     float16 [nodes x dims] node centroids (unit vectors), same order as manifest.nodes
  export/web/groups/<leaf>.json  jobs of one leaf, DFS order, with exact float32 embeddings (base64)
Then `scripts/upload-web.sh` puts it in R2.  Run: uv run scripts/build-manifest.py [--leaf-max 400] [--leaf-radius 0.30]
"""
import argparse, base64, collections, glob, json, os, re, sys, time
import numpy as np, duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--leaf-max", type=int, default=400)
ap.add_argument("--leaf-radius", type=float, default=0.30)
ap.add_argument("--pca", type=int, default=256)
args = ap.parse_args()

root = os.path.join(os.path.dirname(__file__), "..", os.environ.get("EXPORT_DIR", "export"))  # EXPORT_DIR=export-slim for the vector-only pull
out = os.path.join(root, "web"); os.makedirs(os.path.join(out, "groups"), exist_ok=True)
J = os.path.join(root, "jobs", "*.parquet")
con = duckdb.connect()
con.execute("SET threads=4"); con.execute("SET memory_limit='6GB'"); con.execute("SET arrow_large_buffer_size=true")  # >2 GB of jd strings
t = time.time()
recipe = con.execute(f"SELECT embed_model, count(*) FROM read_parquet('{J}') WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
print("recipes:", recipe)
tag = recipe[0][0]
q = f"""SELECT ats, slug, id, coalesce(title,'') AS title, coalesce(location,'') AS location, coalesce(url,'') AS url,
               coalesce(json_extract_string(raw_json, '$.company_name'), '') AS company_hint,
               epoch_ms(first_seen_at) AS first_seen_ms,
               epoch_ms(published_at) AS published_ms,
               left(regexp_replace(regexp_replace(coalesce(content,''), '<[^>]+>', ' ', 'g'), '\\s+', ' ', 'g'), 4000) AS jd,
               json_extract(enrichment_json, '$.data') AS enrichment,
               embedding
        FROM read_parquet('{J}') WHERE is_open AND embed_status = 'done' AND embed_model = '{tag}'"""
# Load via Arrow: the FLOAT[] column comes back as a ListArray whose flat values view is a zero-copy
# float32 buffer -> reshape to (N, D). Metadata columns go through Python objects (small).
# Stream in record batches: vectors go straight into a preallocated float32 array (N x D), small
# columns become Python lists, and the big text columns (jd, enrichment) stay in Arrow until a group
# file needs them. Peak memory ≈ 4·N·D bytes + text, instead of ~3x that.
import pyarrow as pa
import zstandard
# jd text for 3M jobs is ~10 GB uncompressed; held raw (as an Arrow table) it stacked on the 18 GB
# vector matrix and got this process OOM-killed. Hold each row zstd-compressed (~3x smaller) and
# decompress only when a group file is written.
_zc = zstandard.ZstdCompressor(level=3); _zd = zstandard.ZstdDecompressor()
N = con.execute(q.replace("SELECT ats, slug, id, coalesce(title,'') AS title, coalesce(location,'') AS location, coalesce(url,'') AS url,", "SELECT count(*) FROM (SELECT ats, slug, id, coalesce(title,'') AS title, coalesce(location,'') AS location, coalesce(url,'') AS url,", 1) + ")").fetchone()[0]
D = 1536
X = np.empty((N, D), dtype=np.float32)
small = {c: [] for c in ("ats", "slug", "id", "title", "location", "url", "company_hint", "first_seen_ms", "published_ms")}
jd_z = []; enr_z = []
pos_ = 0
reader = con.execute(q).fetch_record_batch(50_000)
while True:
    try: b = reader.read_next_batch()
    except StopIteration: break
    emb = b.column("embedding")
    vals = emb.values.to_numpy(zero_copy_only=False)
    n = len(b)
    X[pos_:pos_ + n] = vals.reshape(n, -1)[:, :D]
    for c in small: small[c].extend(b.column(c).to_pylist())
    jd_z.extend(_zc.compress(t.encode()) if t else None for t in b.column("jd").to_pylist())
    enr_z.extend(_zc.compress(t.encode()) if t else None for t in b.column("enrichment").to_pylist())
    pos_ += n
    print(f"\r  loaded {pos_:,}/{N:,}", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
del reader
meta_rows = list(zip(small["ats"], small["slug"], small["id"], small["title"], small["location"], small["url"], small["company_hint"], small["first_seen_ms"], small["published_ms"]))
del small
# Clean + unit-normalize IN ROW BLOCKS: a whole-matrix np.linalg.norm materializes an X-sized x*x temp
# (~18 GB at 3M jobs), which doubled peak memory and got this process SIGKILLed once the corpus outgrew
# RAM. Blockwise keeps the temp at ~1 GB regardless of N.
for _i in range(0, X.shape[0], 200_000):
	_blk = X[_i:_i + 200_000]
	np.nan_to_num(_blk, copy=False)  # a handful of rows carry NaN/inf from bad decodes; zero them
	_blk /= np.sqrt((_blk * _blk).sum(axis=1, keepdims=True)) + 1e-9
del _blk
N, D = X.shape
print(f"loaded {N:,} vectors x {D} in {time.time()-t:.0f}s")

# company name per board from boards parquet (resolved), else slug
B = os.path.join(root, "boards", "*.parquet")
comp = dict(((a, s), n) for a, s, n in con.execute(f"SELECT ats, slug, company_name FROM read_parquet('{B}') WHERE company_name IS NOT NULL").fetchall())
compfull = dict(((a, s), {"name": n, "website": w, "industry": i, "size": z, "hq": h, "staffing": st, "desc": d}) for a, s, n, w, i, z, h, st, d in con.execute(f"SELECT ats, slug, company_name, company_website, company_industry, company_size_bucket, company_hq_country, company_is_staffing_agency, company_description FROM read_parquet('{B}') WHERE company_name IS NOT NULL").fetchall())

# PCA for splitting
t = time.time()
rng = np.random.default_rng(0)
samp = X[rng.choice(N, min(N, 50_000), replace=False)]
mu = samp.mean(0)
_, _, Vt = np.linalg.svd(samp - mu, full_matrices=False)
P = Vt[: args.pca].T.astype(np.float32)
# project in row blocks: (X - mu) would materialize an X-sized temp (~18 GB at 3M jobs)
Z = np.empty((N, args.pca), dtype=np.float32)
for _i in range(0, N, 200_000):
	Z[_i:_i + 200_000] = (X[_i:_i + 200_000] - mu) @ P
print(f"PCA-{args.pca} in {time.time()-t:.0f}s")

def sims_to(idx, cen, chunk=200_000):
    """X[idx] @ cen without materializing X[idx] for huge nodes."""
    out = np.empty(len(idx), dtype=np.float32)
    for i in range(0, len(idx), chunk): out[i:i + chunk] = X[idx[i:i + chunk]] @ cen
    return out

# recursive bisection
nodes = []; order = np.empty(N, dtype=np.int64); pos = 0
def two_means(idx, iters=6, chunk=500_000):
    # chunked: Z[idx] for the root node is a multi-GB copy, and each iteration made several of them
    r = np.random.default_rng(len(idx) * 7919)
    c = Z[r.choice(idx, 2, replace=False)].copy()
    lab = np.empty(len(idx), dtype=bool)
    for _ in range(iters):
        for i in range(0, len(idx), chunk):
            zi = Z[idx[i:i + chunk]]
            lab[i:i + chunk] = ((zi - c[1]) ** 2).sum(1) < ((zi - c[0]) ** 2).sum(1)
        for k, m in ((0, ~lab), (1, lab)):
            if m.any():
                sel = idx[m]; acc = np.zeros(Z.shape[1], dtype=np.float64)
                for i in range(0, len(sel), chunk): acc += Z[sel[i:i + chunk]].sum(0, dtype=np.float64)
                c[k] = (acc / len(sel)).astype(np.float32)
    return lab
def build(idx, parent, depth):
    global pos
    cen = np.zeros(D, dtype=np.float64)
    for i in range(0, len(idx), 200_000): cen += X[idx[i:i + 200_000]].sum(0, dtype=np.float64)
    cen = (cen / len(idx)).astype(np.float32); cen /= np.linalg.norm(cen) + 1e-9
    rad = float((1 - sims_to(idx, cen)).max())
    me = len(nodes); nodes.append({"id": me, "parent": parent, "lo": pos, "hi": None, "radius": round(rad, 4), "depth": depth, "children": [], "_cen": cen})
    split = None
    if len(idx) > args.leaf_max and rad > args.leaf_radius and depth < 40:
        lab = two_means(idx); a, b = idx[~lab], idx[lab]
        if len(a) and len(b): split = (a, b)
    if split is None:
        order[pos:pos + len(idx)] = idx; pos += len(idx)
    else:
        nodes[me]["children"] = [build(split[0], me, depth + 1), build(split[1], me, depth + 1)]
    nodes[me]["hi"] = pos
    return me
t = time.time(); build(np.arange(N), None, 0)
leaves = [n for n in nodes if not n["children"]]
print(f"tree: {len(nodes)} nodes, {len(leaves)} leaves in {time.time()-t:.0f}s; leaf sizes median {int(np.median([n['hi']-n['lo'] for n in leaves]))}, max {max(n['hi']-n['lo'] for n in leaves)}")

# labels: top title words + medoid + exemplars
STOP = set("and or of the for a in to with at on & senior sr jr ii iii i lead staff associate assistant manager specialist".split())
def words(idx, k=4):
    c = collections.Counter()
    if len(idx) > 20000: idx = np.random.default_rng(len(idx)).choice(idx, 20000, replace=False)
    for r in idx:
        for w in re.findall(r"[a-z][a-z+#]+", meta_rows[r][3].lower()):
            if w not in STOP and len(w) > 2: c[w] += 1
    return [w for w, _ in c.most_common(k)]
def company(r):
    a, s = meta_rows[r][0], meta_rows[r][1]
    return comp.get((a, s)) or meta_rows[r][6] or s
def norm_title(t):
    return re.sub(r"[^a-z]+", " ", t.lower()).strip()

def sub_medoids(idx, k, rng_seed):
    """k-means (in PCA space) inside a group, on a sample for big groups; returns medoid row per
    sub-cluster, largest first."""
    r = np.random.default_rng(rng_seed)
    samp = idx if len(idx) <= 5000 else r.choice(idx, 5000, replace=False)
    Zi = Z[samp]
    c = Zi[r.choice(len(samp), k, replace=False)].copy()
    for _ in range(8):
        d = ((Zi[:, None, :] - c[None]) ** 2).sum(-1); lab = d.argmin(1)
        for j in range(k):
            m = lab == j
            if m.any(): c[j] = Zi[m].mean(0)
    out = []
    for j in np.argsort(-np.bincount(lab, minlength=k)):
        m = np.where(lab == j)[0]
        if len(m) == 0: continue
        sub = samp[m]; cen = X[sub].mean(0); cen /= np.linalg.norm(cen) + 1e-9
        out.append((int(len(m)), sub[int(np.argmax(X[sub] @ cen))]))
    return out

def exemplars_for(idx, cen, k=6):
    """Medoid of the group, then the medoids of its sub-clusters (typical job of each region inside the
    group), largest region first, skipping repeated titles (location-replicated postings)."""
    med = idx[int(np.argmax(sims_to(idx, cen)))]
    kk = max(2, min(8, len(idx) // 25))
    cand = [r for _, r in sub_medoids(idx, kk, len(idx))] if len(idx) >= 10 else list(idx)
    seen = {norm_title(meta_rows[med][3])}; ex = [med]
    for r in cand:
        t = norm_title(meta_rows[r][3])
        if t in seen: continue
        seen.add(t); ex.append(r)
        if len(ex) >= k: break
    return ex

for n in nodes:
    idx = order[n["lo"]:n["hi"]]
    ex_rows = exemplars_for(idx, n["_cen"])
    n["size"] = int(len(idx)); n["label"] = " · ".join(words(idx))
    n["exemplars"] = [{"title": meta_rows[r][3][:80], "company": company(r)[:40], "location": meta_rows[r][4][:40]} for r in ex_rows]
    n["medoid"] = meta_rows[ex_rows[0]][3][:80]
    samp = idx if len(idx) <= 20000 else np.random.default_rng(len(idx)).choice(idx, 20000, replace=False)
    n["distinct_titles"] = int(len({norm_title(meta_rows[r][3]) for r in samp}) * (len(idx) / len(samp)))

# outputs
C = np.stack([n["_cen"] for n in nodes]).astype(np.float16)
C.tofile(os.path.join(out, "centroids.bin"))
manifest = {
    "recipe": tag, "dims": D, "jobs": N, "nodes": len(nodes), "leaves": len(leaves),
    "built_at": int(time.time() * 1000), "pca": {"mu": mu.astype(float).round(5).tolist(), "components": None},
    "tree": [{k: v for k, v in n.items() if not k.startswith("_")} for n in nodes],
}
with open(os.path.join(out, "manifest.json"), "w") as f: json.dump(manifest, f)
# groups: one file per leaf, jobs in DFS order, int8 embeddings (per-vector scale)
t = time.time()
for n in leaves:
    idx = order[n["lo"]:n["hi"]]
    V = X[idx].astype(np.float32)  # exact vectors (unit length), float32 little-endian base64
    jobs = []
    for i, r in enumerate(idx):
        a, s, jid, title, loc, url, _, fs, pub = meta_rows[r]
        _j = jd_z[int(r)]; _e = enr_z[int(r)]
        jd = _zd.decompress(_j).decode() if _j else ""; enr = _zd.decompress(_e).decode() if _e else None
        jobs.append({"ats": a, "slug": s, "id": jid, "title": title, "company": company(r), "location": loc, "url": url, "seen": int(fs or 0), "pub": int(pub or 0), "jd": jd,
                     **({"e": json.loads(enr)} if enr else {}), **({"co_": compfull[(a, s)]} if (a, s) in compfull else {}),
                     "v": base64.b64encode(V[i].tobytes()).decode()})
    with open(os.path.join(out, "groups", f"{n['id']}.json"), "w") as f: json.dump({"leaf": n["id"], "lo": n["lo"], "hi": n["hi"], "jobs": jobs}, f)
size = sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, "groups", "*.json")))
print(f"wrote manifest ({os.path.getsize(os.path.join(out,'manifest.json'))/1e6:.1f} MB), centroids ({C.nbytes/1e6:.1f} MB), {len(leaves)} group files ({size/1e6:.0f} MB) in {time.time()-t:.0f}s")

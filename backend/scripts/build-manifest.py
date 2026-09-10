# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1", "pyarrow", "boto3"]
# ///
"""Build the local-first client data from export/jobs/*.parquet (must include embeddings):
  export/web/manifest.json     tree nodes {id, parent, lo, hi, radius, size, label, medoid, exemplars, children}
                               + recipe/dims/counts. Row ranges are into the DFS order.
  export/web/centroids.bin     float16 [nodes x dims] node centroids (unit vectors), same order as manifest.nodes
  export/web/groups/<leaf>.json  jobs of one leaf, DFS order, with exact float32 embeddings (base64)
With --publish each group file is uploaded to R2 as it is written; scripts/publish-web.py (finalize) reconciles and
publishes centroids + manifest. Run: uv run scripts/build-manifest.py [--leaf-max 400] [--leaf-radius 0.30] [--out DIR] [--publish]
"""
import argparse, base64, collections, glob, json, os, re, sys, time
import numpy as np, duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--leaf-max", type=int, default=400)
ap.add_argument("--leaf-radius", type=float, default=0.30)
ap.add_argument("--pca", type=int, default=256)
ap.add_argument("--out", help="output dir (default <WORK_DIR or EXPORT_DIR>/web)")
ap.add_argument("--publish", action="store_true", help="upload each group file to R2 (groups/<leaf>.json) as it is written; centroids + manifest are published by the finalize stage")
ap.add_argument("--groups-prefix", default=os.environ.get("GROUPS_PREFIX", "groups/"), help="R2 key prefix for --publish")
args = ap.parse_args()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2 import R2, Uploader
# EXPORT_DIR is the export to build from: a local dir, or an s3://bucket/exports/<date> prefix (read in place).
# WORK_DIR is local scratch (memmap, staging, DuckDB spill); defaults to EXPORT_DIR when that is local.
export_dir = os.environ.get("EXPORT_DIR", "export")
is_s3 = export_dir.startswith("s3://")
root = export_dir if is_s3 else os.path.join(os.path.dirname(__file__), "..", export_dir)
work = os.environ.get("WORK_DIR") or (os.path.join(os.path.dirname(__file__), "..", "work-" + export_dir.rstrip("/").rsplit("/", 1)[-1]) if is_s3 else root)
os.makedirs(work, exist_ok=True)
out = args.out or os.path.join(work, "web"); os.makedirs(os.path.join(out, "groups"), exist_ok=True)
# per-process temp names: two builds on the same export (a validation A/B) must not clobber each other's memmaps
TMP = f".build-{os.getpid()}"
con_tmp = os.path.join(work, f"{TMP}.duckdb_tmp"); os.makedirs(con_tmp, exist_ok=True)
J = f"{root.rstrip('/')}/jobs/*.parquet"
con = duckdb.connect()
r2 = R2() if (is_s3 or args.publish) else None
if is_s3: r2.duckdb(con)
uploader = Uploader(r2, workers=8) if args.publish else None
con.execute("SET threads=4"); con.execute("SET memory_limit='6GB'"); con.execute("SET arrow_large_buffer_size=true")  # >2 GB of jd strings
con.execute(f"SET temp_directory='{con_tmp}'"); con.execute("SET preserve_insertion_order=false")
t = time.time()
recipe = con.execute(f"SELECT embed_model, count(*) FROM read_parquet('{J}') WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
print("recipes:", recipe)
tag = recipe[0][0]
# first-party postings only in the search tree for now: the aggregator tier has no age curve of its own yet
# (exports before 2026-09-10 have no tier column at all)
_has_tier = any(r[0] == "tier" for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{J}', union_by_name=true)").fetchall())
WHERE_ = f"FROM read_parquet('{J}', union_by_name=true) WHERE is_open AND embed_status = 'done' AND embed_model = '{tag}'" + (" AND coalesce(tier, 'first_party') = 'first_party'" if _has_tier else "")
# The exact key string per row ties the vector-loading pass to the group-writing pass without relying on parquet
# scan order (which is not stable across queries). Not a hash: 3.1M keys produced one 64-bit collision on 2026-09-08.
HKEY = "ats || '/' || slug || '#' || id AS h"
q_load = f"""SELECT {HKEY}, ats, slug, coalesce(title,'') AS title, coalesce(location,'') AS location,
               coalesce(json_extract_string(raw_json, '$.company_name'), '') AS company_hint, embedding {WHERE_}"""
q_rows = f"""SELECT {HKEY}, ats, slug, id, coalesce(title,'') AS title, coalesce(location,'') AS location, coalesce(url,'') AS url,
               epoch_ms(first_seen_at) AS first_seen_ms, epoch_ms(published_at) AS published_ms,
               left(regexp_replace(regexp_replace(coalesce(content,''), '<[^>]+>', ' ', 'g'), '\\s+', ' ', 'g'), 4000) AS jd,
               json_extract(enrichment_json, '$.data') AS enrichment {WHERE_}"""
# Pass 1 (vectors): stream record batches; vectors go straight into a file-backed float16 array (N x D); the
# only per-row Python kept is what labels and exemplars need (title, location, company hint, board). Everything
# else a group file needs (id, url, dates, jd text, enrichment) is streamed back out of the parquet in DFS order
# by pass 2 below, so no text is ever held for the whole corpus.
import pyarrow as pa
N = con.execute(f"SELECT count(*) {WHERE_}").fetchone()[0]
D = 1536
_xpath = os.path.join(work, f"{TMP}.vectors.f16.npy")
if os.path.exists(_xpath): os.remove(_xpath)
# file-backed so memory pressure evicts pages instead of killing the build (two jetsam deaths on 2026-09-05)
X = np.lib.format.open_memmap(_xpath, mode="w+", dtype=np.float16, shape=(N, D))  # storage only; consumers compute f32/f64 per block
titles, locs, hints = [], [], []
board_ids = {}; board_of = np.empty(N, dtype=np.int32)
H = []  # key strings, as Arrow chunks (no per-row Python objects)
pos_ = 0
reader = con.execute(q_load).to_arrow_reader(50_000)
while True:
    try: b = reader.read_next_batch()
    except StopIteration: break
    emb = b.column("embedding")
    vals = emb.values.to_numpy(zero_copy_only=False)
    n = len(b)
    X[pos_:pos_ + n] = vals.reshape(n, -1)[:, :D]
    if (pos_ // 50_000) % 8 == 7: X.flush()  # dirty file-backed pages count as footprint until written back
    H.append(pa.array(b.column("h").to_pylist(), type=pa.string()))  # a copy: keeping the batch's own column pins the whole batch, vectors included (19 GB by the end of loading)
    for k, (a_, s_) in enumerate(zip(b.column("ats").to_pylist(), b.column("slug").to_pylist())):
        board_of[pos_ + k] = board_ids.setdefault((a_, s_), len(board_ids))
    titles.extend(b.column("title").to_pylist()); locs.extend(b.column("location").to_pylist()); hints.extend(b.column("company_hint").to_pylist())
    pos_ += n
    print(f"\r  loaded {pos_:,}/{N:,}", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
del reader
assert pos_ == N, f"loaded {pos_} rows, expected {N}"
boards = [None] * len(board_ids)
for k, i_ in board_ids.items(): boards[i_] = k
del board_ids
H = pa.chunked_array(H).combine_chunks()
if len(H) != N: sys.exit(f"keys {len(H)} != rows {N}")
# Clean + unit-normalize IN ROW BLOCKS: a whole-matrix np.linalg.norm materializes an X-sized x*x temp
# (~18 GB at 3M jobs), which doubled peak memory and got this process SIGKILLed once the corpus outgrew
# RAM. Blockwise keeps the temp at ~1 GB regardless of N.
for _i in range(0, X.shape[0], 200_000):
	_blk = X[_i:_i + 200_000]
	np.nan_to_num(_blk, copy=False)  # a handful of rows carry NaN/inf from bad decodes; zero them
	_b32 = _blk.astype(np.float32)
	_blk[:] = _b32 / (np.sqrt((_b32 * _b32).sum(axis=1, keepdims=True)) + 1e-9)
	del _b32
del _blk
X.flush()
N, D = X.shape
# Deterministic row order: sort every per-row array by key, so the build no longer depends on parquet scan order
# (which varies with column selection and thread count). Same export in, same tree out, run after run.
t2 = time.time()
import pyarrow.compute as pc
perm = pc.sort_indices(H).to_numpy()
H = H.take(pa.array(perm)); titles = [titles[i] for i in perm]; locs = [locs[i] for i in perm]; hints = [hints[i] for i in perm]; board_of = board_of[perm]
_x2 = os.path.join(work, f"{TMP}.vectors.f16.sorted.npy")
X2 = np.lib.format.open_memmap(_x2, mode="w+", dtype=np.float16, shape=(N, D))
for _i in range(0, N, 200_000):
    X2[_i:_i + 200_000] = X[perm[_i:_i + 200_000]]
    if (_i // 200_000) % 4 == 3: X2.flush()
X2.flush()
del X; os.remove(_xpath); X = X2; _xpath = _x2; del perm
print(f"loaded {N:,} vectors x {D} in {time.time()-t:.0f}s (key-sorted in {time.time()-t2:.0f}s)")
if os.environ.get("BUILD_MANIFEST_STOP_AFTER") == "load": print("stopping after load (BUILD_MANIFEST_STOP_AFTER)"); os.remove(_xpath); sys.exit(0)

# company name per board from boards parquet (resolved), else slug
B = f"{root.rstrip('/')}/boards/*.parquet"
comp = dict(((a, s), n) for a, s, n in con.execute(f"SELECT ats, slug, company_name FROM read_parquet('{B}') WHERE company_name IS NOT NULL").fetchall())
compfull = dict(((a, s), {"name": n, "website": w, "industry": i, "size": z, "hq": h, "staffing": st, "desc": d}) for a, s, n, w, i, z, h, st, d in con.execute(f"SELECT ats, slug, company_name, company_website, company_industry, company_size_bucket, company_hq_country, company_is_staffing_agency, company_description FROM read_parquet('{B}') WHERE company_name IS NOT NULL").fetchall())

# PCA for splitting
t = time.time()
rng = np.random.default_rng(0)
samp = X[rng.choice(N, min(N, 50_000), replace=False)].astype(np.float32)
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
        for w in re.findall(r"[a-z][a-z+#]+", titles[r].lower()):
            if w not in STOP and len(w) > 2: c[w] += 1
    return [w for w, _ in c.most_common(k)]
def company(r):
    a, s = boards[board_of[r]]
    return comp.get((a, s)) or hints[r] or s
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
        sub = samp[m]; cen = X[sub].mean(0, dtype=np.float32); cen /= np.linalg.norm(cen) + 1e-9
        out.append((int(len(m)), sub[int(np.argmax(X[sub] @ cen))]))
    return out

def exemplars_for(idx, cen, k=6):
    """Medoid of the group, then the medoids of its sub-clusters (typical job of each region inside the
    group), largest region first, skipping repeated titles (location-replicated postings)."""
    med = idx[int(np.argmax(sims_to(idx, cen)))]
    kk = max(2, min(8, len(idx) // 25))
    cand = [r for _, r in sub_medoids(idx, kk, len(idx))] if len(idx) >= 10 else list(idx)
    seen = {norm_title(titles[med])}; ex = [med]
    for r in cand:
        t = norm_title(titles[r])
        if t in seen: continue
        seen.add(t); ex.append(r)
        if len(ex) >= k: break
    return ex

for n in nodes:
    idx = order[n["lo"]:n["hi"]]
    ex_rows = exemplars_for(idx, n["_cen"])
    n["size"] = int(len(idx)); n["label"] = " · ".join(words(idx))
    n["exemplars"] = [{"title": titles[r][:80], "company": company(r)[:40], "location": locs[r][:40]} for r in ex_rows]
    n["medoid"] = titles[ex_rows[0]][:80]
    samp = idx if len(idx) <= 20000 else np.random.default_rng(len(idx)).choice(idx, 20000, replace=False)
    n["distinct_titles"] = int(len({norm_title(titles[r]) for r in samp}) * (len(idx) / len(samp)))

# outputs
C = np.stack([n["_cen"] for n in nodes]).astype(np.float16)
C.tofile(os.path.join(out, "centroids.bin"))
manifest = {
    "recipe": tag, "dims": D, "jobs": N, "nodes": len(nodes), "leaves": len(leaves),
    "groups": args.groups_prefix,   # where this build's group files live under /data/ (per-build prefix; readers must use it)
    "built_at": int(time.time() * 1000), "pca": {"mu": mu.astype(float).round(5).tolist(), "components": None},
    "tree": [{k: v for k, v in n.items() if not k.startswith("_")} for n in nodes],
}
with open(os.path.join(out, "manifest.json"), "w") as f: json.dump(manifest, f)
# Pass 2 (group files): the parquet is streamed back in DFS order. `assign` maps each row's key to its DFS
# position; DuckDB joins, sorts by position (spilling to temp_directory), and hands back record batches. Leaves are
# contiguous in that order, so a file is written the moment its last row arrives. Vectors come from X (the same
# float16-normalized values as before) so group files are byte-identical to the single-pass build.
t = time.time()
# Pass 2 is the memory peak of the whole build (DuckDB sorting ~30 GB of wide rows, plus batch conversion):
# the buffer cap makes the sort spill to temp_directory instead of growing (the key join + sort refuse to run
# under 4 GB; 6 GB works), fewer threads mean fewer concurrent sort partitions, and small batches bound the
# Python-side copy. Output is unaffected.
con.execute("SET memory_limit='6GB'"); con.execute("SET threads=2")
con.execute("CREATE TABLE assign (h VARCHAR, pos BIGINT)")
_assign = pa.table({"h": H.take(pa.array(order)), "pos": pa.array(np.arange(N, dtype=np.int64))})
con.register("assign_src", _assign); con.execute("INSERT INTO assign SELECT h, pos FROM assign_src"); con.unregister("assign_src"); del _assign
leaf_at = {n["lo"]: n for n in leaves}  # DFS position -> the leaf that starts there
cur = None; jobs = []; V = None; written = 0; seen_rows = 0
# One scan of the parquet writes the joined rows to a local staging dir partitioned by position chunk; each chunk
# is then sorted on its own. A single ORDER BY over the whole corpus needs more buffer than the sort can spill
# (DuckDB ran out at 5.5 GiB); sorting 250k rows at a time never does.
import shutil
CHUNK = 250_000; stage = os.path.join(work, f"{TMP}.stage"); shutil.rmtree(stage, ignore_errors=True)
con.execute(f"""COPY (SELECT a.pos, (a.pos // {CHUNK})::INTEGER AS chunk, j.* EXCLUDE (h) FROM ({q_rows}) j JOIN assign a USING (h))
  TO '{stage}' (FORMAT PARQUET, PARTITION_BY (chunk), COMPRESSION ZSTD)""")
print(f"  staged {N:,} rows in {(N + CHUNK - 1) // CHUNK} chunks, {time.time()-t:.0f}s", file=sys.stderr, flush=True)
def _batches():
    for k in range((N + CHUNK - 1) // CHUNK):
        r = con.execute(f"SELECT * FROM read_parquet('{stage}/chunk={k}/*.parquet', hive_partitioning=false) ORDER BY pos").to_arrow_reader(5_000)
        while True:
            try: yield r.read_next_batch()
            except StopIteration: break
for b in _batches():
    cols = {c: b.column(c).to_pylist() for c in ("pos", "ats", "slug", "id", "title", "location", "url", "first_seen_ms", "published_ms", "jd", "enrichment")}
    for k in range(len(b)):
        p = cols["pos"][k]
        if p != seen_rows: sys.exit(f"group pass out of order at position {p} (expected {seen_rows}); the key join lost or duplicated rows")
        seen_rows += 1
        if cur is None or p >= cur["hi"]:
            cur = leaf_at[p]; jobs = []; V = X[order[cur["lo"]:cur["hi"]]].astype(np.float32)
        r = int(order[p]); a, s_, jid, title, loc, url = cols["ats"][k], cols["slug"][k], cols["id"][k], cols["title"][k], cols["location"][k], cols["url"][k]
        jd = cols["jd"][k] or ""; enr = cols["enrichment"][k]
        jobs.append({"ats": a, "slug": s_, "id": jid, "title": title, "company": company(r), "location": loc, "url": url, "seen": int(cols["first_seen_ms"][k] or 0), "pub": int(cols["published_ms"][k] or 0), "jd": jd,
                     **({"e": json.loads(enr)} if enr else {}), **({"co_": compfull[(a, s_)]} if (a, s_) in compfull else {}),
                     "v": base64.b64encode(V[p - cur["lo"]].tobytes()).decode()})
        if p + 1 == cur["hi"]:
            gpath = os.path.join(out, "groups", f"{cur['id']}.json")
            with open(gpath, "w") as f: json.dump({"leaf": cur["id"], "lo": cur["lo"], "hi": cur["hi"], "jobs": jobs}, f)
            if uploader: uploader.put(f"{args.groups_prefix}{cur['id']}.json", gpath, "application/json")
            written += 1; jobs = []; V = None
            if written % 500 == 0: print(f"\r  {written}/{len(leaves)} group files, {time.time()-t:.0f}s", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)
shutil.rmtree(stage, ignore_errors=True)
if seen_rows != N or written != len(leaves): sys.exit(f"group pass wrote {written}/{len(leaves)} files over {seen_rows}/{N} rows")
if uploader:
    failed = uploader.join()
    print(f"published {r2.uploaded} group files ({r2.uploaded_bytes/1e6:.0f} MB) to {args.groups_prefix}; {len(failed)} failed" + (f", e.g. {failed[0]}" if failed else "") + "; the finalize stage reconciles", flush=True)
    json.dump({"prefix": args.groups_prefix, "published": r2.uploaded, "failed": failed}, open(os.path.join(out, ".published-groups.json"), "w"))
del X
try: os.remove(_xpath)
except OSError: pass
shutil.rmtree(con_tmp, ignore_errors=True)
size = sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, "groups", "*.json")))
print(f"wrote manifest ({os.path.getsize(os.path.join(out,'manifest.json'))/1e6:.1f} MB), centroids ({C.nbytes/1e6:.1f} MB), {len(leaves)} group files ({size/1e6:.0f} MB) in {time.time()-t:.0f}s")

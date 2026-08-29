# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1"]
# ///
"""Country estimator for location strings the rules in tools/locparse.py can't place ("Charleston",
"Main Campus", "Utrecht, Nederland"). Embeds every distinct location string once (text-embedding-3-small,
256 dims — they're a few tokens each; the cache in export/location-embeddings.npz makes daily runs
cost pennies), then for each unplaced string takes the nearest placed strings and votes on the country.
Thresholds are calibrated on held-out placed strings so the table is >= 95% accurate where it answers.
Writes <EXPORT_DIR>/web/location-countries.json {string: [country, confidence]} — locparse uses it as a
fallback and the client shows those countries as estimates.
Run: EXPORT_DIR=export/latest uv run scripts/build-location-table.py   (key: OPENAI_API_KEY or ../oai_key.txt)"""
import json, os, sys, time, urllib.request, collections
from concurrent.futures import ThreadPoolExecutor
import numpy as np, duckdb
here = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(here, "..", "..", "tools"))
from locparse import parse, REMOTE_RE
root = os.path.join(here, "..", os.environ.get("EXPORT_DIR", "export/latest"))
cache_path = os.path.join(here, "..", "export", "location-embeddings.npz")
DIMS, MODEL = 256, "text-embedding-3-small"
key = os.environ.get("OPENAI_API_KEY") or open(os.path.join(here, "..", "..", "oai_key.txt"), encoding="utf-8").read().strip()

t0 = time.time()
rows = duckdb.connect().execute(f"SELECT location, count(*) FROM read_parquet('{root}/jobs/*.parquet') WHERE is_open AND location IS NOT NULL AND length(location) BETWEEN 2 AND 120 GROUP BY location").fetchall()
strings = [r[0].strip() for r in rows if r[0].strip()]; counts = {r[0].strip(): r[1] for r in rows}
placed, unplaced = {}, []
for s in strings:
    p = parse(s, "", "")
    if len(p["countries"]) == 1: placed[s] = p["countries"][0]
    elif not p["countries"]:
        bare = REMOTE_RE.sub("", s.lower()); bare = "".join(ch for ch in bare if ch.isalnum())
        if bare in ("", "job", "jobs", "position", "role", "worldwide", "global", "anywhere", "fully", "100"): continue  # "Remote", "Remote job": no country is the right answer
        unplaced.append(s)
print(f"{len(strings):,} distinct location strings: {len(placed):,} placed by rules, {len(unplaced):,} unplaced "
      f"({sum(counts[s] for s in unplaced):,} postings = {sum(counts[s] for s in unplaced) / sum(counts.values()):.0%})")

# ---- embeddings, cached by string ----
emb = {}
if os.path.exists(cache_path):
    z = np.load(cache_path, allow_pickle=True); emb = dict(zip(z["keys"].tolist(), z["vecs"]))
need = [s for s in placed if s not in emb] + [s for s in unplaced if s not in emb]
print(f"embedding {len(need):,} new strings ({len(emb):,} cached)")
usage = [0]
def embed_batch(batch):
    body = json.dumps({"model": MODEL, "input": batch, "dimensions": DIMS}).encode()
    for attempt in range(6):
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body, headers={"authorization": f"Bearer {key}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r: d = json.loads(r.read())
            usage[0] += d.get("usage", {}).get("total_tokens", 0)
            return [np.asarray(e["embedding"], dtype=np.float32) for e in sorted(d["data"], key=lambda e: e["index"])]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503): time.sleep(2 * (attempt + 1)); continue
            raise SystemExit(f"openai HTTP {e.code}: {e.read()[:200]}")
    raise SystemExit("embedding failed after retries")
if need:
    batches = [need[i:i + 1024] for i in range(0, len(need), 1024)]; done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for batch, vecs in zip(batches, ex.map(embed_batch, batches)):
            for s, v in zip(batch, vecs): emb[s] = v
            done += len(batch); print(f"\r  {done:,}/{len(need):,} embedded, {usage[0]:,} tokens (${usage[0] * 0.02 / 1e6:.2f})", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    keys = list(emb.keys()); np.savez(cache_path, keys=np.array(keys, dtype=object), vecs=np.stack([emb[k] for k in keys]))

# ---- kNN vote ----
A_keys = [s for s in placed if s in emb]; A = np.stack([emb[s] for s in A_keys]); A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-9
A_cc = np.array([placed[s] for s in A_keys])
def vote(Q, A, A_cc, k=10):
    """returns (country, confidence, best_sim) per query row"""
    out = []
    for i in range(0, len(Q), 2048):
        S = Q[i:i + 2048] @ A.T
        top = np.argpartition(-S, k, axis=1)[:, :k]
        for r in range(len(top)):
            sims = S[r, top[r]]; w = collections.Counter()
            for j, sim in zip(top[r], sims): w[A_cc[j]] += max(float(sim), 0) ** 4  # sharpen: the closest neighbours dominate
            tot = sum(w.values()) or 1e-9; cc, best = w.most_common(1)[0]
            out.append((cc, best / tot, float(sims.max())))
    return out

# calibrate on held-out placed strings (they are never their own neighbour: we exclude them from A)
rng = np.random.default_rng(0); ho = rng.choice(len(A_keys), size=min(5000, len(A_keys) // 10), replace=False); mask = np.ones(len(A_keys), bool); mask[ho] = False
res = vote(A[ho], A[mask], A_cc[mask]); truth = A_cc[ho]
best = None
for min_sim in (0.5, 0.6, 0.7, 0.8):
    for min_conf in (0.6, 0.8, 0.9):
        ok = [(c == t) for (c, conf, sim), t in zip(res, truth) if conf >= min_conf and sim >= min_sim]
        cov = len(ok) / len(res); acc = (sum(ok) / len(ok)) if ok else 0
        print(f"  min_sim {min_sim} min_conf {min_conf}: accuracy {acc:.1%} on {cov:.0%} of held-out strings")
        # held-out placed strings are easier than real unplaced ones (they contain a country token), so require
        # a 0.8 vote floor regardless of what the calibration would tolerate
        if acc >= 0.95 and min_conf >= 0.8 and (best is None or cov > best[0]): best = (cov, min_sim, min_conf, acc)
if best is None: raise SystemExit("no threshold reaches 95% held-out accuracy; not writing a table")
cov, MIN_SIM, MIN_CONF, acc = best
print(f"chosen: min_sim {MIN_SIM}, min_conf {MIN_CONF} -> {acc:.1%} accurate on {cov:.0%} of held-out strings")

Q_keys = [s for s in unplaced if s in emb]; Q = np.stack([emb[s] for s in Q_keys]); Q /= np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9
table = {}
for s, (cc, conf, sim) in zip(Q_keys, vote(Q, A, A_cc)):
    if conf >= MIN_CONF and sim >= MIN_SIM: table[s] = [cc, round(conf, 2)]
covered = sum(counts[s] for s in table); unplaced_jobs = sum(counts[s] for s in unplaced)
out = os.path.join(root, "web", "location-countries.json"); os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({"model": f"{MODEL}:{DIMS}", "min_sim": MIN_SIM, "min_conf": MIN_CONF, "holdout_accuracy": acc, "n": len(table), "built_at": int(time.time() * 1000), "table": table}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print(f"wrote {out}: {len(table):,} of {len(unplaced):,} unplaced strings get a country ({covered:,} of {unplaced_jobs:,} unplaced postings = {covered / max(unplaced_jobs, 1):.0%}); "
      f"top: {collections.Counter(v[0] for v in table.values()).most_common(6)}; tokens {usage[0]:,} (${usage[0] * 0.02 / 1e6:.2f}); {time.time() - t0:.0f}s")

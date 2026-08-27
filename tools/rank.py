# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1"]
# ///
"""LLM merge sort over your slice, bring-your-own-key.

  OPENAI_API_KEY=sk-... uv run tools/rank.py --top 200 --budget 1500 [--parallel 8] [--model gpt-5.6-luna]

Takes the top N jobs of work/jobs.parquet (ordered by your taste model if work/model.json exists,
else by similarity to the ideal JD), and sorts them with an LLM as the comparator: "given this
ideal JD, which of these two jobs is the better match?" Bottom-up merge sort; merges at the same
level run in parallel (comparisons inside a merge are inherently sequential). M caps the number of
LLM comparisons: levels run while the budget allows a full level; any unfinished level is resolved
by the base order, so you always get a total order. Every comparison is cached in
work/llm-compares.json (pair-keyed, A/B order randomized per call), so re-runs are free and a bigger
--budget later only pays for new pairs. Output: work/llm-ranked.csv, work/llm-order.json.
Cost: ~$0.0005 per comparison on gpt-5.6-luna (two ~1,500-char excerpts); N log2 N comparisons for a
full sort (200 jobs ≈ 1,500 comparisons ≈ $0.75). Time is dominated by the last merge, which is
sequential (~3 s per comparison): 200 jobs ≈ 15-20 min; --parallel only speeds up the early levels."""
import argparse, base64, json, os, random, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np
np.seterr(all="ignore")

WORK = os.environ.get("WORK", "work")
ap = argparse.ArgumentParser()
ap.add_argument("--top", type=int, default=200, help="N: how many of the best-ranked jobs to sort")
ap.add_argument("--budget", type=int, default=1500, help="M: max LLM comparisons this run (cached ones are free)")
ap.add_argument("--parallel", type=int, default=8)
ap.add_argument("--model", default="gpt-5.6-luna")
ap.add_argument("--key", default=os.environ.get("OPENAI_API_KEY"))
ap.add_argument("--excerpt", type=int, default=1500, help="chars of each JD sent per comparison")
a = ap.parse_args()
if not a.key: sys.exit("set OPENAI_API_KEY (or --key)")

# ---------- load slice + base order ----------
import duckdb
rows = duckdb.connect().execute(f"SELECT ats, slug, id, title, company, location, url, jd, sim, vec_b64 FROM read_parquet('{os.path.join(WORK, 'jobs.parquet')}')").fetchall()
ideal = json.load(open(os.path.join(WORK, "ideal.json")))
ideal_text = open(ideal["source"]).read() if os.path.exists(ideal.get("source", "")) else ""
mp = os.path.join(WORK, "model.json")
if os.path.exists(mp):
    m = json.load(open(mp)); w = np.asarray(m.get("taste") or m["w"], dtype=np.float32)
    X = np.stack([np.frombuffer(base64.b64decode(r[9]), dtype=np.float32) for r in rows]); base = X @ w
    print(f"base order: taste model from work/model.json")
else:
    base = np.asarray([r[8] for r in rows], dtype=np.float32); print("base order: similarity to the ideal JD")
order = np.argsort(-base)
# dedupe identical (company, title) postings so we don't burn comparisons on mirrors
seen = set(); idx = []
for i in order:
    k = ((rows[i][4] or "").lower().strip(), (rows[i][3] or "").lower().strip())
    if k in seen: continue
    seen.add(k); idx.append(int(i))
    if len(idx) >= a.top: break
jobs = [rows[i] for i in idx]
key = lambda r: f"{r[0]}/{r[1]}#{r[2]}"
N = len(jobs)
levels = int(np.ceil(np.log2(max(N, 2))))
print(f"{N} jobs to sort, {levels} merge levels, ≈{N * levels:,} comparisons for a full sort; budget {a.budget}")

# ---------- LLM comparator with cache ----------
cache_path = os.path.join(WORK, "llm-compares.json")
cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
lock = threading.Lock(); used = [0]; spent = [0.0]; over_budget = [False]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["winner", "confidence", "why"],
          "properties": {"winner": {"type": "string", "enum": ["A", "B"]}, "confidence": {"type": "number"}, "why": {"type": "string"}}}
INSTR = ("You are helping one specific person find the job they want. You will be given their ideal job description, "
         "then two real postings A and B. Decide which single posting is the better match for THIS person: role, seniority, "
         "the work itself, stack, arrangement/location, compensation if stated, company type. Ignore posting length and "
         "polish. If genuinely equal, pick the one whose day-to-day work is closer to the ideal. Answer with strict JSON.")
def card(r):
    return f"Title: {r[3]}\nCompany: {r[4]}\nLocation: {r[5]}\nPosting:\n{(r[7] or '')[:a.excerpt]}"
def llm_compare(x, y):
    """returns True if x is better than y"""
    kx, ky = key(x), key(y)
    ck = kx + "||" + ky if kx < ky else ky + "||" + kx
    with lock:
        if ck in cache: return cache[ck]["winner"] == kx
        if used[0] >= a.budget: over_budget[0] = True; return None
        used[0] += 1
    flip = random.random() < 0.5
    A, B = (y, x) if flip else (x, y)
    body = {"model": a.model, "reasoning": {"effort": "none"}, "max_output_tokens": 300,
            "instructions": INSTR,
            "input": f"IDEAL JOB DESCRIPTION:\n{ideal_text[:4000]}\n\n=== A ===\n{card(A)}\n\n=== B ===\n{card(B)}",
            "text": {"format": {"type": "json_schema", "name": "pick", "schema": SCHEMA, "strict": True}}}
    for attempt in range(5):
        req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                     headers={"authorization": f"Bearer {a.key}", "content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r: res = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4: time.sleep(2 * (attempt + 1)); continue
            sys.exit(f"openai HTTP {e.code}: {e.read()[:200]}")
    text = next(c["text"] for o in res["output"] if o["type"] == "message" for c in o["content"] if c["type"] == "output_text")
    d = json.loads(text); u = res.get("usage", {})
    cost = (u.get("input_tokens", 0) * 0.2 + u.get("output_tokens", 0) * 1.2) / 1e6
    winner = (A if d["winner"] == "A" else B)
    with lock:
        spent[0] += cost
        cache[ck] = {"winner": key(winner), "loser": key(B if winner is A else A), "confidence": d.get("confidence"), "why": d.get("why"), "model": a.model}
        if used[0] % 25 == 0:
            json.dump(cache, open(cache_path, "w")); print(f"  {used[0]}/{a.budget} comparisons, ${spent[0]:.2f}", flush=True)
    return winner is x

# ---------- parallel bottom-up merge sort ----------
def merge(L, R):
    out = []; i = j = 0
    while i < len(L) and j < len(R):
        better = llm_compare(L[i], R[j])
        if better is None:  # budget exhausted mid-merge: fall back to base order for the rest
            return out + interleave(L[i:], R[j:])
        if better: out.append(L[i]); i += 1
        else: out.append(R[j]); j += 1
    return out + L[i:] + R[j:]
def interleave(L, R):  # merge by base score (no LLM)
    b = {key(r): float(base[i]) for i, r in zip(idx, jobs)}
    return sorted(L + R, key=lambda r: -b[key(r)])
runs = [[j] for j in jobs]
level = 0; t0 = time.time()
while len(runs) > 1:
    level += 1
    need = sum(len(runs[i]) + len(runs[i + 1]) for i in range(0, len(runs) - 1, 2))
    cached = 0  # rough: unknown until run; just check remaining budget
    remaining = a.budget - used[0]
    if remaining <= 0:
        print(f"level {level}: budget exhausted, finishing with base order"); runs = [interleave(sum(runs, []), [])]; break
    print(f"level {level}: {len(runs)} runs → {len(runs) // 2 + len(runs) % 2}, up to {need} comparisons, {remaining} budget left", flush=True)
    pairs = [(runs[i], runs[i + 1]) for i in range(0, len(runs) - 1, 2)]
    with ThreadPoolExecutor(max_workers=a.parallel) as ex: merged = list(ex.map(lambda p: merge(*p), pairs))
    if len(runs) % 2: merged.append(runs[-1])
    runs = merged
    json.dump(cache, open(cache_path, "w"))
final = runs[0]
json.dump(cache, open(cache_path, "w"))
# ---------- outputs ----------
outp = os.path.join(WORK, "llm-ranked.csv")
with open(outp, "w") as f:
    f.write("rank,title,company,location,url,key\n")
    for n, r in enumerate(final, 1):
        f.write(f"{n},\"{(r[3] or '').replace(chr(34), chr(39))}\",\"{(r[4] or '').replace(chr(34), chr(39))}\",\"{(r[5] or '').replace(chr(34), chr(39))}\",{r[6]},{key(r)}\n")
json.dump({"model": a.model, "top": N, "comparisons": used[0], "usd": round(spent[0], 4), "order": [key(r) for r in final], "budget_exhausted": over_budget[0]}, open(os.path.join(WORK, "llm-order.json"), "w"))
print(f"\ndone: {used[0]} new comparisons (${spent[0]:.2f}), {len(cache)} cached total, {time.time() - t0:.0f}s" + (" — budget ran out; tail resolved by base order" if over_budget[0] else ""))
print(f"wrote {outp} and {WORK}/llm-order.json\nTop 15:")
for n, r in enumerate(final[:15], 1): print(f"  {n:2d}. {r[3][:55]:55s} | {r[4] or '':22s} | {(r[5] or '')[:24]}")

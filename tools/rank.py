# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1"]
# ///
"""LLM-assisted merge sort over your slice, bring-your-own-key. Budget goes where it matters.

  OPENAI_API_KEY=sk-... uv run tools/rank.py --top 200 --budget 1500 [--parallel 8] [--model gpt-5.6-luna]

Takes the top N jobs of work/jobs.parquet (ordered by your taste model if work/model.json exists,
else by similarity to the ideal JD), and merge-sorts them. The LLM ("given this ideal JD, which of
these two jobs is the better match?") is consulted only where it can change the outcome: pairs that
are both in the base top K (--focus), or pairs within G places of each other in the base order
(--gap). Everything else is decided by the base order (taste model / cosine similarity), so the
final merge costs a few calls instead of N. Merges at the same level run in parallel. M caps LLM
comparisons; when it runs out the rest is resolved by the base order, so you always get a total
order. Every comparison is cached in
work/llm-compares.json (pair-keyed, A/B order randomized per call), so re-runs are free and a bigger
--budget later only pays for new pairs. Output: work/llm-ranked.csv, work/llm-order.json.
Cost: ~$0.0005 per LLM comparison on gpt-5.6-luna. With the defaults (--focus 40, --gap 6) a
200-job sort uses a few hundred calls (~$0.15) and a few minutes; raise --focus/--gap to spend
more of the budget, lower them to spend less.

No key? The coding agent driving this repo can be the judge instead (--agent):

  uv run tools/rank.py --agent --top 200 [--batch 40]

Same sort, but on a cache miss the pair is written to work/rank-pairs.md (readable: the ideal JD
once, then A/B cards) and work/rank-pairs.json. The agent reads the .md, judges every pair, writes
work/rank-answers.json as {"<pair id>": {"winner": "A"|"B", "confidence": 0-1, "why": "..."}}, and
re-runs the same command. Answers land in the same cache; the sort resumes and emits the next batch,
until it prints "done". A 200-job sort is usually 3-6 batches. Exit status 3 means "pairs pending"."""
import argparse, base64, json, os, random, re, sys, threading, time, urllib.request, urllib.error
# Windows consoles default to cp1252; our output has ✓ · – etc. Reconfigure stdout/stderr to UTF-8 (no-op elsewhere).
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
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
ap.add_argument("--focus", type=int, default=40, help="K: ask the LLM whenever both jobs are in the base top K (the region where rank matters)")
ap.add_argument("--agent", action="store_true", help="no API: write pairs for the coding agent to judge, ingest work/rank-answers.json, repeat")
ap.add_argument("--batch", type=int, default=40, help="agent mode: max pairs to hand over per run")
ap.add_argument("--gap", type=int, default=6, help="G: outside the top K, ask the LLM only when the two jobs are within G places of each other in the base order; otherwise trust it")
ap.add_argument("--no-eligibility", action="store_true", help="sort every job, not just the ones eligible for the location in work/ideal.json (and not hidden)")
a = ap.parse_args()
_focus_set = any(x.startswith("--focus") for x in sys.argv); _gap_set = any(x.startswith("--gap") for x in sys.argv)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locparse import eligibility as loc_eligibility
if not a.key and not a.agent: sys.exit("set OPENAI_API_KEY (or use --agent to let the coding agent judge)")

# ---------- load slice + base order ----------
import duckdb
rows = duckdb.connect().execute(f"SELECT ats, slug, id, title, company, location, url, jd, sim, vec_b64 FROM read_parquet('{os.path.join(WORK, 'jobs.parquet')}')").fetchall()
ideal = json.load(open(os.path.join(WORK, "ideal.json"), encoding="utf-8"))
ideal_text = open(ideal["source"], encoding="utf-8").read() if os.path.exists(ideal.get("source", "")) else ""
mp = os.path.join(WORK, "model.json")
if os.path.exists(mp):
    m = json.load(open(mp, encoding="utf-8")); w = np.asarray(m.get("taste") or m["w"], dtype=np.float32)
    X = np.stack([np.frombuffer(base64.b64decode(r[9]), dtype=np.float32) for r in rows]); base = X @ w + float(m.get("b") or 0)
    # kNN term from the page's model: max cosine to any yes minus max cosine to any no, weighted by the leave-one-out λ
    knn = m.get("knn") or {}
    if knn.get("yes") or knn.get("no"):
        kidx = {f"{r[0]}/{r[1]}#{r[2]}": i for i, r in enumerate(rows)}
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
        def maxcos(keys):
            ids = [kidx[k] for k in keys if k in kidx]
            if not ids: return np.zeros(len(rows), dtype=np.float32)
            S = Xn @ Xn[ids].T
            for c, i in enumerate(ids): S[i, c] = -1  # a job is not its own neighbour
            return np.maximum(S.max(axis=1), 0)
        base = base + float(knn.get("lambda", 1.0)) * (maxcos(knn.get("yes", [])) - maxcos(knn.get("no", [])))
    print(f"base order: taste model from work/model.json" + (f" (+ kNN term, λ={knn.get('lambda')}, {len(knn.get('yes', []))} yes / {len(knn.get('no', []))} no)" if knn else ""))
else:
    base = np.asarray([r[8] for r in rows], dtype=np.float32); print("base order: similarity to the ideal JD")
    # raw cosine is a weak base order: widen the region where the judge is consulted unless the user set it
    if not _focus_set: a.focus = max(a.focus, 80)
    if not _gap_set: a.gap = max(a.gap, 12)
    print(f"  (no taste model: focus {a.focus}, gap {a.gap})")
order = np.argsort(-base)
# only jobs the page would show: eligible for the ideal.json location, not from a hidden company
pref = (ideal.get("location") or "").strip(); hidden = set(); skipped = {"ineligible": 0, "hidden": 0, "empty": 0, "dup": 0}
ipath = os.path.join(WORK, "interactions.jsonl")
if os.path.exists(ipath):
    for line in open(ipath, encoding="utf-8"):
        try: ev = json.loads(line)
        except Exception: continue
        if ev.get("type") == "hide_company" and ev.get("board"): (hidden.add if ev.get("on", True) else hidden.discard)(ev["board"])
def _norm_title(t): return re.sub(r"\s*[-–(\[|].*$", "", (t or "").lower()).strip()  # "SRE - EMEA" == "SRE (US)"
seen = set(); idx = []
for i in order:
    r = rows[i]
    if not a.no_eligibility:
        if f"{r[0]}/{r[1]}" in hidden: skipped["hidden"] += 1; continue
        if pref and loc_eligibility(pref, r[5] or "", r[7] or "", r[3] or "")[0] is False: skipped["ineligible"] += 1; continue
    if len((r[7] or "").strip()) < 200: skipped["empty"] += 1; continue  # no description: nothing to judge, and the page shows it anyway
    k = ((r[4] or "").lower().strip(), _norm_title(r[3]), (r[7] or "")[:300])  # same company, same title stem, same opening = one req split by region
    k2 = ((r[4] or "").lower().strip(), (r[3] or "").lower().strip())
    if k in seen or k2 in seen: skipped["dup"] += 1; continue
    seen.add(k); seen.add(k2); idx.append(int(i))
    if len(idx) >= a.top: break
jobs = [rows[i] for i in idx]
print(f"pool: {len(jobs)} of {len(rows)} jobs (skipped {skipped['ineligible']} ineligible for {pref!r}, {skipped['hidden']} hidden companies, {skipped['empty']} without a description, {skipped['dup']} duplicates)" + ("  [--no-eligibility]" if a.no_eligibility else ""))
key = lambda r: f"{r[0]}/{r[1]}#{r[2]}"
N = len(jobs)
levels = int(np.ceil(np.log2(max(N, 2))))
print(f"{N} jobs to sort, {levels} merge levels, ≈{N * levels:,} comparisons for a full sort; budget {a.budget}")

# ---------- LLM comparator with cache ----------
cache_path = os.path.join(WORK, "llm-compares.json")
cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
lock = threading.Lock(); used = [0]; spent = [0.0]; over_budget = [False]
needed = []; needed_keys = set()  # agent mode: pairs awaiting judgment this run
pairs_json = os.path.join(WORK, "rank-pairs.json"); pairs_md = os.path.join(WORK, "rank-pairs.md"); answers_path = os.path.join(WORK, "rank-answers.json")
if a.agent and os.path.exists(answers_path) and os.path.exists(pairs_json):
    asked = json.load(open(pairs_json, encoding="utf-8")); answers = json.load(open(answers_path, encoding="utf-8")); n_in = 0
    for pid, ans in answers.items():
        q = asked.get(pid)
        if not q or str(ans.get("winner", "")).upper() not in ("A", "B"): continue
        win = q["A"] if str(ans["winner"]).upper() == "A" else q["B"]; lose = q["B"] if win == q["A"] else q["A"]
        cache[q["id"]] = {"winner": win, "loser": lose, "confidence": ans.get("confidence"), "why": ans.get("why"), "model": "agent"}; n_in += 1
    json.dump(cache, open(cache_path, "w", encoding="utf-8")); os.remove(answers_path)
    print(f"ingested {n_in} agent answers from work/rank-answers.json ({len(cache)} cached total)")
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
        if a.agent:
            if ck not in needed_keys and len(needed) < a.batch: needed_keys.add(ck); needed.append((ck, x, y))
            over_budget[0] = True; return None
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
            json.dump(cache, open(cache_path, "w", encoding="utf-8")); print(f"  {used[0]}/{a.budget} comparisons, ${spent[0]:.2f}", flush=True)
    return winner is x

# ---------- parallel bottom-up merge sort ----------
bscore = {key(r): float(base[i]) for i, r in zip(idx, jobs)}
brank = {key(r): n for n, r in enumerate(jobs)}  # jobs are in base order already
stats = {"llm": 0, "base": 0}
def decide(x, y, pos):
    """True if x ranks above y. LLM when both are in the base top-K (rank matters there) or it's a
    close call by base score; otherwise the base order decides (cosine similarity as tiebreak)."""
    bx, by = bscore[key(x)], bscore[key(y)]
    rx, ry = brank[key(x)], brank[key(y)]
    if (rx < a.focus and ry < a.focus) or abs(rx - ry) <= a.gap:
        r = llm_compare(x, y)
        if r is not None: stats["llm"] += 1; return r
    stats["base"] += 1
    return (bx, x[8]) >= (by, y[8])
def merge(L, R):
    out = []; i = j = 0
    while i < len(L) and j < len(R):
        if decide(L[i], R[j], len(out)): out.append(L[i]); i += 1
        else: out.append(R[j]); j += 1
    return out + L[i:] + R[j:]
def interleave(L, R):  # merge by base score (no LLM)
    b = {key(r): float(base[i]) for i, r in zip(idx, jobs)}
    return sorted(L + R, key=lambda r: -b[key(r)])
runs = [[j] for j in jobs]
level = 0; t0 = time.time()
while len(runs) > 1:
    level += 1
    remaining = a.budget - used[0]
    print(f"level {level}: {len(runs)} runs → {len(runs) // 2 + len(runs) % 2}, {remaining} budget left", flush=True)
    pairs = [(runs[i], runs[i + 1]) for i in range(0, len(runs) - 1, 2)]
    with ThreadPoolExecutor(max_workers=a.parallel) as ex: merged = list(ex.map(lambda p: merge(*p), pairs))
    if len(runs) % 2: merged.append(runs[-1])
    runs = merged
    json.dump(cache, open(cache_path, "w", encoding="utf-8"))
final = runs[0]
# promotion pass: a job outside the top K that the judge preferred (confidence >= 0.8) over a job inside the top K
# moves up to just above it. Merge sort only asks about neighbours, so a strong job can be stranded by base order.
K = a.focus; promoted = 0
for _ in range(2):
    pos = {key(r): i for i, r in enumerate(final)}
    for r in list(final[K:]):
        k = key(r); best = None
        for t in final[:K]:
            ck = k + "||" + key(t) if k < key(t) else key(t) + "||" + k; c = cache.get(ck)
            if c and c["winner"] == k and (c.get("confidence") or 0) >= 0.8: best = pos[key(t)] if best is None else min(best, pos[key(t)]); 
        if best is not None:
            final.remove(r); final.insert(best, r); promoted += 1; pos = {key(x): i for i, x in enumerate(final)}
if promoted: print(f"promotion pass: {promoted} jobs moved into the top {K} on high-confidence judge wins")
json.dump(cache, open(cache_path, "w", encoding="utf-8"))
if a.agent and needed:
    asked = {}; md = ["# Pairs to judge\n", "For each pair, decide which posting is the better match for THIS person's ideal job description: role, seniority, the work itself, stack, arrangement/location, compensation if stated, company type. Ignore posting length and polish. If genuinely equal, pick the one whose day-to-day work is closer to the ideal.\n",
                      f"Write `work/rank-answers.json` as `{{\"<pair id>\": {{\"winner\": \"A\"|\"B\", \"confidence\": 0-1, \"why\": \"one sentence\"}}}}` for every pair below, then re-run `uv run tools/rank.py --agent --top {a.top}`.\n",
                      "## Ideal job description\n", ideal_text[:4000], "\n"]
    shown = {}  # key -> short ref; a posting is printed in full the first time only
    def ref(r):
        k = key(r)
        if k in shown: return f"### {shown[k]} (see above): {r[3]} | {r[4]} | {r[5]}\n"
        shown[k] = f"J{len(shown) + 1}"
        return f"### {shown[k]}: {r[3]} | {r[4]} | {r[5]}\n" + (r[7] or "")[:a.excerpt] + "\n"
    for n, (ck, x, y) in enumerate(needed, 1):
        A, B = (y, x) if random.random() < 0.5 else (x, y); pid = f"p{n}"
        asked[pid] = {"id": ck, "A": key(A), "B": key(B)}
        md += [f"\n---\n## {pid}: A = {shown.get(key(A), 'new')} · B = {shown.get(key(B), 'new')}\n", "**A** " + ref(A), "**B** " + ref(B)]
    md.insert(3, "Postings are printed in full the first time they appear (as J1, J2, …) and referenced by that label afterwards.\n")
    json.dump(asked, open(pairs_json, "w", encoding="utf-8")); open(pairs_md, "w", encoding="utf-8").write("\n".join(md))
    print(f"\n{len(needed)} pairs need your judgment -> work/rank-pairs.md  (write work/rank-answers.json, then re-run this command). Partial ranking below.")
# ---------- outputs ----------
outp = os.path.join(WORK, "llm-ranked.csv")
with open(outp, "w", encoding="utf-8") as f:
    f.write("rank,title,company,location,url,key\n")
    for n, r in enumerate(final, 1):
        f.write(f"{n},\"{(r[3] or '').replace(chr(34), chr(39))}\",\"{(r[4] or '').replace(chr(34), chr(39))}\",\"{(r[5] or '').replace(chr(34), chr(39))}\",{r[6]},{key(r)}\n")
json.dump({"model": "agent" if a.agent else a.model, "top": N, "pending": len(needed), "comparisons": used[0], "usd": round(spent[0], 4), "order": [key(r) for r in final], "budget_exhausted": over_budget[0]}, open(os.path.join(WORK, "llm-order.json"), "w", encoding="utf-8"))
print(f"\ndone: {stats['llm']} LLM decisions ({used[0]} new, ${spent[0]:.2f}), {stats['base']} decided by base order, {len(cache)} cached total, {time.time() - t0:.0f}s" + (" — budget ran out; remainder by base order" if over_budget[0] else ""))
print(f"wrote {outp} and {WORK}/llm-order.json\nTop 15:")
for n, r in enumerate(final[:15], 1): print(f"  {n:2d}. {r[3][:55]:55s} | {r[4] or '':22s} | {(r[5] or '')[:24]}")
if a.agent and needed: sys.exit(3)
if a.agent: print("done: no pending pairs; work/llm-ranked.csv is final")

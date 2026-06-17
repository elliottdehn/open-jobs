#!/usr/bin/env python3
"""
langsort.py - STEP 2 of the hull -> learn -> rank pipeline. Gather the LLM's PAIRWISE judgments
("which of these two fits the resume better?"). Pairwise judgment is far better-calibrated than
absolute 0-100 scoring, and every decision is appended to a log (--log) that btrank.py turns into a
ranking and, optionally, a reusable model.

Two modes:
  --mode sample (default): draw ~--per-item random comparisons per role and run them ALL in parallel
    (no ordering dependency), enough to LEARN the LLM's discrimination cheaply. This is the step-2
    default; feed the decision log to btrank.py.
  --mode sort: a contradiction-free MERGE SORT that emits a full total order. It only compares the
    heads of two already-sorted runs, so it never asks a transitively-implied comparison and a noisy
    comparator can never surface a contradiction -- but the final merge is a sequential ~n tail.
    Use it to get an exact order over a small shortlist.

Decisions are memoized and the log is replayed on restart, so a killed run resumes losing nothing.

    export OPENAI_API_KEY=sk-...
    python3 langsort.py --resume resume.txt --candidates hull.json --mode sample --per-item 12
"""
import argparse, json, os, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import pyarrow.parquet as pq

CHAT = "https://api.openai.com/v1/chat/completions"
JD_MAX = 4000
SCHEMA = {"name": "pick", "strict": True, "schema": {"type": "object", "additionalProperties": False,
          "required": ["winner"], "properties": {"winner": {"type": "string", "enum": ["A", "B"]}}}}
PROMPT = """You compare two jobs for fit to a candidate's resume. Decide which job is the better fit for
THIS candidate (skills, level, domain, trajectory). Output 'A' if job A fits better, 'B' if job B does.
You must choose one even when it's close."""


def ask(resume, a, b, key, model, reasoning, max_out, tries=6):
    body = {"model": model, "max_completion_tokens": max_out,
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"RESUME:\n{resume}\n\n=== JOB A: {a[0]} @ {a[1]} ===\n{a[2][:JD_MAX]}"
                                                     f"\n\n=== JOB B: {b[0]} @ {b[1]} ===\n{b[2][:JD_MAX]}"}]}
    if reasoning != "none" and model.startswith(("gpt-5", "o1", "o3", "o4")): body["reasoning_effort"] = reasoning
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(CHAT, data=data, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                ch = json.load(r)["choices"][0]
                txt = ch["message"]["content"]
                if not txt:                            # truncated mid-reasoning -> bump cap and retry
                    raise RuntimeError(f"empty response (finish={ch.get('finish_reason')}); raise --max-out")
                return json.loads(txt)["winner"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1: time.sleep(min(2 ** i, 30)); continue
            raise RuntimeError(f"OpenAI {e.code}: {e.read().decode('utf-8','replace')[:120]}")
        except Exception:
            if i < tries - 1: time.sleep(min(2 ** i, 30)); continue
            raise


def fetch_jds(parquet, ids):
    need, out = set(ids), {}
    sys.stderr.write(f"fetching {len(need)} JDs...\n")
    for b in pq.ParquetFile(parquet).iter_batches(batch_size=20000, columns=["id", "jd_markdown", "title", "company", "url"]):
        d = b.to_pydict()
        for i, jid in enumerate(d["id"]):
            if jid in need and jid not in out:
                out[jid] = (d["title"][i] or "", d["company"][i] or "", d["jd_markdown"][i] or "", d["url"][i] or "")
        if len(out) >= len(need): break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True)
    ap.add_argument("--candidates", default="hull.json", help="list with 'id' (sorts --top of it; 0 = all)")
    ap.add_argument("--parquet", default="open-jobs.parquet")
    ap.add_argument("--top", type=int, default=0, help="how many candidates to use (0 = all)")
    ap.add_argument("--mode", default="sample", choices=["sample", "sort"],
                    help="sample = ~per-item random comparisons, fully parallel, for learning (step 2); "
                         "sort = contradiction-free merge sort emitting a total order (sequential tail)")
    ap.add_argument("--per-item", type=int, default=12, help="[sample] target comparisons per role")
    ap.add_argument("--max-comparisons", type=int, default=0, help="[sample] cap on total comparisons (0 = no cap)")
    ap.add_argument("--model", default="gpt-5.4-nano-2026-03-17")
    ap.add_argument("--reasoning", default="none", choices=["none", "minimal", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--max-out", type=int, default=256,
                    help="cap output tokens per comparison; the decision is ~10 tokens, this just bounds reasoning")
    ap.add_argument("--log", default="langsort_decisions.jsonl",
                    help="append every pairwise decision here; replayed on restart to skip decided pairs")
    ap.add_argument("--out", default="langsort_results.json")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key: sys.exit("set OPENAI_API_KEY")

    resume = open(args.resume, encoding="utf-8", errors="replace").read().strip()
    cands = json.load(open(args.candidates))
    if args.top: cands = cands[:args.top]
    jds = fetch_jds(args.parquet, [c["id"] for c in cands])
    items = [c for c in cands if jds.get(c["id"]) and jds[c["id"]][2].strip()]   # need a JD to compare
    est = [1]                                               # expected total comparisons (set per mode, for ETA)
    sys.stderr.write(f"{len(items):,} roles with a JD | mode={args.mode} | model {args.model} | {args.workers} workers\n")

    memo, mlock, calls, t0 = {}, threading.Lock(), [0], time.time()
    if os.path.exists(args.log):                   # resume: replay decided pairs so we never re-ask them
        for line in open(args.log):
            try: d = json.loads(line); memo[(d["a_id"], d["b_id"])] = (d["winner"] == "A")
            except Exception: pass
        if memo: sys.stderr.write(f"resumed {len(memo):,} prior decisions from {args.log}\n")
    logf = open(args.log, "a")

    def before(x, y):                              # True if x should rank before y (x is the better fit)
        kx, ky = (x["id"], y["id"]), (y["id"], x["id"])
        with mlock:
            if kx in memo: return memo[kx]
            if ky in memo: return not memo[ky]
        jx, jy = jds[x["id"]], jds[y["id"]]
        w = ask(resume, (jx[0], jx[1], jx[2]), (jy[0], jy[1], jy[2]), key, args.model, args.reasoning, args.max_out)
        res = (w == "A")
        with mlock:
            memo[kx] = res; calls[0] += 1
            logf.write(json.dumps({"a_id": x["id"], "b_id": y["id"], "winner": w,
                                   "winner_id": x["id"] if res else y["id"],
                                   "a": f"{jx[1]}: {jx[0]}", "b": f"{jy[1]}: {jy[0]}"}, ensure_ascii=False) + "\n")
            logf.flush()
            if calls[0] % 10 == 0:
                dt = time.time() - t0; rate = calls[0] / max(dt, 1e-6)
                eta = int((est[0] - calls[0]) / max(rate, 1e-6))
                sys.stderr.write(f"    {calls[0]:,}/~{est[0]:,} comparisons ({100*calls[0]//max(est[0],1)}%) "
                                 f"| {rate:.1f}/s | eta ~{eta//60}m{eta%60:02d}s    \r"); sys.stderr.flush()
        return res

    def merge(a, b):                               # merge two better-first runs; only frontier compares
        out, i, j = [], 0, 0
        while i < len(a) and j < len(b):
            if before(a[i], b[j]): out.append(a[i]); i += 1
            else: out.append(b[j]); j += 1
        return out + a[i:] + b[j:]

    if args.mode == "sample":                      # gather ~per-item comparisons, fully parallel (no sort tail)
        import random
        rnd = random.Random(0)
        idx = {c["id"]: c for c in items}
        ids = list(idx)
        pairs = set()
        for a in ids:
            for _ in range(args.per_item):
                b = rnd.choice(ids)
                if b != a: pairs.add((a, b) if a < b else (b, a))
        pairs = [p for p in pairs if p not in memo and (p[1], p[0]) not in memo]   # skip resumed pairs
        if args.max_comparisons: pairs = pairs[:args.max_comparisons]
        est[0] = max(len(pairs), 1)
        sys.stderr.write(f"sampling {len(pairs):,} fresh pairs (~{args.per_item}/role over {len(ids):,} roles), "
                         f"fully parallel on {args.workers} workers\n")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(lambda pr: before(idx[pr[0]], idx[pr[1]]), pairs))
        logf.close()
        sys.stderr.write(f"\ngathered {calls[0]:,} new decisions -> {args.log}\n"
                         f"next: python3 btrank.py --candidates {args.candidates} --decisions {args.log}\n\n")
        return

    levels = max(1, (len(items) - 1).bit_length())  # mode == sort: contradiction-free total order
    est[0] = len(items) * levels
    sys.stderr.write(f"merge-sorting in {levels} levels, ~{est[0]:,} comparisons (sequential tail)\n")
    runs = [[c] for c in items]                    # bottom-up merge sort; merges per level run in parallel
    lvl = 0
    while len(runs) > 1:
        lvl += 1
        pairs = [(runs[k], runs[k + 1]) for k in range(0, len(runs) - 1, 2)]
        tail = [runs[-1]] if len(runs) % 2 else []
        biggest = max((len(a) + len(b) for a, b in pairs), default=0)
        sys.stderr.write(f"\nlevel {lvl}/{levels}: {len(pairs)} parallel merges, "
                         f"biggest {biggest} roles (~{biggest} sequential compares)\n")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            runs = list(ex.map(lambda p: merge(*p), pairs)) + tail
    logf.close()

    order = runs[0] if runs else []
    out = [{"rank": n + 1, "id": c["id"], "company": jds[c["id"]][1], "title": jds[c["id"]][0],
            "url": jds[c["id"]][3]} for n, c in enumerate(order)]
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
    sys.stderr.write(f"\nsorted {len(out)} roles in {calls[0]} comparisons -> {args.out}\n\n")
    for r in out[:20]:
        print(f"  #{r['rank']:<3} {str(r['company'])[:24]:<24} {str(r['title'])[:48]}")


if __name__ == "__main__":
    main()

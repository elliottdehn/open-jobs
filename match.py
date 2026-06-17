#!/usr/bin/env python3
"""
match.py - the LLM PRECISION stage over rank.py / score lists. Takes a resume + a candidate list
(rank.py / score_distill / hull JSON), pulls each finalist's full JD, and has an LLM score resume-vs-JD
fit (0-100 + verdict + matched strengths + gaps). Writes match_results.json sorted by match strength.

Built for SCALE: high concurrency + resumable (every result is appended to {out}.jsonl as it lands, so
a crash/Ctrl-C just resumes and re-scores only the misses). Cheap model by default; thousands of jobs
cost a couple dollars.

    export OPENAI_API_KEY=sk-...
    python3 match.py --resume resume.txt --candidates hull.json --top 0 --workers 24   # 0 = all
"""
import argparse, json, os, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import pyarrow.parquet as pq

CHAT = "https://api.openai.com/v1/chat/completions"
JD_MAX = 6000
SCHEMA = {"name": "resume_match", "strict": True, "schema": {"type": "object", "additionalProperties": False,
    "required": ["match_score", "verdict", "summary", "strengths", "gaps"], "properties": {
        "match_score": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["strong", "solid", "stretch", "weak"]},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}}}}}
PROMPT = """Score how well THIS candidate's resume fits THIS job. Be calibrated and honest -- most jobs are
not strong matches. Judge only on evidence in the resume vs the JD; do not assume unstated skills.
match_score 0-100: 90+ = strong (clearly qualified, should apply), 70-89 = solid (good fit, some stretch),
50-69 = stretch (plausible but real gaps), <50 = weak. verdict = the matching band. strengths: concrete
resume facts that satisfy this JD's requirements. gaps: this JD's requirements the resume does not evidence."""


def chat(resume, title, company, jd, key, model, reasoning, tries=6):
    body = {"model": model, "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"RESUME:\n{resume}\n\nJOB: {title} @ {company}\n\n{jd[:JD_MAX]}"}]}
    if model.startswith(("gpt-5", "o1", "o3", "o4")): body["reasoning_effort"] = reasoning
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(CHAT, data=data, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(json.load(r)["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1: time.sleep(min(2 ** i, 30)); continue
            raise RuntimeError(f"OpenAI {e.code}: {e.read().decode('utf-8','replace')[:140]}")
        except Exception:
            if i < tries - 1: time.sleep(min(2 ** i, 30)); continue
            raise


def fetch_jds(parquet, ids):
    need, out = set(ids), {}
    sys.stderr.write(f"fetching {len(need):,} JDs from {parquet}...\n")
    for b in pq.ParquetFile(parquet).iter_batches(batch_size=20000, columns=["id", "jd_markdown", "title", "company", "url"]):
        d = b.to_pydict()
        for i, jid in enumerate(d["id"]):
            if jid in need and jid not in out:
                out[jid] = (d["title"][i], d["company"][i], d["url"][i], d["jd_markdown"][i] or "")
        if len(out) >= len(need): break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True, help="path to resume text (.txt/.md; convert PDF first)")
    ap.add_argument("--candidates", default="matches.json", help="rank.py/score/hull JSON (list with 'id')")
    ap.add_argument("--parquet", default="open-jobs.parquet")
    ap.add_argument("--top", type=int, default=0, help="how many finalists to score (0 = all)")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--reasoning", default="minimal", choices=["minimal", "low", "medium", "high"])
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--out", default="match_results.json")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key: sys.exit("set OPENAI_API_KEY")

    resume = open(args.resume, encoding="utf-8", errors="replace").read().strip()
    if len(resume) < 100: sys.exit("resume looks empty/too short (give a text resume, not a PDF)")
    cands = json.load(open(args.candidates))
    if args.top: cands = cands[:args.top]
    if not cands: sys.exit("no candidates in " + args.candidates)

    # resume: every finished result is appended to the .jsonl log; skip ids already scored
    log_path = args.out + ".jsonl"
    done = {}
    if os.path.exists(log_path):
        for line in open(log_path):
            try: r = json.loads(line); done[r["id"]] = r
            except Exception: pass
    todo = [c for c in cands if c["id"] not in done]
    sys.stderr.write(f"{len(cands):,} candidates | {len(todo):,} to score, {len(done):,} cached\n")
    if not todo:
        sys.stderr.write("nothing to do\n")
    else:
        jds = fetch_jds(args.parquet, [c["id"] for c in todo])
        logf = open(log_path, "a"); lock = threading.Lock(); n = [0]; t0 = time.time()
        def work(c):
            meta = jds.get(c["id"])
            if not meta or not meta[3].strip():
                with lock: n[0] += 1
                return
            title, company, url, jd = meta
            try:
                m = chat(resume, title, company, jd, key, args.model, args.reasoning)
            except Exception as e:
                with lock: n[0] += 1
                return                                   # failed -> not logged -> retried on rerun
            rec = {**m, "company": company, "title": title, "url": url,
                   "embed_score": c.get("score"), "id": c["id"]}
            with lock:
                logf.write(json.dumps(rec, ensure_ascii=False) + "\n"); logf.flush()
                done[c["id"]] = rec; n[0] += 1
                if n[0] % 50 == 0:
                    rate = n[0] / max(time.time() - t0, 1e-6)
                    sys.stderr.write(f"  {n[0]:,}/{len(todo):,} | {rate:.1f}/s | eta {int((len(todo)-n[0])/max(rate,1e-6))}s\r")
                    sys.stderr.flush()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(as_completed(ex.submit(work, c) for c in todo))
        logf.close()

    results = sorted(done.values(), key=lambda r: -r["match_score"])
    json.dump(results, open(args.out, "w"), ensure_ascii=False, indent=1)
    miss = len(cands) - len(results)
    sys.stderr.write(f"\nscored {len(results):,} -> {args.out}" + (f" ({miss:,} unscored: no JD or failed)" if miss else "") + "\n\n")
    for r in results[:15]:
        print(f"  {r['match_score']:>3} {r['verdict']:<8} {str(r['company'])[:20]:<20} {str(r['title'])[:44]:<44}")
        print(f"       {r['summary'][:96]}")


if __name__ == "__main__":
    main()

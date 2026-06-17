#!/usr/bin/env python3
"""
rank.py - the AGENTS.md "lexical-seed -> learn -> score the whole corpus" pattern, ready to run.

The cheap RECALL stage: hard-filter a candidate set, learn a linear ranker in embedding space from a
seed (positives) vs hard-negatives (same area, not-seed) + easy-negatives, score every candidate,
write matches.json sorted by fit. Then feed matches.json to match.py for LLM precision scoring.

Run it on the downloaded dataset (see download.py). It's a SUBSTRATE -- drive it with flags, or edit
passes()/the seed for exotic searches.

    pip install pyarrow numpy
    python3 rank.py --function engineering --level Senior --country US --remote \
        --seed "backend,distributed-systems,rust,golang,kubernetes,api,kafka,crypto"
    python3 rank.py --function data --seed "ml,llm,inference,feature-store,spark,airflow"   # any role

Output: matches.json (each row: score, id, company, title, url, salary, scope, skills, alt_titles, seed_hits).
"""
import argparse, json, sys
import numpy as np
import pyarrow.parquet as pq

DIM = 1536
COLS = ["id", "company", "company_name", "title", "url", "function", "sub_function", "level",
        "work_mode", "remote_scope", "country_code", "is_staffing", "management",
        "salary_min_k", "salary_max_k", "salary_currency", "alt_titles", "skills", "jd_embedding"]


def hay(r):  # text the seed/skills match against: title + alt_titles + skills + sub_function
    return " ".join([(r["title"] or "").lower(), *(r["alt_titles"] or []), *(r["skills"] or []),
                     (r["sub_function"] or "").lower()])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="open-jobs.parquet")
    ap.add_argument("--out", default="matches.json")
    ap.add_argument("--seed", required=True, help="comma terms defining the target role (drives the ranker)")
    ap.add_argument("--function", default="", help="comma list to gate candidates (e.g. engineering)")
    ap.add_argument("--level", default="", help="comma list (e.g. Senior or Senior,Staff)")
    ap.add_argument("--country", default="", help="ISO-2 (e.g. US); matches country_code or US-scoped remote")
    ap.add_argument("--remote", action="store_true", help="require fully_remote/remote_first")
    ap.add_argument("--min-comp", type=float, default=0, help="annual USD floor on salary_max_k")
    ap.add_argument("--include-staffing", action="store_true")
    ap.add_argument("--include-management", action="store_true")
    ap.add_argument("--lam", type=float, default=1.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    seed = [s.strip().lower() for s in args.seed.split(",") if s.strip()]
    funcs = {x.strip() for x in args.function.split(",") if x.strip()}
    levels = {x.strip() for x in args.level.split(",") if x.strip()}
    country = args.country.strip().upper()

    def country_ok(r):
        if not country: return True
        if r["country_code"] == country: return True
        return country == "US" and r["remote_scope"] in ("us-only", "us-canada")

    def passes(r):                                   # the candidate SET (objective filters)
        if funcs and r["function"] not in funcs: return False
        if levels and r["level"] not in levels: return False
        if args.remote and r["work_mode"] not in ("fully_remote", "remote_first"): return False
        if not country_ok(r): return False
        if args.min_comp and not ((r["salary_max_k"] or -1) >= args.min_comp): return False
        if r["is_staffing"] and not args.include_staffing: return False
        if r["management"] and not args.include_management: return False
        if r["salary_currency"] and country == "US" and r["salary_currency"] != "USD": return False
        return True

    def is_seed(r): return any(s in hay(r) for s in seed)
    in_func = lambda r: (not funcs) or r["function"] in funcs

    pf = pq.ParquetFile(args.inp)
    pos, hard, easy, cand_vec, cand_meta = [], [], [], [], []
    POS_CAP = HARD_CAP = EASY_CAP = 8000
    seen = 0
    sys.stderr.write("streaming jd_embeddings...\n")
    for b in pf.iter_batches(batch_size=20000, columns=COLS):
        e = b.column("jd_embedding")
        mask = np.asarray(e.is_valid())
        if not mask.any(): continue
        vecs = e.drop_null().flatten().to_numpy(zero_copy_only=False).reshape(-1, DIM).astype("float32")
        cols = b.to_pydict()
        for vrow, gi in enumerate(np.nonzero(mask)[0]):
            r = {k: cols[k][gi] for k in COLS if k != "jd_embedding"}
            v = vecs[vrow]; sw = is_seed(r)
            if in_func(r) and sw and len(pos) < POS_CAP: pos.append(v)
            elif in_func(r) and (not sw) and len(hard) < HARD_CAP: hard.append(v)
            elif (not in_func(r)) and len(easy) < EASY_CAP: easy.append(v)
            if passes(r):
                cand_vec.append(v); cand_meta.append((r, sorted({s for s in seed if s in hay(r)})))
        seen += int(mask.sum()); sys.stderr.write(f"  {seen:,} | cands {len(cand_vec):,}\r"); sys.stderr.flush()

    if not cand_vec: sys.exit("\nno candidates matched the filters")
    sys.stderr.write(f"\ntrain {len(pos):,}+/{len(hard):,} hard-/{len(easy):,} easy- | {len(cand_vec):,} candidates\n")
    X = np.vstack([pos, hard, easy]).astype("float32"); X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    y = np.r_[np.ones(len(pos)), -np.ones(len(hard) + len(easy))]
    w = np.linalg.solve(X.T @ X + args.lam * np.eye(DIM), X.T @ y)
    C = np.vstack(cand_vec).astype("float32"); C /= (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    scores = C @ w

    out, seen_key = [], set()
    for s, (r, hits) in sorted(zip(scores, cand_meta), key=lambda t: -t[0]):
        key = (str(r["company_name"] or r["company"]).lower(), str(r["title"]).lower())
        if key in seen_key: continue
        seen_key.add(key)
        out.append({"score": round(float(s), 4), "id": r["id"], "company": r["company_name"] or r["company"],
                    "title": r["title"], "url": r["url"], "level": r["level"], "remote_scope": r["remote_scope"],
                    "country_code": r["country_code"], "salary_min_k": r["salary_min_k"],
                    "salary_max_k": r["salary_max_k"], "sub_function": r["sub_function"],
                    "skills": r["skills"], "alt_titles": r["alt_titles"], "seed_hits": hits})
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
    sys.stderr.write(f"wrote {len(out):,} ranked matches -> {args.out}\n\n")
    for m in out[:args.top]:
        sal = f"${int(m['salary_min_k'])}-{int(m['salary_max_k']) if m['salary_max_k']>0 else '?'}k" if (m['salary_min_k'] or 0) > 0 else ""
        print(f"  [{m['score']:.3f}] {str(m['company'])[:22]:<22} {str(m['title'])[:46]:<46} {m['remote_scope'] or '':<9} {sal}")


if __name__ == "__main__":
    main()

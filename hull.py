#!/usr/bin/env python3
"""
hull.py - STEP 1 of the hull -> learn -> rank pipeline. Compute the "convex hull" of a search: the
MOST SPECIFIC filter that STRICTLY CONTAINS every relevant role.

Filter only on HARD ELIGIBILITY (function, level, location, work authorization, comp floor) and broad
role RECALL (title / alt-title terms) -- never on soft fit. Fit is exactly what ranking decides in
steps 2-3; excluding on it here would throw away relevant results. Keep unknown-value rows: a -1
salary means "not stated", not "too low", so a comp floor keeps it.

    pip install pyarrow
    python3 hull.py --function engineering --level Senior,Staff --country US --remote \
        --title "software engineer,backend,platform,distributed systems" --out hull.json

Output: hull.json, a deduped list of candidates (id + key fields) to feed langsort.py.
"""
import argparse, json, sys
import pyarrow.parquet as pq

COLS = ["id", "company", "company_name", "title", "url", "function", "sub_function", "level",
        "work_mode", "is_remote", "remote_scope", "country_code", "country_required", "is_staffing",
        "management", "salary_min_k", "salary_max_k", "salary_currency", "visa_sponsorship",
        "alt_titles", "skills"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="open-jobs.parquet")
    ap.add_argument("--out", default="hull.json")
    ap.add_argument("--function", default="", help="comma list, e.g. engineering")
    ap.add_argument("--level", default="", help="comma list, e.g. Senior,Staff")
    ap.add_argument("--country", default="", help="ISO-2; US also matches us-only / us-canada remote scope")
    ap.add_argument("--remote", action="store_true", help="require fully_remote / remote_first")
    ap.add_argument("--title", default="", help="comma lexical terms; keep rows whose title OR alt_titles contain any")
    ap.add_argument("--min-comp", type=float, default=0, help="USD floor on salary_max_k (keeps unknown -1 rows)")
    ap.add_argument("--include-staffing", action="store_true", help="keep staffing/agency listings (default: drop)")
    ap.add_argument("--include-management", action="store_true", help="keep people-management roles (default: drop)")
    ap.add_argument("--require-visa", action="store_true", help="keep only roles offering visa sponsorship")
    args = ap.parse_args()

    funcs = {x.strip() for x in args.function.split(",") if x.strip()}
    levels = {x.strip() for x in args.level.split(",") if x.strip()}
    terms = [t.strip().lower() for t in args.title.split(",") if t.strip()]
    country = args.country.strip().upper()

    def country_ok(r):
        if not country: return True
        if r["country_code"] == country: return True
        return country == "US" and r["remote_scope"] in ("us-only", "us-canada")

    def title_ok(r):
        if not terms: return True
        fields = [(r["title"] or "").lower(), *[(a or "").lower() for a in (r["alt_titles"] or [])]]
        return any(t in f for f in fields for t in terms)

    def passes(r):                                       # only HARD eligibility + recall; never soft fit
        if funcs and r["function"] not in funcs: return False
        if levels and r["level"] not in levels: return False
        if args.remote and r["work_mode"] not in ("fully_remote", "remote_first"): return False
        if not country_ok(r): return False
        if args.min_comp and not ((r["salary_max_k"] or -1) >= args.min_comp): return False
        if r["is_staffing"] and not args.include_staffing: return False
        if r["management"] and not args.include_management: return False
        if args.require_visa and r["visa_sponsorship"] != "yes": return False
        return title_ok(r)

    pf = pq.ParquetFile(args.inp)
    out, seen, n = [], set(), 0
    sys.stderr.write(f"streaming {args.inp} (structured fields only, memory-bounded)...\n")
    for b in pf.iter_batches(batch_size=20000, columns=COLS):
        cols = b.to_pydict()
        for i in range(len(cols["id"])):
            n += 1
            r = {k: cols[k][i] for k in COLS}
            if not passes(r): continue
            key = (str(r["company_name"] or r["company"]).lower(), str(r["title"]).lower())
            if key in seen: continue                     # collapse the same role cross-posted
            seen.add(key)
            out.append({"id": r["id"], "company": r["company_name"] or r["company"], "title": r["title"],
                        "url": r["url"], "level": r["level"], "sub_function": r["sub_function"],
                        "remote_scope": r["remote_scope"], "country_code": r["country_code"],
                        "salary_min_k": r["salary_min_k"], "salary_max_k": r["salary_max_k"],
                        "skills": r["skills"], "alt_titles": r["alt_titles"]})
        sys.stderr.write(f"  {n:,} scanned | {len(out):,} in hull\r"); sys.stderr.flush()

    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
    sys.stderr.write(f"\nconvex hull: {len(out):,} unique roles -> {args.out}\n")
    if not out: sys.stderr.write("EMPTY: loosen a filter (the hull must CONTAIN every relevant role).\n")
    else: sys.stderr.write(f"next: python3 langsort.py --resume resume.txt --candidates {args.out} --mode sample\n")
    sys.stderr.write("\n")


if __name__ == "__main__":
    main()

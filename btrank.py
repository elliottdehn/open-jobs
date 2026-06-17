#!/usr/bin/env python3
"""
btrank.py - STEP 3 of the hull -> learn -> rank pipeline. Collapse the pairwise decisions langsort.py
gathered (langsort_decisions.jsonl) into ONE ranking via Bradley-Terry: each role gets a strength fit
from its wins and losses, which is robust to the uneven comparison counts sampling produces. Individual
LLM decisions can be intransitive (A>B>C>A); Bradley-Terry can't represent a cycle, so it finds the
single global order that satisfies the most decisions -- it denoises the comparisons rather than
following any one path.

Reports how well the fitted order agrees with the raw decisions (the LLM's self-consistency). Roles
never compared are appended at the end, ordered by --match score if given.

Optionally (--distill-out) it learns a linear model over the job EMBEDDINGS from the same decisions
(Bradley-Terry with a linear utility = logistic regression on embedding differences), saved in the
score_distill.py format so you can score the WHOLE corpus, not just the hull, with no more LLM calls.

    python3 btrank.py --candidates hull.json --decisions langsort_decisions.jsonl --out ranked.json
    python3 btrank.py --candidates hull.json --decisions langsort_decisions.jsonl \
        --distill-out taste.npz --parquet open-jobs.parquet      # also learn a corpus-wide ranker
"""
import argparse, json, math, sys
from collections import defaultdict


def bradley_terry(items, wins, games, iters=300, alpha=0.5):
    # MM iteration, lightly smoothed toward a reference strength 1 (alpha phantom win + loss) so
    # undefeated/winless roles stay finite.
    p = {i: 1.0 for i in items}
    for _ in range(iters):
        new = {}
        for i in items:
            denom = sum(games[i][j] / (p[i] + p[j]) for j in games[i]) + 2 * alpha / (p[i] + 1.0)
            new[i] = (wins[i] + alpha) / denom if denom > 0 else p[i]
        gm = math.exp(sum(math.log(max(v, 1e-12)) for v in new.values()) / len(new))
        p = {i: v / gm for i, v in new.items()}
    return p


def distill(decisions, parquet, out_path):
    import numpy as np, pyarrow.parquet as pq
    DIM = 1536
    ids = {d["a_id"] for d in decisions} | {d["b_id"] for d in decisions}
    emb = {}
    sys.stderr.write(f"loading {len(ids):,} embeddings to distill...\n")
    for b in pq.ParquetFile(parquet).iter_batches(batch_size=20000, columns=["id", "jd_embedding"]):
        idl = b.column("id").to_pylist(); col = b.column("jd_embedding")
        valid = np.asarray(col.is_valid())
        vecs = col.drop_null().flatten().to_numpy(zero_copy_only=False).reshape(-1, DIM).astype("float32")
        vi = 0
        for k, ok in enumerate(valid):
            if ok:
                if idl[k] in ids and idl[k] not in emb: emb[idl[k]] = vecs[vi]
                vi += 1
        if len(emb) >= len(ids): break
    pairs = [(d["a_id"], d["b_id"], 1.0 if d["winner"] == "A" else 0.0)
             for d in decisions if d["a_id"] in emb and d["b_id"] in emb]
    if not pairs: sys.exit("no embeddings found for the decisions; cannot distill")

    keys = list(emb); E = np.vstack([emb[i] for i in keys]); mean = E.mean(0)
    _, _, Vt = np.linalg.svd(E - mean, full_matrices=False); V = Vt[:64].T   # PCA 1536 -> 64
    Z = (E - mean) @ V; zmu = Z.mean(0); zsd = Z.std(0) + 1e-9
    pj = {k: ((emb[k] - mean) @ V - zmu) / zsd for k in keys}                # standardized scores
    X = np.array([pj[a] - pj[b] for a, b, _ in pairs])
    y = np.array([t for _, _, t in pairs])
    w = np.zeros(64); lr, lam = 0.5, 1.0                                     # logistic GD on differences
    for _ in range(800):
        pr = 1.0 / (1.0 + np.exp(-(X @ w)))
        w -= lr * (X.T @ (pr - y) / len(y) + lam * w / len(y))
    np.savez(out_path, mean=mean, V=V, zmu=zmu, zsd=zsd, w=np.r_[w, 0.0], lam=float(lam))
    acc = (((1.0 / (1.0 + np.exp(-(X @ w)))) > 0.5).astype(float) == y).mean()
    sys.stderr.write(f"distilled -> {out_path} | {len(pairs):,} pairs | {acc*100:.1f}% train pair accuracy\n"
                     f"score the whole corpus: python3 score_distill.py --models {out_path} --in {parquet}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="hull.json", help="the hull, for labels/urls + the uncompared tail")
    ap.add_argument("--decisions", default="langsort_decisions.jsonl")
    ap.add_argument("--out", default="ranked.json")
    ap.add_argument("--match", default="", help="optional match_results.json; orders never-compared roles by its score")
    ap.add_argument("--distill-out", default="", help="also fit a corpus-wide model to FILE.npz (needs --parquet)")
    ap.add_argument("--parquet", default="open-jobs.parquet")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    dec = [json.loads(l) for l in open(args.decisions) if l.strip()]
    if not dec: sys.exit("no decisions in " + args.decisions)
    info = {c["id"]: c for c in json.load(open(args.candidates))}
    mscore = {}
    if args.match:
        try: mscore = {r["id"]: r.get("match_score") for r in json.load(open(args.match))}
        except Exception as e: sys.stderr.write(f"(ignoring --match: {e})\n")

    wins, games = defaultdict(float), defaultdict(lambda: defaultdict(float))
    for d in dec:
        a, b = d["a_id"], d["b_id"]
        games[a][b] += 1; games[b][a] += 1; wins[d["winner_id"]] += 1
    items = list(games)
    p = bradley_terry(items, wins, games)

    ranked = sorted(items, key=lambda i: -p[i])
    tail = sorted((i for i in info if i not in set(items)), key=lambda i: -(mscore.get(i) or 0))
    ng = lambda i: int(sum(games[i].values()))
    out = []
    for i in ranked:
        c = info.get(i, {})
        out.append({"id": i, "company": c.get("company"), "title": c.get("title"), "url": c.get("url"),
                    "bt_strength": round(p[i], 4), "games": ng(i), "wins": int(wins[i]),
                    "match_score": mscore.get(i), "compared": True})
    for i in tail:
        c = info.get(i, {})
        out.append({"id": i, "company": c.get("company"), "title": c.get("title"), "url": c.get("url"),
                    "bt_strength": None, "games": 0, "wins": 0, "match_score": mscore.get(i), "compared": False})
    for n, r in enumerate(out): r["rank"] = n + 1
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)

    loser = lambda d: d["a_id"] if d["winner_id"] == d["b_id"] else d["b_id"]
    agree = sum(1 for d in dec if p.get(d["winner_id"], 0) > p.get(loser(d), 0)) / max(len(dec), 1)
    g = sorted(ng(i) for i in items); med = g[len(g) // 2] if g else 0
    sys.stderr.write(f"\n{len(dec):,} decisions | {len(items):,}/{len(info):,} roles compared "
                     f"({len(tail):,} not yet) | median {med} comparisons/role\n")
    sys.stderr.write(f"fitted order agrees with {agree*100:.1f}% of raw decisions "
                     f"(higher = the LLM was nearly a clean linear order)\n-> {args.out}\n\n")
    for r in out[:args.top]:
        tag = f"{r['wins']}W/{r['games']}" if r["compared"] else "uncompared"
        print(f"  #{r['rank']:<3} {str(r['company'])[:22]:<22} {str(r['title'])[:42]:<42} [{tag}]")

    if args.distill_out:
        distill(dec, args.parquet, args.distill_out)


if __name__ == "__main__":
    main()

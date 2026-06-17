#!/usr/bin/env python3
"""
btrank.py - STEP 3 of the hull -> learn -> rank pipeline. Turn the pairwise decisions langsort.py
gathered (langsort_decisions.jsonl) into one ranking.

The default treats the LLM's decisions as GOLD: each is an edge (winner ranked above loser), and
together they define a partial order. btrank topologically sorts it, so the final ranking respects
every decision it can, and uses a model distilled from the same decisions (logistic regression over
the job embeddings) only to DISAMBIGUATE: to order roles the decisions leave incomparable, to place
roles never compared (and, via --distill-out, the whole corpus), and to break cycles if any exist
(strongly-connected components are condensed first). On a real ~2,600-role hull this fits held-out
decisions ~90%, vs ~68% for the model alone, because the decisions' transitive structure carries far
more than the embeddings do. In practice the decision graph is acyclic, so the gold order honors 100%
of decisions and the model only fills the gaps.

    python3 btrank.py --candidates hull.json --decisions langsort_decisions.jsonl \
        --parquet open-jobs.parquet --out ranked.json
    python3 btrank.py ... --distill-out taste.npz        # also save a corpus-wide ranker

--method: gold (default), fuse (softer blend; lets the model override sparse decisions, but bottlenecks
at the embedding ceiling), or bt (plain Bradley-Terry, no embeddings needed).
"""
import argparse, json, math, sys, heapq


def load_dec(path):
    dec = [json.loads(l) for l in open(path) if l.strip()]
    if not dec: sys.exit("no decisions in " + path)
    return dec


def model_scores(dec, candidates, parquet):
    """Load candidate embeddings, PCA(64)+standardize, fit logistic on decision differences.
    Returns (roles, ri, R, s, A, B, Y, model_dict): per-role model log-strength s, decisions as
    role-index arrays, and the model in score_distill.py's npz format."""
    import numpy as np, pyarrow.parquet as pq
    DIM = 1536
    info = {c["id"]: c for c in candidates}
    roles = list(info); ri = {r: i for i, r in enumerate(roles)}; R = len(roles)
    idset = set(roles); emb = {}
    sys.stderr.write(f"loading embeddings for {R:,} candidates...\n")
    for b in pq.ParquetFile(parquet).iter_batches(batch_size=20000, columns=["id", "jd_embedding"]):
        idl = b.column("id").to_pylist(); col = b.column("jd_embedding"); valid = np.asarray(col.is_valid())
        vecs = col.drop_null().flatten().to_numpy(zero_copy_only=False).reshape(-1, DIM).astype("float32")
        vi = 0
        for k, ok in enumerate(valid):
            if ok:
                if idl[k] in idset and idl[k] not in emb: emb[idl[k]] = vecs[vi]
                vi += 1
        if len(emb) >= len(idset): break
    have = [r for r in roles if r in emb]
    if not have: sys.exit("no embeddings for candidates; need --parquet with a jd_embedding column")
    Eh = np.vstack([emb[r] for r in have]); mean = Eh.mean(0)
    _, _, Vt = np.linalg.svd(Eh - mean, full_matrices=False); V = Vt[:64].T
    Z = (Eh - mean) @ V; zmu = Z.mean(0); zsd = Z.std(0) + 1e-9
    Pr = np.zeros((R, 64))
    for r in have: Pr[ri[r]] = ((emb[r] - mean) @ V - zmu) / zsd
    A, B, Y = [], [], []
    for d in dec:
        if d["a_id"] in ri and d["b_id"] in ri:
            A.append(ri[d["a_id"]]); B.append(ri[d["b_id"]]); Y.append(1.0 if d["winner"] == "A" else 0.0)
    A, B, Y = np.array(A, int), np.array(B, int), np.array(Y)
    he = np.array([(roles[A[k]] in emb and roles[B[k]] in emb) for k in range(len(A))], bool) if len(A) else np.array([], bool)
    w = np.zeros(64)
    if he.any():
        Xm = Pr[A[he]] - Pr[B[he]]; ym = Y[he]
        for _ in range(800):
            pr = 1 / (1 + np.exp(-(Xm @ w))); w -= 0.5 * (Xm.T @ (pr - ym) / len(ym) + w / len(ym))
    s = Pr @ w
    model_dict = {"mean": mean, "V": V, "zmu": zmu, "zsd": zsd, "w": np.r_[w, 0.0], "lam": 1.0}
    return roles, ri, R, s, A, B, Y, model_dict


def gold_rank(R, s, A, B, Y):
    """Gold partial order (winner -> loser) topologically sorted, with s disambiguating: cycles
    condensed into SCCs, ready components ordered by their best model score, ties within a component
    or among incomparable roles broken by s. Returns (order, fidelity, in_cycle, cnt)."""
    import numpy as np
    adj = [[] for _ in range(R)]; radj = [[] for _ in range(R)]
    cnt = np.zeros(R)
    for k in range(len(A)):
        a, b = (A[k], B[k]) if Y[k] > 0.5 else (B[k], A[k])      # a beats b -> a ranked above b
        adj[a].append(b); radj[b].append(a); cnt[a] += 1; cnt[b] += 1
    seen = np.zeros(R, bool); fin = []                            # Kosaraju pass 1: finish order
    for st in range(R):
        if seen[st]: continue
        stk = [(st, 0)]
        while stk:
            u, pi = stk[-1]
            if pi == 0: seen[u] = True
            if pi < len(adj[u]):
                stk[-1] = (u, pi + 1); v = adj[u][pi]
                if not seen[v]: stk.append((v, 0))
            else:
                fin.append(u); stk.pop()
    comp = np.full(R, -1, int); c = 0                             # pass 2 on the transpose
    for st in reversed(fin):
        if comp[st] >= 0: continue
        stk = [st]; comp[st] = c
        while stk:
            u = stk.pop()
            for v in radj[u]:
                if comp[v] < 0: comp[v] = c; stk.append(v)
        c += 1
    cnodes = [[] for _ in range(c)]
    for i in range(R): cnodes[comp[i]].append(i)
    cscore = [max(s[i] for i in cn) for cn in cnodes]
    cadj = [set() for _ in range(c)]; indeg = [0] * c
    for a in range(R):
        for b in adj[a]:
            if comp[a] != comp[b] and comp[b] not in cadj[comp[a]]:
                cadj[comp[a]].add(comp[b]); indeg[comp[b]] += 1
    h = [(-cscore[ci], ci) for ci in range(c) if indeg[ci] == 0]; heapq.heapify(h)
    order = []
    while h:
        _, ci = heapq.heappop(h)
        for i in sorted(cnodes[ci], key=lambda x: -s[x]): order.append(i)
        for cj in cadj[ci]:
            indeg[cj] -= 1
            if indeg[cj] == 0: heapq.heappush(h, (-cscore[cj], cj))
    pos = np.empty(R, int); pos[order] = np.arange(R)
    if len(A):
        atr = np.where(Y > 0.5, A, B); btr = np.where(Y > 0.5, B, A)
        fidelity = float((pos[atr] < pos[btr]).mean())
    else:
        fidelity = 1.0
    in_cycle = sum(len(cn) for cn in cnodes if len(cn) > 1)
    return order, fidelity, in_cycle, cnt


def soft_fuse(R, s, A, B, Y, lam=1.0, iters=500, lr=0.5):
    """BT in log-strength space, initialized at the model prior s and shrunk back toward it."""
    import numpy as np
    th = s.copy(); cnt = np.zeros(R)
    if len(A): np.add.at(cnt, A, 1); np.add.at(cnt, B, 1)
    for _ in range(iters):
        g = np.zeros(R)
        if len(A):
            pr = 1 / (1 + np.exp(-(th[A] - th[B])))
            np.add.at(g, A, Y - pr); np.add.at(g, B, -(Y - pr)); g /= len(Y)
        g -= lam * (th - s) / R; th += lr * g
    return th, cnt


def bradley_terry(items, wins, games, iters=300, alpha=0.5):
    p = {i: 1.0 for i in items}
    for _ in range(iters):
        new = {}
        for i in items:
            denom = sum(games[i][j] / (p[i] + p[j]) for j in games[i]) + 2 * alpha / (p[i] + 1.0)
            new[i] = (wins[i] + alpha) / denom if denom > 0 else p[i]
        gm = math.exp(sum(math.log(max(v, 1e-12)) for v in new.values()) / len(new))
        p = {i: v / gm for i, v in new.items()}
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="hull.json", help="the hull (id + labels/urls)")
    ap.add_argument("--decisions", default="langsort_decisions.jsonl")
    ap.add_argument("--out", default="ranked.json")
    ap.add_argument("--method", default="gold", choices=["gold", "fuse", "bt"],
                    help="gold = decisions as a gold partial order, model disambiguates (default); "
                         "fuse = softer blend; bt = plain Bradley-Terry (no embeddings)")
    ap.add_argument("--parquet", default="open-jobs.parquet", help="needed for gold/fuse and --distill-out")
    ap.add_argument("--distill-out", default="", help="also save the model as a corpus-wide ranker (FILE.npz)")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    dec = load_dec(args.decisions)
    info = {c["id"]: c for c in json.load(open(args.candidates))}
    candidates = list(info.values())

    if args.method == "bt":
        from collections import defaultdict
        wins, games = defaultdict(float), defaultdict(lambda: defaultdict(float))
        for d in dec:
            games[d["a_id"]][d["b_id"]] += 1; games[d["b_id"]][d["a_id"]] += 1; wins[d["winner_id"]] += 1
        items = list(games); p = bradley_terry(items, wins, games)
        ranked = sorted(items, key=lambda i: -p[i])
        out = [{"rank": n + 1, "id": i, "company": info.get(i, {}).get("company"),
                "title": info.get(i, {}).get("title"), "url": info.get(i, {}).get("url"),
                "bt_strength": round(p[i], 4), "comparisons": int(sum(games[i].values())), "compared": True}
               for n, i in enumerate(ranked)]
        for i in (set(info) - set(items)):
            out.append({"rank": len(out) + 1, "id": i, "company": info[i].get("company"),
                        "title": info[i].get("title"), "url": info[i].get("url"),
                        "bt_strength": None, "comparisons": 0, "compared": False})
        json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
        sys.stderr.write(f"\nBradley-Terry ranked {len(items):,} compared roles -> {args.out}\n\n")
        for r in out[:args.top]:
            print(f"  #{r['rank']:<3} {str(r['company'])[:22]:<22} {str(r['title'])[:42]:<42}")
        return

    # gold / fuse both need the distilled model
    roles, ri, R, s, A, B, Y, model_dict = model_scores(dec, candidates, args.parquet)
    if args.method == "gold":
        order, fidelity, in_cycle, cnt = gold_rank(R, s, A, B, Y)
        out = []
        for rank, i in enumerate(order):
            c = info[roles[i]]
            out.append({"rank": rank + 1, "id": roles[i], "company": c.get("company"), "title": c.get("title"),
                        "url": c.get("url"), "model_score": round(float(s[i]), 4),
                        "comparisons": int(cnt[i]), "compared": bool(cnt[i] > 0)})
        ncomp = int((cnt > 0).sum())
        sys.stderr.write(f"\ngold partial order over {R:,} roles ({ncomp:,} compared, {R-ncomp:,} placed by "
                         f"imputation) from {len(A):,} decisions\n")
        sys.stderr.write(f"gold fidelity {fidelity*100:.1f}% of decisions honored | "
                         f"{in_cycle:,} roles in cycles (disambiguated by model) -> {args.out}\n\n")
    else:  # fuse
        import numpy as np
        th, cnt = soft_fuse(R, s, A, B, Y)
        order = list(np.argsort(-th))
        out = []
        for rank, i in enumerate(order):
            c = info[roles[i]]
            out.append({"rank": rank + 1, "id": roles[i], "company": c.get("company"), "title": c.get("title"),
                        "url": c.get("url"), "fused_strength": round(float(th[i]), 4),
                        "model_prior": round(float(s[i]), 4), "comparisons": int(cnt[i]),
                        "compared": bool(cnt[i] > 0)})
        agree = float(np.mean((th[A] > th[B]) == (Y > 0.5))) if len(A) else 0.0
        sys.stderr.write(f"\nsoft fusion over {R:,} roles | agrees with {agree*100:.1f}% of decisions -> {args.out}\n\n")
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
    for r in out[:args.top]:
        tag = f"{r['comparisons']}cmp" if r["compared"] else "imputed"
        print(f"  #{r['rank']:<3} {str(r['company'])[:22]:<22} {str(r['title'])[:42]:<42} [{tag}]")

    if args.distill_out:
        import numpy as np
        np.savez(args.distill_out, **model_dict)
        sys.stderr.write(f"\nsaved corpus-wide model -> {args.distill_out}"
                         f"  (score all jobs: python3 score_distill.py --models {args.distill_out})\n")


if __name__ == "__main__":
    main()

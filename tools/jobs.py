# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "duckdb>=1.1"]
# ///
"""open-jobs agent toolchain. Everything runs locally except one embedding call.

  uv run tools/jobs.py embed  --file work/ideal-jd.md            -> work/ideal.json (vector + recipe)
  uv run tools/jobs.py groups [--k 30] [--min-sim 0]              -> nearest groups (id, size, label, exemplars)
  uv run tools/jobs.py fetch  --groups 12,45,301 [--top N]        -> download groups -> work/jobs.parquet
  uv run tools/jobs.py html   [--out work/search.html]            -> single-file search UI over work/jobs.parquet
  uv run tools/jobs.py serve  [--port 8765]                       -> serve work/ + record interactions to work/interactions.jsonl
  uv run tools/jobs.py enrich [--top N | --all]                   -> structured extraction + company for the slice (metered per IP: $5/h, $50/day) -> work/enrichment.json
  uv run tools/jobs.py rank   [--labels work/interactions.jsonl]  -> re-rank with a classifier trained on labels -> work/ranked.csv
  uv run tools/jobs.py status                                     -> what's in work/

Env: WORKER_URL (default https://backend.dehnbostele.workers.dev), WORK (default work/).
"""
import argparse, base64, json, os, sys, time, urllib.request, urllib.error, math, re
import numpy as np
np.seterr(all="ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locparse import parse as parse_location, eligibility as loc_eligibility
from seniority import extract as seniority_of, FROM_ENRICH as SENIORITY_FROM_ENRICH
from salary import extract as extract_salary  # Apple Accelerate emits spurious divide/overflow warnings on large matmuls

BASE = os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev")
DATA = os.environ.get("DATA_URL", BASE)  # where /data/* is served from (override for a local mirror)
WORK = os.environ.get("WORK", "work")
UA = {"user-agent": "open-jobs-tools/0.1"}
os.makedirs(WORK, exist_ok=True)

def get(path, binary=False):
    req = urllib.request.Request(f"{DATA if path.startswith('/data/') else BASE}{path}", headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read() if binary else json.loads(r.read())

def manifest():
    p = os.path.join(WORK, "manifest.json"); c = os.path.join(WORK, "centroids.bin")
    if not (os.path.exists(p) and os.path.exists(c)) or time.time() - os.path.getmtime(p) > 6 * 3600:
        m = get("/data/manifest.json"); open(p, "w", encoding="utf-8").write(json.dumps(m))
        open(c, "wb").write(get("/data/centroids.bin", binary=True))
    m = json.load(open(p, encoding="utf-8"))
    C = np.fromfile(c, dtype=np.float16).astype(np.float32).reshape(-1, m["dims"])
    return m, C

def ideal():
    p = os.path.join(WORK, "ideal.json")
    if not os.path.exists(p): sys.exit("no work/ideal.json — run `embed --file work/ideal-jd.md` first")
    d = json.load(open(p, encoding="utf-8")); v = np.asarray(d["vector"], dtype=np.float32); v /= np.linalg.norm(v) + 1e-9
    return d, v

def cmd_embed(a):
    text = open(a.file, encoding="utf-8").read()
    body = json.dumps({"text": text, "title": a.title or "", "location": a.location or ""}).encode()
    req = urllib.request.Request(f"{BASE}/embed", data=body, headers={**UA, "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r: d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"embed failed: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
    out = {"vector": d["vector"], "recipe": d["recipe"], "source": a.file, "embedded_at": int(time.time() * 1000), "title": a.title, "location": a.location}
    json.dump(out, open(os.path.join(WORK, "ideal.json"), "w", encoding="utf-8"))
    print(f"embedded {a.file} -> {WORK}/ideal.json ({d['recipe']})")

def nice_company(c):
    return "" if re.fullmatch(r"[0-9a-fA-F-]{20,}", c or "") else (c or "")

def node_label(n):
    ex = "; ".join(f"{e['title']}" + (f" @ {nice_company(e['company'])}" if nice_company(e['company']) else "") for e in n["exemplars"][:4])
    return f"{n['label']}  [{ex}]"

def nearest(m, C, v, k, min_sim=0.0):
    """Rank every leaf by centroid similarity (all centroids are local; ball bounds are too loose in
    1536-d to prune). Returns [(leaf, sim)] best first."""
    T = m["tree"]
    sims = C @ v
    leaves = [n for n in T if not n["children"]]
    leaves.sort(key=lambda n: -sims[n["id"]])
    return [(n, float(sims[n["id"]])) for n in leaves if sims[n["id"]] >= min_sim][:k]

def cmd_groups(a):
    m, C = manifest(); d, v = ideal()
    if d["recipe"] != m["recipe"]: print(f"warning: ideal.json recipe {d['recipe']} != manifest {m['recipe']}; re-run embed", file=sys.stderr)
    rows = nearest(m, C, v, a.k, a.min_sim)
    print(f"{m['jobs']:,} jobs in {m['leaves']:,} groups (built {time.strftime('%Y-%m-%d', time.localtime(m['built_at']/1000))}). Nearest to your ideal JD:\n")
    print(f"{'id':>6} {'sim':>5} {'jobs':>6} {'titles':>6}  label  [exemplars]")
    for n, s in rows:
        print(f"{n['id']:>6} {s:5.2f} {n['size']:>6} {n.get('distinct_titles', ''):>6}  {node_label(n)[:150]}")
    json.dump([{"id": n["id"], "sim": s, "size": n["size"], "label": n["label"], "exemplars": n["exemplars"]} for n, s in rows], open(os.path.join(WORK, "groups.json"), "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {WORK}/groups.json. Next: `fetch --groups <ids>` (maybe) — or `fetch --top N` for the N nearest.")

def leaves_under(m, n):
    T = m["tree"]; out = []; st = [n]
    while st:
        x = st.pop()
        if not x["children"]: out.append(x)
        else: st.extend(T[c] for c in x["children"])
    return out

def cmd_fetch(a):
    import duckdb
    m, C = manifest(); d, v = ideal()
    T = m["tree"]
    ids = [int(x) for x in a.groups.split(",")] if a.groups else [n["id"] for n, _ in nearest(m, C, v, a.top)]
    leaves = []
    for i in ids: leaves.extend(leaves_under(m, T[i]))
    gdir = os.path.join(WORK, "groups"); os.makedirs(gdir, exist_ok=True)
    rows = []; total = 0
    for li, leaf in enumerate(leaves):
        p = os.path.join(gdir, f"{leaf['id']}.json")
        if not os.path.exists(p): open(p, "wb").write(get(f"/data/groups/{leaf['id']}.json", binary=True))
        g = json.load(open(p, encoding="utf-8"))
        for j in g["jobs"]:
            vec = np.frombuffer(base64.b64decode(j["v"]), dtype=np.float32)
            rows.append((j["ats"], j["slug"], j["id"], j["title"], j["company"], j["location"], j["url"], j.get("seen") or 0, j.get("jd") or "", leaf["id"], float(vec @ v), j["v"]))
        total += len(g["jobs"])
        print(f"\r{li+1}/{len(leaves)} groups, {total:,} jobs", end="", file=sys.stderr)
    print(file=sys.stderr)
    con = duckdb.connect(os.path.join(WORK, "jobs.duckdb"))
    con.execute("CREATE OR REPLACE TABLE jobs (ats VARCHAR, slug VARCHAR, id VARCHAR, title VARCHAR, company VARCHAR, location VARCHAR, url VARCHAR, seen_ms BIGINT, jd VARCHAR, leaf INTEGER, sim DOUBLE, vec_b64 VARCHAR)")
    con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute(f"COPY (SELECT * FROM jobs) TO '{os.path.join(WORK, 'jobs.parquet')}' (FORMAT PARQUET)")
    print(f"wrote {WORK}/jobs.parquet and {WORK}/jobs.duckdb: {len(rows):,} jobs from {len(leaves)} groups. Columns: ats, slug, id, title, company, location, url, seen_ms, jd, leaf, sim (cosine to ideal JD), vec_b64 (float32 LE base64).")

def post(path, body):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(), headers={**UA, "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, {}

def cmd_enrich(a):
    rows = load_jobs()
    keys = [(r[0], r[1], r[2]) for r in rows]  # ordered by sim desc
    if not a.all: keys = keys[: a.top]
    p = os.path.join(WORK, "enrichment.json")
    store = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"jobs": {}, "boards": {}}
    todo = [k for k in keys if f"{k[0]}/{k[1]}#{k[2]}" not in store["jobs"]]
    print(f"{len(keys)} jobs selected, {len(todo)} not yet enriched locally; sending in batches of 100")
    spent = 0.0
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        code, res = post("/enrich", {"jobs": [{"ats": k[0], "slug": k[1], "id": k[2]} for k in batch]})
        for name, b in (res.get("boards") or {}).items(): store["boards"][name] = b
        for key, j in (res.get("jobs") or {}).items():
            if j.get("status") == "done": store["jobs"][key] = j["enrichment"]
        json.dump(store, open(p, "w", encoding="utf-8"))
        if code == 429:
            print(f"rate limited by the server: spent ${res.get('spent', {}).get('hourUsd', 0):.2f} this hour / ${res.get('spent', {}).get('dayUsd', 0):.2f} today; retry in {res.get('retryAfterSeconds')}s. Saved what came back.")
            break
        if code != 200: print(f"batch failed: HTTP {code} {str(res)[:200]}"); break
        c = res.get("cost", {}); spent += c.get("thisCallUsd", 0)
        print(f"  {min(i + 100, len(todo))}/{len(todo)}: ${c.get('thisCallUsd', 0):.3f} this call, ${c.get('hourUsd', 0):.2f}/${c.get('hourLimit')} this hour, ${c.get('dayUsd', 0):.2f}/${c.get('dayLimit')} today", flush=True)
    print(f"wrote {p}: {len(store['jobs'])} jobs, {len(store['boards'])} boards enriched (this run ≈ ${spent:.2f}). Re-run `html` to use it.")

def load_jobs():
    import duckdb
    p = os.path.join(WORK, "jobs.parquet")
    if not os.path.exists(p): sys.exit("no work/jobs.parquet — run `fetch` first")
    return duckdb.connect().execute(f"SELECT * FROM read_parquet('{p}') ORDER BY sim DESC").fetchall()

def subgroups(vecs, titles, comps, k=6, min_size=8):
    """Split a slice into at most k groups: start with everything as one group and repeatedly bisect
    (2-means) the broadest remaining group (largest spread × size) until there are k.
    Returns (assignment[i] -> gid, {gid: {label, medoid, size, radius, exemplars}})."""
    X = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    N = len(X)
    STOP = set("and or of the for a in to with at on & senior sr jr ii iii i lead staff associate assistant manager specialist engineer".split())
    def norm_t(t): return re.sub(r"[^a-z]+", " ", (t or "").lower()).strip()
    def words(idx, n=4):
        c = {}
        for i in idx:
            for w in re.findall(r"[a-z][a-z+#]+", (titles[i] or "").lower()):
                if w not in STOP and len(w) > 2: c[w] = c.get(w, 0) + 1
        return [w for w, _ in sorted(c.items(), key=lambda x: -x[1])[:n]]
    def spread(idx):
        cen = X[idx].mean(0); cen /= np.linalg.norm(cen) + 1e-9
        return float((1 - X[idx] @ cen).mean()), cen
    def two_means(idx):
        r = np.random.default_rng(len(idx)); c = X[r.choice(idx, 2, replace=False)].copy()
        for _ in range(10):
            lab = ((X[idx] - c[1]) ** 2).sum(1) < ((X[idx] - c[0]) ** 2).sum(1)
            for kk, m in ((0, ~lab), (1, lab)):
                if m.any(): c[kk] = X[idx[m]].mean(0)
        return lab
    leaves = [np.arange(N)]
    while len(leaves) < k:
        # broadest group = mean distance to centroid × sqrt(size); only split if both halves stay >= min_size
        cand = sorted(range(len(leaves)), key=lambda i: -spread(leaves[i])[0] * np.sqrt(len(leaves[i])))
        split = False
        for i in cand:
            idx = leaves[i]
            if len(idx) < 2 * min_size: continue
            lab = two_means(idx); a_, b_ = idx[~lab], idx[lab]
            if len(a_) < min_size or len(b_) < min_size: continue
            leaves[i] = a_; leaves.append(b_); split = True; break
        if not split: break
    assign = np.zeros(N, dtype=np.int64); groups = {}
    for gid, idx in enumerate(leaves):
        assign[idx] = gid
        r, cen = spread(idx); dist = 1 - X[idx] @ cen; order = idx[np.argsort(dist)]; med = order[0]
        ex = [int(med)]; seen = {norm_t(titles[med])}
        for i in order:
            if len(ex) >= 4: break
            if norm_t(titles[i]) not in seen: seen.add(norm_t(titles[i])); ex.append(int(i))
        groups[gid] = {"label": " · ".join(words(idx)), "medoid": titles[med], "size": int(len(idx)), "radius": round(float(dist.max()), 3),
                       "exemplars": [{"title": titles[i], "company": comps[i] or ""} for i in ex]}
    return assign, groups

def salary_model():
    """Salary estimator (ridge on the embedding -> log annual USD), published with the index; cached in work/."""
    p = os.path.join(WORK, "salary-model.json")
    try:
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 24 * 3600:
            open(p, "w", encoding="utf-8").write(json.dumps(get("/data/salary-model.json")))
        m = json.load(open(p, encoding="utf-8")); return np.asarray(m["w"], dtype=np.float32), float(m["b"]), float(m["sigma"]), m
    except Exception:
        return None

def arrangement_model():
    """Work-arrangement estimator (softmax on the embedding -> remote/hybrid/onsite), published with the index; cached in work/."""
    p = os.path.join(WORK, "arrangement-model.json")
    try:
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 24 * 3600:
            open(p, "w", encoding="utf-8").write(json.dumps(get("/data/arrangement-model.json")))
        m = json.load(open(p, encoding="utf-8")); return np.asarray(m["W"], dtype=np.float32), np.asarray(m["b"], dtype=np.float32), m["classes"], float(m.get("threshold", 0.7)), m
    except Exception:
        return None

def location_table():
    """Country estimates for location strings the rules can't place (nearest-neighbour vote over location-string
    embeddings, built by backend/scripts/build-location-table.py); cached in work/, installed into locparse."""
    import locparse
    p = os.path.join(WORK, "location-countries.json")
    try:
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 24 * 3600:
            open(p, "w", encoding="utf-8").write(json.dumps(get("/data/location-countries.json"), ensure_ascii=False))
        t = json.load(open(p, encoding="utf-8")); locparse.LOC_TABLE = t["table"]; return t
    except Exception:
        return None

def seniority_model():
    """Seniority estimator (softmax on the embedding -> title levels), published with the index; cached in work/."""
    p = os.path.join(WORK, "seniority-model.json")
    try:
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > 24 * 3600:
            open(p, "w", encoding="utf-8").write(json.dumps(get("/data/seniority-model.json")))
        m = json.load(open(p, encoding="utf-8")); return np.asarray(m["W"], dtype=np.float32), np.asarray(m["b"], dtype=np.float32), m["classes"], float(m.get("threshold", 0.7)), m
    except Exception:
        return None

def cmd_html(a):
    d, v = ideal()
    rows = load_jobs()
    sm = salary_model(); am = arrangement_model(); lt = location_table(); snm = seniority_model(); n_est_rm = 0; n_est_co = 0; n_est_sn = 0
    # dedupe identical (company, title) postings (multi-location / ATS mirrors): keep the highest-sim one
    seen = set(); uniq = []
    for r in rows:
        key = (re.sub(r"\W+", " ", (r[4] or "").lower()).strip(), re.sub(r"\W+", " ", (r[3] or "").lower()).strip())
        if key in seen: continue
        seen.add(key); uniq.append(r)
    if len(uniq) < len(rows): print(f"deduped {len(rows) - len(uniq)} repeated (company, title) postings")
    rows = uniq
    # "never show <company> again" clicks are logged as hide_company events; honor the final state at compile time
    hidden = {}
    ip = os.path.join(WORK, "interactions.jsonl")
    if os.path.exists(ip):
        for line in open(ip, encoding="utf-8"):
            try: ev = json.loads(line)
            except Exception: continue
            if ev.get("type") == "hide_company" and ev.get("board"):
                if ev.get("on", True): hidden[ev["board"]] = ev.get("company") or ev["board"]
                else: hidden.pop(ev["board"], None)
    if hidden:
        before = len(rows); rows = [r for r in rows if f"{r[0]}/{r[1]}" not in hidden]
        print(f"hidden companies ({len(hidden)}): {', '.join(sorted(hidden.values()))[:120]} -> dropped {before - len(rows)} postings")
    pref = (d.get("location") or "").strip()
    ep = os.path.join(WORK, "enrichment.json")
    enr = json.load(open(ep, encoding="utf-8")) if os.path.exists(ep) else {"jobs": {}, "boards": {}}
    jobs = []
    for r in rows:
        loc = parse_location(r[5], r[8], r[3])
        key = f"{r[0]}/{r[1]}#{r[2]}"
        e = (enr["jobs"].get(key) or {}).get("data")
        el, elr = loc_eligibility(pref, r[5], r[8], r[3], (e or {}).get("work_arrangement")) if pref else (None, "")
        coe = loc.get("country_est")
        if coe:
            n_est_co += 1
            if el is None and elr == "no location info": elr = f"no stated country (est. {coe['country']} {coe['p']:.0%})"
        # seniority: the title, else the enrichment, else the estimator, else "mid" by default
        sn = seniority_of(r[3]) or SENIORITY_FROM_ENRICH.get((e or {}).get("seniority") or "")
        sne = None
        if not sn:
            # "Member of Technical Staff" is a generic IC title (the model reads the word "Staff"): default to mid
            if snm is not None and not re.search(r"member of (?:the )?technical staff|\bMTS\b", r[3] or "", re.I):
                vec_s = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32); vec_s = vec_s / (np.linalg.norm(vec_s) + 1e-9)
                z = snm[0] @ vec_s + snm[1]; z = z - z.max(); pr = np.exp(z); pr /= pr.sum(); k = int(pr.argmax())
                # untitled postings are harder than the titled holdout (the embedding contains the title), so the bar is 0.85
                sne = {"v": snm[2][k], "p": round(float(pr[k]), 2)} if pr[k] >= max(snm[3], 0.85) else {"v": "mid", "p": None}
            else: sne = {"v": "mid", "p": None}
            n_est_sn += 1
        rm_known = (e or {}).get("work_arrangement") if e and e.get("work_arrangement") != "unspecified" else loc["remote"]
        rme = None
        if am is not None and rm_known == "unknown":
            vec = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32); vec = vec / (np.linalg.norm(vec) + 1e-9)
            z = am[0] @ vec + am[1]; z = z - z.max(); pr = np.exp(z); pr /= pr.sum(); pk = {c: float(pr[i]) for i, c in enumerate(am[2])}
            # the model's job is to spot remote/hybrid signals; a posting that names a place and shows neither is onsite by default
            # hybrid needs a higher bar: the labelled hybrid jobs are mostly "tech company in a city", so the model
            # leans hybrid on prior alone; remote signals are crisper (holdout precision 95%)
            if pk.get("remote", 0) >= am[3]: rme = {"v": "remote", "p": round(pk["remote"], 2)}
            elif pk.get("hybrid", 0) >= max(am[3], 0.85): rme = {"v": "hybrid", "p": round(pk["hybrid"], 2)}
            elif loc["cities"] or loc["regions"]: rme = {"v": "onsite", "p": None}  # default: names a place, nothing says remote/hybrid
            if rme: n_est_rm += 1
            if rme and el is False and elr == "not labelled remote": elr = f"not labelled remote (est. {rme['v']}" + (f" {rme['p']:.0%})" if rme['p'] else ")")
        est = None
        if sm is not None:
            vec = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32); vec = vec / (np.linalg.norm(vec) + 1e-9)
            mid = float(np.exp(vec @ sm[0] + sm[1])); k = float(np.exp(sm[2]))
            est = {"mid": round(mid, -3), "lo": round(mid / k, -3), "hi": round(mid * k, -3)}
        comp = (enr["boards"].get(f"{r[0]}/{r[1]}") or {}).get("company")
        jobs.append({"k": key, "t": r[3], "c": (comp or {}).get("name") or r[4], "l": r[5], "u": r[6], "s": r[7], "jd": r[8][:a.jd_chars], "g": r[9], "sim": round(r[10], 4), "v": r[11],
                     "rm": rm_known, "rme": rme, "coe": coe, "sn": sn, "sne": sne, "co": loc["countries"], "rg": loc["regions"], "ci": loc["cities"],
                     "el": el, "elr": elr, "sal": extract_salary(r[8]), "est": est, "e": e, "co_": comp and {"name": comp.get("name"), "website": comp.get("website"), "industry": comp.get("industry"), "size": comp.get("size_bucket"), "hq": (comp.get("hq_location") or {}).get("country_code"), "staffing": comp.get("is_staffing_agency"), "desc": comp.get("description")}})
    print(f"{sum(1 for j in jobs if j['e'])} jobs and {sum(1 for j in jobs if j['co_'])} with enriched company data")
    # fine groups over the pooled slice (what the "What?" step shows)
    V = np.stack([np.frombuffer(base64.b64decode(j["v"]), dtype=np.float32) for j in jobs])
    assign, G3 = subgroups(V, [j["t"] for j in jobs], [j["c"] for j in jobs])
    for j, g in zip(jobs, assign): j["g3"] = int(g)
    print(f"{len(G3)} groups over the slice: " + ", ".join(f"{g['size']} {g['label']}" for g in G3.values()))
    # group metadata for the leaves in this slice (labels/exemplars from the manifest)
    try:
        m, _ = manifest(); T = m["tree"]
        leaves = sorted({j["g"] for j in jobs})
        GROUPS = {int(g): {"label": T[g]["label"], "medoid": T[g]["medoid"], "size": T[g]["size"], "exemplars": T[g]["exemplars"][:4]} for g in leaves if g < len(T)}
    except Exception as e:
        print(f"(no group metadata: {e})"); GROUPS = {}
    if snm is not None: print(f"seniority estimates from the published model (n={snm[4]['n']:,}, holdout {snm[4]['holdout']['accuracy']:.0%}): {n_est_sn} jobs whose title doesn't state a level get one (model at p>={max(snm[3], 0.85)}, else mid)")
    if lt is not None: print(f"country estimates from the published location table (n={lt['n']:,}, held-out accuracy {lt['holdout_accuracy']:.1%}): {n_est_co} jobs with no stated country get an estimate")
    if am is not None: print(f"work arrangement estimates from the published model (n={am[4]['n']:,}, holdout {am[4]['holdout']['accuracy']:.0%}): {n_est_rm} jobs with unknown arrangement get an estimate at p>={am[3]}")
    if sm is not None: print(f"salary estimates from the published model (n={sm[3]['n']:,}, ±{100*(np.exp(sm[2])-1):.0f}%): {sum(1 for j in jobs if not j['sal'])} jobs without a stated salary get an estimated band")
    if pref:
        ne = sum(1 for j in jobs if j["el"] is False); nu = sum(1 for j in jobs if j["el"] is None)
        print(f"eligibility for '{pref}': {len(jobs) - ne - nu} eligible, {ne} ineligible (hidden by default), {nu} unknown")
    html = TEMPLATE.replace("__PREF__", json.dumps(pref)).replace("__GROUPS3__", json.dumps(G3)).replace("__GROUPS__", json.dumps(GROUPS)).replace("__JOBS__", json.dumps(jobs)).replace("__IDEAL__", json.dumps({"vector": d["vector"], "title": d.get("title"), "recipe": d["recipe"]})).replace("__IDEAL_TEXT__", json.dumps(open(d["source"], encoding="utf-8").read() if os.path.exists(d["source"]) else ""))
    out = a.out or os.path.join(WORK, "search.html")
    open(out, "w", encoding="utf-8").write(html)
    print(f"wrote {out}: {len(jobs):,} jobs ({os.path.getsize(out)/1e6:.1f} MB). Open it directly, or `serve` to record interactions.")

def cmd_serve(a):
    import http.server, socketserver, webbrowser
    work = os.path.abspath(WORK); log = os.path.join(work, "interactions.jsonl")
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw): super().__init__(*args, directory=work, **kw)
        def do_POST(self):
            if self.path == "/event":
                n = int(self.headers.get("content-length", 0)); body = self.rfile.read(n)
                with open(log, "ab") as f: f.write(body.rstrip(b"\n") + b"\n")
                self.send_response(204); self.end_headers()
            else: self.send_response(404); self.end_headers()
        def do_GET(self):
            if self.path in ("/", ""):
                self.send_response(302); self.send_header("Location", "/search.html"); self.end_headers(); return
            super().do_GET()
        def log_message(self, *args): pass
    with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), H) as srv:
        url = f"http://127.0.0.1:{a.port}/search.html"
        print(f"serving {work} at {url}\ninteractions -> {log}\nCtrl-C to stop")
        if not a.no_open: webbrowser.open(url)
        try: srv.serve_forever()
        except KeyboardInterrupt: pass

def cmd_rank(a):
    d, v = ideal(); rows = load_jobs()
    labels = {}; compares = []
    if os.path.exists(a.labels):
        for line in open(a.labels, encoding="utf-8"):
            try: e = json.loads(line)
            except Exception: continue
            if e.get("type") == "label": labels[e["key"]] = e["value"]
            if e.get("type") == "compare": compares.append((e["a"], e["b"], e["win"]))
    X = np.stack([np.frombuffer(base64.b64decode(r[11]), dtype=np.float32) for r in rows]); keys = [f"{r[0]}/{r[1]}#{r[2]}" for r in rows]
    pos = [i for i, k in enumerate(keys) if labels.get(k) == 1]; neg = [i for i, k in enumerate(keys) if labels.get(k) == 0]
    kidx = {k: i for i, k in enumerate(keys)}
    u = v.copy()
    pairs = [(kidx[a_], kidx[b_], 1.0 if win == "a" else 0.0) for a_, b_, win in compares if a_ in kidx and b_ in kidx]
    if pairs:  # taste model from Sort comparisons: P(a>b) = sigmoid(u·(va−vb)), L2-pulled to the ideal vector
        for _ in range(300):
            for ia, ib, y in pairs:
                diff = X[ia] - X[ib]; p = 1 / (1 + math.exp(-float(u @ diff))); u -= 0.7 * ((p - y) * diff + 0.02 * (u - v))
        print(f"taste model from {len(pairs)} comparisons")
    w = u.copy(); b = 0.0
    if pos and neg:
        idx = pos + neg; y = np.array([1] * len(pos) + [0] * len(neg), dtype=np.float32)
        for _ in range(200):
            p = 1 / (1 + np.exp(-(X[idx] @ w + b))); g = p - y
            w -= 0.5 * (X[idx].T @ g / len(idx) + 0.01 * (w - u)); b -= 0.5 * g.mean()
        score = 1 / (1 + np.exp(-(X @ w + b)))
        print(f"classifier trained on {len(pos)} yes / {len(neg)} no")
    else:
        score = X @ u; print("no labels (need >=1 yes and >=1 no): ranking by " + ("taste model" if pairs else "similarity to the ideal JD"))
    order = np.argsort(-score)
    out = os.path.join(WORK, "ranked.csv")
    with open(out, "w", encoding="utf-8") as f:
        f.write("score,label,title,company,location,url,key\n")
        for i in order:
            r = rows[i]; f.write(f"{score[i]:.4f},{labels.get(keys[i], '')},\"{r[3].replace(chr(34), chr(39))}\",\"{(r[4] or '').replace(chr(34), chr(39))}\",\"{(r[5] or '').replace(chr(34), chr(39))}\",{r[6]},{keys[i]}\n")
    json.dump({"recipe": d["recipe"], "w": w.tolist(), "b": float(b), "taste": u.tolist(), "labels": labels, "compares": len(pairs)}, open(os.path.join(WORK, "model.json"), "w", encoding="utf-8"))
    print(f"wrote {out} and {WORK}/model.json. Top 10:")
    for i in order[:10]: print(f"  {score[i]:.3f}  {rows[i][3][:60]} | {rows[i][4]} | {rows[i][5]}")

def cmd_probe(a):
    """Why isn't <url> in my list? Board freshness, snapshot membership, group rank vs the fetch cutoff, rank in the slice."""
    import urllib.parse, datetime
    q = {"url": a.url}
    if a.board: q["board"] = a.board
    # if the posting is already in the slice we know its board and canonical URL (covers embedded boards like ?gh_jid=)
    jp = os.path.join(WORK, "jobs.parquet")
    if not a.board and os.path.exists(jp):
        import duckdb, re as _re
        u = a.url.strip().rstrip("/"); gh = _re.search(r"gh_jid=(\d+)", u)
        row = duckdb.connect().execute(f"SELECT ats, slug, id, url FROM read_parquet('{jp}') WHERE rtrim(url, '/') = ? OR (? <> '' AND ats = 'greenhouse' AND id = ?) LIMIT 1", [u, gh.group(1) if gh else "", gh.group(1) if gh else ""]).fetchone()
        if row: q["board"] = f"{row[0]}/{row[1]}"; q["url"] = row[3]; print(f"(known from work/jobs.parquet: {row[0]}/{row[1]} id {row[2]})")
    d = get("/probe?" + urllib.parse.urlencode(q))
    if d.get("error"): sys.exit(f"probe failed: {d['error']}")
    r, board, job = d["resolved"], d.get("board"), d.get("job")
    now = time.time() * 1000
    ago = lambda ms: f"{(now - ms) / 3600000:.1f}h ago" if ms else "never"
    when = lambda ms: datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "?"
    print(f"url: {a.url}")
    if not r.get("ats"): sys.exit(f"  ✗ {r.get('hint', 'unrecognized URL')} (not one of the 25 crawled ATSes)")
    if not r.get("slug"): sys.exit(f"  ✗ board unresolved: {r.get('hint')}\n    (re-run with --board <ats>/<slug> once you know it; boards are listed in backend/slugs.json)")
    print(f"board: {r['ats']}/{r['slug']}" + (f"  (job id {r['id']})" if r.get("id") else ""))
    if not d.get("crawled"):
        sys.exit("  ✗ this board is not in slugs.json, so it has never been crawled. Add it (PR to backend/slugs.json) and it joins the daily fetch.")
    if not board:
        sys.exit("  ✗ board is in slugs.json but has never completed a fetch (new or unreachable). It will appear after its first successful daily fetch.")
    print(f"  crawled: yes · last successful fetch {when(board['lastOkAt'])} ({ago(board['lastOkAt'])}) · {board['jobCount']} open jobs · status {board['lastStatus']}" + (f" ({board['lastError'][:80]})" if board.get("lastError") else "") + f" · next fetch {when(board['nextFetchAt'])}")
    m, C = manifest(); built = m.get("built_at"); T = m["tree"]
    if not job or not job.get("found"):
        print(f"  ✗ job NOT seen by the crawler as of {ago(board['lastOkAt'])}.")
        print("    → newer than this board's last crawl (it will be picked up at the next fetch), or the posting is not on the board's public listing API.")
        return
    st = job["status"]
    print(f"job: {job['title']!r} · {job.get('location') or '?'} · {st}" + (f" (removed {when(job['removedAt'])})" if st == "removed" else "") + f" · first seen {when(job['firstSeenAt'])} ({ago(job['firstSeenAt'])}) · embed {job['embedStatus']} · detail {job['detailStatus']}")
    if st == "removed": print("  ✗ the board no longer lists it; removed jobs are not in the public snapshot."); return
    if job["firstSeenAt"] > (built or 0):
        print(f"  ✗ NEWER than the public snapshot (built {when(built)}). It will be in the next daily build; nothing you do locally can surface it before then.")
        nxt = "next build"
    elif job["embedStatus"] != "done":
        print(f"  ✗ not embedded yet (embed {job['embedStatus']}), so it was skipped at build time ({when(built)}). Usually resolves in the next build."); nxt = "next build"
    else:
        print(f"  ✓ in the public snapshot (built {when(built)}, job first seen {ago(job['firstSeenAt'])})"); nxt = None
    emb = job.get("embedding")
    if not emb: return
    v = np.asarray(emb, dtype=np.float32); v /= np.linalg.norm(v) + 1e-9
    ip = os.path.join(WORK, "ideal.json")
    if not os.path.exists(ip): print("  (no work/ideal.json, so no ranking context)"); return
    _, ideal_v = ideal(); sim = float(v @ ideal_v)
    # tree descent = the build's assignment rule (nearest sub-centroid), so this is the job's group
    jp = os.path.join(WORK, "jobs.parquet"); local = None
    if os.path.exists(jp):
        import duckdb
        local = duckdb.connect().execute(f"SELECT leaf, sim, title, location FROM read_parquet('{jp}') WHERE ats=? AND slug=? AND id=?", [r["ats"], r["slug"], job["id"]]).fetchone()
    # group by tree descent (the build's assignment rule, nearest sub-centroid); node ids are per build, so
    # the parquet's stale `leaf` column can't be compared against today's manifest
    n = T[0]
    while n["children"]: n = max((T[c] for c in n["children"]), key=lambda c: float(C[c["id"]] @ v))
    leaves = nearest(m, C, ideal_v, len(T)); rank = next(i for i, (lf, _) in enumerate(leaves, 1) if lf["id"] == n["id"])
    gp = os.path.join(WORK, "groups.json"); fetched = [g["id"] for g in json.load(open(gp, encoding="utf-8"))] if os.path.exists(gp) else []
    leaf_ids = {lf["id"] for lf, _ in leaves}; stale = fetched and not all(g in leaf_ids for g in fetched)
    print(f"  similarity to your ideal JD: {sim:.3f} · group {n['id']} ({n['label']}) is rank {rank} of {len(leaves)} groups for your JD")
    if local: pass  # membership is settled below by the parquet itself
    elif stale: print(f"  (work/groups.json predates today's manifest, so group ids can't be compared; you fetched {len(fetched)} groups and this one ranks {rank}: re-run `fetch --top {max(rank, len(fetched))}` to be sure)")
    elif fetched:
        if n["id"] in fetched: print(f"  ✓ that group IS in your slice (you fetched {len(fetched)} groups)")
        else: print(f"  ✗ that group is NOT in your slice: you fetched {len(fetched)} groups and it ranks {rank}. `fetch --top {max(rank, len(fetched))}` (or `--groups {n['id']}`) would include it.")
    if os.path.exists(jp):
        con = duckdb.connect(); row = local
        total = con.execute(f"SELECT count(*), sum(sim > ?) FROM read_parquet('{jp}')", [sim]).fetchone()
        if row:
            print(f"  ✓ it IS in work/jobs.parquet at rank {int(total[1] or 0) + 1} of {total[0]} by similarity.")
            pref = (json.load(open(ip, encoding="utf-8")).get("location") or "").strip()
            if pref:
                el, why = loc_eligibility(pref, row[3] or "", "", row[2] or "")
                if el is False: print(f"    but eligibility for {pref!r} hides it: {why} (toggle the ✓ eligible filter, or it's a parser miss worth a bug report)")
            hidden = set()
            ipath = os.path.join(WORK, "interactions.jsonl")
            if os.path.exists(ipath):
                for line in open(ipath, encoding="utf-8"):
                    try: ev = json.loads(line)
                    except Exception: continue
                    if ev.get("type") == "hide_company" and ev.get("board"): (hidden.add if ev.get("on", True) else hidden.discard)(ev["board"])
            if f"{r['ats']}/{r['slug']}" in hidden: print("    and this company is hidden (\"never show again\"); un-hide it in the page.")
            elif el is not False: print("    so if it isn't on the page, check the active facets, bans, and search box: it is in the data.")
        else:
            print(f"  ✗ not in work/jobs.parquet; by similarity it would sit at rank {int(total[1] or 0) + 1} of {total[0]} in your slice.")

def cmd_status(a):
    for f in ("ideal-jd.md", "ideal.json", "groups.json", "jobs.parquet", "search.html", "enrichment.json", "interactions.jsonl", "model.json", "ranked.csv"):
        p = os.path.join(WORK, f); print(f"{'✓' if os.path.exists(p) else '·'} {f}" + (f"  ({os.path.getsize(p)/1e6:.1f} MB, {time.strftime('%H:%M', time.localtime(os.path.getmtime(p)))})" if os.path.exists(p) else ""))
    p = os.path.join(WORK, "interactions.jsonl")
    if os.path.exists(p):
        ev = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        from collections import Counter
        print("interactions:", dict(Counter(e.get("type") for e in ev)), "| yes:", sum(1 for e in ev if e.get("type") == "label" and e.get("value") == 1), "no:", sum(1 for e in ev if e.get("type") == "label" and e.get("value") == 0))

TEMPLATE = open(os.path.join(os.path.dirname(__file__), "search.html"), encoding="utf-8").read()

ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
s = sub.add_parser("embed"); s.add_argument("--file", required=True); s.add_argument("--title"); s.add_argument("--location")
s = sub.add_parser("groups"); s.add_argument("--k", type=int, default=30); s.add_argument("--min-sim", type=float, default=0.0)
s = sub.add_parser("fetch"); s.add_argument("--groups"); s.add_argument("--top", type=int, default=12)
s = sub.add_parser("html"); s.add_argument("--out"); s.add_argument("--jd-chars", type=int, default=4000)
s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765); s.add_argument("--no-open", action="store_true")
s = sub.add_parser("enrich"); s.add_argument("--top", type=int, default=300); s.add_argument("--all", action="store_true")
s = sub.add_parser("rank"); s.add_argument("--labels", default=os.path.join(WORK, "interactions.jsonl"))
s = sub.add_parser("probe", help="why isn't this posting in my list?"); s.add_argument("url"); s.add_argument("--board", help="ats/slug when the URL doesn't name the board (workable, paylocity)")
sub.add_parser("status")
args = ap.parse_args()
{"embed": cmd_embed, "groups": cmd_groups, "fetch": cmd_fetch, "html": cmd_html, "serve": cmd_serve, "enrich": cmd_enrich, "rank": cmd_rank, "probe": cmd_probe, "status": cmd_status}[args.cmd](args)

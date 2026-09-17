"""Core data access — manifest, ideal JD, job slice loading, deduplication.

Part 1: foundational functions that every other service depends on.
Part 2 (same file): subgroups and build_jobs_payload — added in Step 7.
"""
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from app.config import WORK
from app.services.models import get

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def nice_company(c: str | None) -> str:
    """Return an empty string for UUID-style company slugs, otherwise the name."""
    return "" if re.fullmatch(r"[0-9a-fA-F-]{20,}", c or "") else (c or "")


def node_label(n: dict) -> str:
    """Human-readable label for a tree node: topic + up to 4 exemplar titles."""
    ex = "; ".join(
        f"{e['title']}" + (f" @ {nice_company(e['company'])}" if nice_company(e["company"]) else "")
        for e in n["exemplars"][:4]
    )
    return f"{n['label']}  [{ex}]"


# ---------------------------------------------------------------------------
# Manifest and centroids (cached 6 hours)
# ---------------------------------------------------------------------------

_SIX_HOURS = 6 * 3600


def manifest() -> tuple[dict, np.ndarray]:
    """Return (manifest_dict, centroids_float32[nodes x dims]).

    Downloads and caches work/manifest.json and work/centroids.bin;
    refreshes if either is absent or older than 6 hours.
    """
    mp = WORK / "manifest.json"
    cp = WORK / "centroids.bin"
    stale = (
        not mp.exists()
        or not cp.exists()
        or time.time() - mp.stat().st_mtime > _SIX_HOURS
    )
    if stale:
        m = get("/data/manifest.json")
        mp.write_text(json.dumps(m), encoding="utf-8")
        cp.write_bytes(get("/data/centroids.bin", binary=True))
    m = json.loads(mp.read_text(encoding="utf-8"))
    C = np.fromfile(cp, dtype=np.float16).astype(np.float32).reshape(-1, m["dims"])
    return m, C


# ---------------------------------------------------------------------------
# Ideal JD
# ---------------------------------------------------------------------------

def ideal() -> tuple[dict, np.ndarray]:
    """Return (ideal_dict, unit_vector).

    Raises FileNotFoundError if work/ideal.json is absent (run embed first).
    """
    p = WORK / "ideal.json"
    if not p.exists():
        raise FileNotFoundError(
            "work/ideal.json not found — run `embed --file work/ideal-jd.md` first"
        )
    d = json.loads(p.read_text(encoding="utf-8"))
    v = np.asarray(d["vector"], dtype=np.float32)
    v /= np.linalg.norm(v) + 1e-9
    return d, v


# ---------------------------------------------------------------------------
# Nearest-neighbour search over leaves
# ---------------------------------------------------------------------------

def nearest(m: dict, C: np.ndarray, v: np.ndarray, k: int, min_sim: float = 0.0) -> list:
    """Rank every leaf node by cosine similarity to *v*.

    All centroids are loaded locally so we score all of them — ball bounds
    are too loose in 1536-d to prune. Returns [(node, sim)] best first.
    """
    T = m["tree"]
    sims = C @ v
    leaves = [n for n in T if not n["children"]]
    leaves.sort(key=lambda n: -sims[n["id"]])
    return [(n, float(sims[n["id"]])) for n in leaves if sims[n["id"]] >= min_sim][:k]


def leaves_under(m: dict, n: dict) -> list:
    """Return all leaf descendants of tree node *n* (depth-first)."""
    T = m["tree"]
    out: list = []
    stack = [n]
    while stack:
        x = stack.pop()
        if not x["children"]:
            out.append(x)
        else:
            stack.extend(T[c] for c in x["children"])
    return out


# ---------------------------------------------------------------------------
# Job slice
# ---------------------------------------------------------------------------

def dedup_rows(rows: list) -> list:
    """Collapse identical (company, title) postings.

    Rows are expected to arrive sorted by sim DESC so the highest-similarity
    copy of each (company, title) pair is kept. Mirrors tools/jobs.py exactly.
    """
    def _n(x):
        return re.sub(r"\W+", " ", (x or "").lower()).strip()

    seen: set = set()
    uniq: list = []
    for r in rows:
        key = (_n(r[4]), _n(r[3]))  # (company, title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    dropped = len(rows) - len(uniq)
    if dropped:
        print(f"deduped {dropped} repeated (company, title) postings", file=sys.stderr)
    return uniq


def load_jobs() -> list:
    """Load work/jobs.parquet ordered by sim DESC, deduped by (company, title).

    Raises FileNotFoundError if the parquet file is absent (run fetch first).
    """
    import duckdb

    p = WORK / "jobs.parquet"
    if not p.exists():
        raise FileNotFoundError(
            "work/jobs.parquet not found — run `fetch` first"
        )
    rows = (
        duckdb.connect()
        .execute(f"SELECT * FROM read_parquet('{p}') ORDER BY sim DESC")
        .fetchall()
    )
    return dedup_rows(rows)


# ---------------------------------------------------------------------------
# Fine subgroups over a job slice (bisecting k-means)
# ---------------------------------------------------------------------------

def subgroups(vecs: np.ndarray, titles: list, comps: list, k: int = 6, min_size: int = 8):
    """Split a slice into at most *k* groups using bisecting 2-means.

    Returns (assignment[i] -> gid, {gid: {label, medoid, size, radius, exemplars}}).
    Mirrors tools/jobs.py exactly.
    """
    X = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    N = len(X)
    STOP = set(
        "and or of the for a in to with at on & "
        "senior sr jr ii iii i lead staff associate assistant "
        "manager specialist engineer".split()
    )

    def norm_t(t):
        return re.sub(r"[^a-z]+", " ", (t or "").lower()).strip()

    def words(idx, n=4):
        c: dict = {}
        for i in idx:
            for w in re.findall(r"[a-z][a-z+#]+", (titles[i] or "").lower()):
                if w not in STOP and len(w) > 2:
                    c[w] = c.get(w, 0) + 1
        return [w for w, _ in sorted(c.items(), key=lambda x: -x[1])[:n]]

    def spread(idx):
        cen = X[idx].mean(0)
        cen /= np.linalg.norm(cen) + 1e-9
        return float((1 - X[idx] @ cen).mean()), cen

    def two_means(idx):
        r = np.random.default_rng(len(idx))
        c = X[r.choice(idx, 2, replace=False)].copy()
        for _ in range(10):
            lab = ((X[idx] - c[1]) ** 2).sum(1) < ((X[idx] - c[0]) ** 2).sum(1)
            for kk, m in ((0, ~lab), (1, lab)):
                if m.any():
                    c[kk] = X[idx[m]].mean(0)
        return lab

    leaves = [np.arange(N)]
    while len(leaves) < k:
        cand = sorted(
            range(len(leaves)),
            key=lambda i: -spread(leaves[i])[0] * np.sqrt(len(leaves[i])),
        )
        split = False
        for i in cand:
            idx = leaves[i]
            if len(idx) < 2 * min_size:
                continue
            lab = two_means(idx)
            a_, b_ = idx[~lab], idx[lab]
            if len(a_) < min_size or len(b_) < min_size:
                continue
            leaves[i] = a_
            leaves.append(b_)
            split = True
            break
        if not split:
            break

    assign = np.zeros(N, dtype=np.int64)
    groups: dict = {}
    for gid, idx in enumerate(leaves):
        assign[idx] = gid
        r, cen = spread(idx)
        dist = 1 - X[idx] @ cen
        order = idx[np.argsort(dist)]
        med = order[0]
        ex = [int(med)]
        seen = {norm_t(titles[med])}
        for i in order:
            if len(ex) >= 4:
                break
            if norm_t(titles[i]) not in seen:
                seen.add(norm_t(titles[i]))
                ex.append(int(i))
        groups[gid] = {
            "label": " · ".join(words(idx)),
            "medoid": titles[med],
            "size": int(len(idx)),
            "radius": round(float(dist.max()), 3),
            "exemplars": [{"title": titles[i], "company": comps[i] or ""} for i in ex],
        }
    return assign, groups


# ---------------------------------------------------------------------------
# Core payload builder (used by the page route and html command)
# ---------------------------------------------------------------------------

def build_jobs_payload(jd_chars: int = 4000) -> dict:
    """Load every data source and return the full jobs payload as a plain dict.

    Keys: jobs, ideal, groups, G3, labels, pref, pref_remote_only.

    Raises FileNotFoundError if work/ideal.json or work/jobs.parquet are absent.
    """
    import base64
    from locparse import parse as parse_location, eligibility as loc_eligibility
    from salary import extract as extract_salary
    from seniority import extract as seniority_of, FROM_ENRICH as SENIORITY_FROM_ENRICH

    from app.services.labels import (
        capture_labelled,
        load_interaction_labels,
        interactions_path,
    )
    from app.services.models import (
        salary_model,
        arrangement_model,
        location_table,
        seniority_model,
        age_model,
        age_predict,
    )

    d, v = ideal()
    rows = load_jobs()

    # Persist labelled jobs so they survive slice rebuilds
    labels_now = load_interaction_labels()
    capture_labelled(rows, labels_now)

    sm = salary_model()
    am = arrangement_model()
    lt = location_table()
    snm = seniority_model()
    agm = age_model()

    # "Never show <company> again" filter — honour final state at build time
    hidden: dict = {}
    ip = interactions_path()
    if ip.exists():
        for line in ip.open(encoding="utf-8"):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "hide_company" and ev.get("board"):
                if ev.get("on", True):
                    hidden[ev["board"]] = ev.get("company") or ev["board"]
                else:
                    hidden.pop(ev["board"], None)
    if hidden:
        rows = [r for r in rows if f"{r[0]}/{r[1]}" not in hidden]

    pref = (d.get("location") or "").strip()

    enr_p = WORK / "enrichment.json"
    enr = json.loads(enr_p.read_text(encoding="utf-8")) if enr_p.exists() else {"jobs": {}, "boards": {}}

    jobs = []
    for r in rows:
        loc = parse_location(r[5], r[8], r[3])
        key = f"{r[0]}/{r[1]}#{r[2]}"
        e = (enr["jobs"].get(key) or {}).get("data")
        el, elr = (
            loc_eligibility(pref, r[5], r[8], r[3], (e or {}).get("work_arrangement"))
            if pref
            else (None, "")
        )
        coe = loc.get("country_est")
        if coe and el is None and elr == "no location info":
            elr = f"no stated country (est. {coe['country']} {coe['p']:.0%})"

        # Seniority: title → enrichment → estimator → "mid"
        sn = seniority_of(r[3]) or SENIORITY_FROM_ENRICH.get((e or {}).get("seniority") or "")
        sne = None
        if not sn:
            if snm is not None and not re.search(
                r"member of (?:the )?technical staff|\bMTS\b", r[3] or "", re.I
            ):
                vec_s = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32)
                vec_s = vec_s / (np.linalg.norm(vec_s) + 1e-9)
                z = snm[0] @ vec_s + snm[1]
                z = z - z.max()
                pr = np.exp(z)
                pr /= pr.sum()
                pk = int(pr.argmax())
                sne = (
                    {"v": snm[2][pk], "p": round(float(pr[pk]), 2)}
                    if pr[pk] >= max(snm[3], 0.85)
                    else {"v": "mid", "p": None}
                )
            else:
                sne = {"v": "mid", "p": None}

        rm_known = (
            (e or {}).get("work_arrangement")
            if e and e.get("work_arrangement") != "unspecified"
            else loc["remote"]
        )
        rme = None
        if am is not None and rm_known == "unknown":
            vec = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32)
            vec = vec / (np.linalg.norm(vec) + 1e-9)
            z = am[0] @ vec + am[1]
            z = z - z.max()
            pr = np.exp(z)
            pr /= pr.sum()
            pk_map = {c: float(pr[i]) for i, c in enumerate(am[2])}
            if pk_map.get("remote", 0) >= am[3]:
                rme = {"v": "remote", "p": round(pk_map["remote"], 2)}
            elif pk_map.get("hybrid", 0) >= max(am[3], 0.85):
                rme = {"v": "hybrid", "p": round(pk_map["hybrid"], 2)}
            elif loc["cities"] or loc["regions"]:
                rme = {"v": "onsite", "p": None}
            if rme and el is False and elr == "not labelled remote":
                elr = (
                    f"not labelled remote (est. {rme['v']}"
                    + (f" {rme['p']:.0%})" if rme["p"] else ")")
                )

        est = None
        agp = None
        if sm is not None or agm is not None:
            vec = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32)
            vec = vec / (np.linalg.norm(vec) + 1e-9)
            if sm is not None:
                mid = float(np.exp(vec @ sm[0] + sm[1]))
                kk = float(np.exp(sm[2]))
                est = {"mid": round(mid, -3), "lo": round(mid / kk, -3), "hi": round(mid * kk, -3)}
            if agm is not None:
                agp = round(age_predict(agm, vec), 1)

        comp = (enr["boards"].get(f"{r[0]}/{r[1]}") or {}).get("company")
        jobs.append({
            "k": key, "t": r[3],
            "c": (comp or {}).get("name") or r[4],
            "l": r[5], "u": r[6], "s": r[7],
            "p": (r[12] if len(r) > 12 else None),
            "agp": agp,
            "jd": r[8][:jd_chars],
            "g": r[9],
            "sim": round(r[10], 4),
            "v": r[11],
            "rm": rm_known, "rme": rme, "coe": coe,
            "sn": sn, "sne": sne,
            "co": loc["countries"], "rg": loc["regions"], "ci": loc["cities"],
            "el": el, "elr": elr,
            "sal": extract_salary(r[8]),
            "est": est,
            "e": e,
            "co_": comp and {
                "name": comp.get("name"),
                "website": comp.get("website"),
                "industry": comp.get("industry"),
                "size": comp.get("size_bucket"),
                "hq": (comp.get("hq_location") or {}).get("country_code"),
                "staffing": comp.get("is_staffing_agency"),
                "desc": comp.get("description"),
            },
        })

    # Fine groups over the slice (G3 — shown in the "What?" panel)
    V = np.stack([
        np.frombuffer(base64.b64decode(j["v"]), dtype=np.float32)
        for j in jobs
    ])
    assign, G3 = subgroups(V, [j["t"] for j in jobs], [j["c"] for j in jobs])
    for j, g in zip(jobs, assign):
        j["g3"] = int(g)

    # Manifest group metadata for leaves present in the slice
    try:
        m, _ = manifest()
        T = m["tree"]
        leaf_ids = sorted({j["g"] for j in jobs})
        GROUPS = {
            int(g): {
                "label": T[g]["label"],
                "medoid": T[g]["medoid"],
                "size": T[g]["size"],
                "exemplars": T[g]["exemplars"][:4],
            }
            for g in leaf_ids
            if g < len(T)
        }
    except Exception as exc:
        print(f"(no group metadata: {exc})", file=sys.stderr)
        GROUPS = {}

    # pref_remote_only: every clause is "remote" with no city
    from locparse import parse as _pp
    _clauses = [
        c.strip()
        for c in re.split(r"\bor\b|;|\||/", pref or "", flags=re.I)
        if c.strip()
    ]
    pref_remote_only = bool(_clauses) and all(
        _pp(c)["remote"] == "remote" and not _pp(c)["cities"] for c in _clauses
    )

    # Current label snapshot (0/1 only) for the browser's initial state
    all_labels = load_interaction_labels()
    init_labels = {k: v for k, v in all_labels.items() if v in (0, 1)}

    return {
        "jobs": jobs,
        "ideal": {
            "vector": d["vector"],
            "title": d.get("title"),
            "recipe": d["recipe"],
        },
        "groups": GROUPS,
        "G3": G3,
        "labels": init_labels,
        "pref": pref,
        "pref_remote_only": pref_remote_only,
    }

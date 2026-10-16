"""Rank command — fit a taste model from labels/comparisons and write ranked.csv.

Mirrors tools/jobs.py cmd_rank() exactly.  Raises instead of sys.exit().
"""
import base64
import json
import math
from pathlib import Path

import numpy as np

from app.config import WORK
from app.services.data import ideal, load_jobs
from app.services.labels import capture_labelled, interactions_path


def run(labels_path: str | None = None) -> None:
    """Fit a logistic-regression taste model and write work/ranked.csv.

    Reads labels and pairwise comparisons from *labels_path* (defaults to
    work/interactions.jsonl).  If there are pairwise comparisons a Bradley-
    Terry taste vector is estimated first and used as the logistic-regression
    prior, matching tools/jobs.py cmd_rank() exactly.

    Parameters
    ----------
    labels_path:
        Path to the interactions JSONL file.  Defaults to
        work/interactions.jsonl.

    Raises
    ------
    FileNotFoundError
        If work/ideal.json or work/jobs.parquet are absent.
    """
    lp = Path(labels_path) if labels_path else interactions_path()

    d, v = ideal()
    rows = load_jobs()

    # Read labels and pairwise comparisons from the interactions log
    labels: dict = {}
    compares: list = []
    if lp.exists():
        for line in lp.open(encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "label":
                labels[e["key"]] = e["value"]
            if e.get("type") == "compare":
                compares.append((e["a"], e["b"], e["win"]))

    # Persist labelled jobs while they're in the slice (survive rebuilds)
    capture_labelled(rows, labels)

    X = np.stack([
        np.frombuffer(base64.b64decode(r[11]), dtype=np.float32)
        for r in rows
    ])
    keys = [f"{r[0]}/{r[1]}#{r[2]}" for r in rows]

    pos = [i for i, k in enumerate(keys) if labels.get(k) == 1]
    neg = [i for i, k in enumerate(keys) if labels.get(k) == 0]
    kidx = {k: i for i, k in enumerate(keys)}

    u = v.copy()

    # Taste model from Sort comparisons: P(a > b) = sigmoid(u·(va − vb))
    pairs = [
        (kidx[a_], kidx[b_], 1.0 if win == "a" else 0.0)
        for a_, b_, win in compares
        if a_ in kidx and b_ in kidx
    ]
    if pairs:
        for _ in range(300):
            for ia, ib, y in pairs:
                diff = X[ia] - X[ib]
                p = 1 / (1 + math.exp(-float(u @ diff)))
                u -= 0.7 * ((p - y) * diff + 0.02 * (u - v))
        print(f"taste model from {len(pairs)} comparisons")

    # Logistic-regression classifier on yes/no labels, L2-pulled toward u
    w = u.copy()
    b = 0.0
    if pos and neg:
        idx = pos + neg
        y = np.array([1] * len(pos) + [0] * len(neg), dtype=np.float32)
        for _ in range(200):
            p = 1 / (1 + np.exp(-(X[idx] @ w + b)))
            g = p - y
            w -= 0.5 * (X[idx].T @ g / len(idx) + 0.01 * (w - u))
            b -= 0.5 * g.mean()
        score = 1 / (1 + np.exp(-(X @ w + b)))

        tot_pos = sum(1 for x in labels.values() if x == 1)
        tot_neg = sum(1 for x in labels.values() if x == 0)
        used = len(pos) + len(neg)
        tot = tot_pos + tot_neg
        skipped = tot - used
        print(
            f"classifier trained on {len(pos)} yes / {len(neg)} no  "
            f"(used {used} of {tot} labels; {skipped} skipped: their jobs "
            f"aren't in the current slice)"
        )
        if skipped and (len(pos) < 3 or len(neg) < 3 or used < 0.6 * tot):
            print(
                f"  ! thin taste model: {skipped}/{tot} of your labels fell "
                f"outside this slice (a rebuild/re-fetch can drop labelled jobs), "
                f"so the classifier is fit on just {used} points and may overfit "
                f"them. Re-fetch with a higher --top (or --groups) to pull your "
                f"labelled jobs back in, or fall back to the 'sim' column (raw JD "
                f"cosine), which is unaffected."
            )
    else:
        score = X @ u
        print(
            "no labels (need >=1 yes and >=1 no): ranking by "
            + ("taste model" if pairs else "similarity to the ideal JD")
        )

    order = np.argsort(-score)

    # Write ranked.csv
    out_csv = WORK / "ranked.csv"
    with out_csv.open("w", encoding="utf-8") as f:
        f.write("score,label,title,company,location,url,key\n")
        for i in order:
            r = rows[i]
            title = (r[3] or "").replace('"', "'")
            company = (r[4] or "").replace('"', "'")
            location = (r[5] or "").replace('"', "'")
            f.write(
                f'{score[i]:.4f},{labels.get(keys[i], "")},'
                f'"{title}","{company}","{location}",'
                f"{r[6]},{keys[i]}\n"
            )

    # Write model.json (for the browser's in-page taste model seed)
    model_out = WORK / "model.json"
    model_out.write_text(
        json.dumps({
            "recipe": d["recipe"],
            "w": w.tolist(),
            "b": float(b),
            "taste": u.tolist(),
            "labels": labels,
            "compares": len(pairs),
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"wrote {out_csv} and {model_out}. Top 10:")
    for i in order[:10]:
        print(f"  {score[i]:.3f}  {rows[i][3][:60]} | {rows[i][4]} | {rows[i][5]}")

"""Label persistence — reading and writing work/interactions.jsonl and work/labelled.jsonl.

These functions are the source of truth for yes/no labels. They are kept
identical in behaviour to tools/jobs.py so that both versions can share
the same work/ directory without data loss.
"""
import base64
import json
import time
from pathlib import Path

import numpy as np

from app.config import WORK

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LABEL_STORE = "labelled.jsonl"
INTERACTIONS = "interactions.jsonl"

# ---------------------------------------------------------------------------
# interactions.jsonl — the append-only event log
# ---------------------------------------------------------------------------

def interactions_path() -> Path:
    return WORK / INTERACTIONS


def load_interaction_labels(path: Path | str | None = None) -> dict:
    """Return the latest yes/no per job key from the append-only interaction log.

    Last write wins; a value of None or anything outside {0, 1} means the
    label was removed. Pass an explicit path to read from a non-default file.
    """
    p = Path(path) if path else interactions_path()
    labels: dict = {}
    if p.exists():
        for line in p.open(encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "label":
                labels[e["key"]] = e["value"]
    return labels


def append_interaction(event: dict) -> None:
    """Append one event dict as a JSONL line to work/interactions.jsonl."""
    line = json.dumps(event, ensure_ascii=False)
    with interactions_path().open("ab") as f:
        f.write(line.encode("utf-8") + b"\n")


# ---------------------------------------------------------------------------
# labelled.jsonl — full records of labelled jobs (survive slice rebuilds)
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    return WORK / LABEL_STORE


def load_label_store() -> dict:
    """Return persisted full records of labelled jobs, keyed by job key (ats/slug#id)."""
    store: dict = {}
    p = _store_path()
    if p.exists():
        for line in p.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("key"):
                store[r["key"]] = r
    return store


def save_label_store(store: dict) -> None:
    """Rewrite labelled.jsonl from the current store (one record per line)."""
    with _store_path().open("w", encoding="utf-8") as f:
        for r in store.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def capture_labelled(rows: list, labels: dict, store: dict | None = None) -> dict:
    """Upsert the full record of every currently-labelled job present in *rows*.

    Called by commands that hold the current slice so a job's full data
    (including its vector) is persisted before a later rebuild can drop it.
    Drops entries whose label was removed. Returns the updated store.
    """
    if store is None:
        store = load_label_store()
    by_key = {f"{r[0]}/{r[1]}#{r[2]}": r for r in rows}
    changed = False
    for key, val in labels.items():
        if val not in (0, 1):
            if store.pop(key, None) is not None:
                changed = True
            continue
        r = by_key.get(key)
        if r is not None:
            store[key] = {
                "key": key, "value": val,
                "ats": r[0], "slug": r[1], "id": r[2],
                "title": r[3], "company": r[4], "location": r[5],
                "url": r[6], "seen_ms": r[7], "jd": r[8],
                "leaf": r[9], "sim": r[10], "vec_b64": r[11],
                "published_ms": (r[12] if len(r) > 12 else None),
                "ts": int(time.time() * 1000),
            }
            changed = True
        elif key in store and store[key].get("value") != val:
            store[key]["value"] = val  # refresh the label on the persisted record
            changed = True
    if changed:
        save_label_store(store)
    return store


def store_row(rec: dict, v: np.ndarray) -> tuple:
    """Turn a persisted label record back into a jobs.parquet row tuple.

    Similarity is recomputed against the current ideal vector *v* so the
    re-injected row ranks correctly after a rebuild.
    """
    vec = np.frombuffer(base64.b64decode(rec["vec_b64"]), dtype=np.float32)
    return (
        rec.get("ats"), rec.get("slug"), rec.get("id"),
        rec.get("title"), rec.get("company"), rec.get("location"),
        rec.get("url"), rec.get("seen_ms") or 0, rec.get("jd") or "",
        rec.get("leaf") if rec.get("leaf") is not None else -1,
        float(vec @ v), rec["vec_b64"],
        rec.get("published_ms"),
    )

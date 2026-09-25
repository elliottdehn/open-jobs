"""Published ML model loaders — salary, age, work arrangement, seniority, location.

Each model is fetched from the backend once and cached in work/ for 24 hours.
All return None on any failure so callers can degrade gracefully.

Also exposes get() and post() — the two HTTP helpers used across services.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from app.config import BASE, DATA, UA, WORK

# ---------------------------------------------------------------------------
# HTTP helpers (shared with data.py, fetch.py, embed.py)
# ---------------------------------------------------------------------------

def get(path: str, binary: bool = False):
    """GET from the backend worker or data CDN."""
    base = DATA if path.startswith("/data/") else BASE
    req = urllib.request.Request(f"{base}{path}", headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read() if binary else json.loads(r.read())


def post(path: str, body: dict) -> tuple[int, dict]:
    """POST JSON to the backend worker. Returns (status_code, response_dict)."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={**UA, "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ONE_DAY = 24 * 3600


def _stale(p: Path) -> bool:
    """True if the file is absent or older than 24 hours."""
    return not p.exists() or time.time() - p.stat().st_mtime > _ONE_DAY


def _fetch_json(remote_path: str, local: Path, ensure_ascii: bool = True) -> dict:
    """Download JSON from the backend and cache it locally."""
    data = get(remote_path)
    local.write_text(json.dumps(data, ensure_ascii=ensure_ascii), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Model loaders
# ---------------------------------------------------------------------------

def salary_model():
    """Ridge regression on job embedding -> log annual USD.
    Returns (w, b, sigma, raw_dict) or None."""
    p = WORK / "salary-model.json"
    try:
        if _stale(p):
            _fetch_json("/data/salary-model.json", p)
        m = json.loads(p.read_text(encoding="utf-8"))
        return np.asarray(m["w"], dtype=np.float32), float(m["b"]), float(m["sigma"]), m
    except Exception:
        return None


def age_model():
    """Posting-age estimator: MLP or linear head on embedding -> log1p(days).
    Returns the raw dict (with pre-parsed numpy arrays) or None."""
    p = WORK / "age-model.json"
    try:
        if _stale(p):
            _fetch_json("/data/age-model.json", p)
        m = json.loads(p.read_text(encoding="utf-8"))
        if m["kind"] == "mlp":
            m["_W1"] = np.asarray(m["W1"], dtype=np.float32)
            m["_b1"] = np.asarray(m["b1"], dtype=np.float32)
            m["_W2"] = np.asarray(m["W2"], dtype=np.float32)
        else:
            m["_w"] = np.asarray(m["w"], dtype=np.float32)
        return m
    except Exception:
        return None


def age_predict(m: dict, vec: np.ndarray) -> float:
    """Expected age in days for a unit job vector under the published age model."""
    if m["kind"] == "mlp":
        h = np.maximum(vec @ m["_W1"] + m["_b1"], 0)
        z = float(h @ m["_W2"] + m["b2"])
    else:
        z = float(vec @ m["_w"] + m["b"])
    return max(0.0, float(np.expm1(z)))


def arrangement_model():
    """Softmax on embedding -> remote / hybrid / onsite.
    Returns (W, b, classes, threshold, raw_dict) or None."""
    p = WORK / "arrangement-model.json"
    try:
        if _stale(p):
            _fetch_json("/data/arrangement-model.json", p)
        m = json.loads(p.read_text(encoding="utf-8"))
        return (
            np.asarray(m["W"], dtype=np.float32),
            np.asarray(m["b"], dtype=np.float32),
            m["classes"],
            float(m.get("threshold", 0.7)),
            m,
        )
    except Exception:
        return None


def seniority_model():
    """Softmax on embedding -> title seniority levels.
    Returns (W, b, classes, threshold, raw_dict) or None."""
    p = WORK / "seniority-model.json"
    try:
        if _stale(p):
            _fetch_json("/data/seniority-model.json", p)
        m = json.loads(p.read_text(encoding="utf-8"))
        return (
            np.asarray(m["W"], dtype=np.float32),
            np.asarray(m["b"], dtype=np.float32),
            m["classes"],
            float(m.get("threshold", 0.7)),
            m,
        )
    except Exception:
        return None


def location_table():
    """Country estimates for location strings that rule-based parsing can't place.
    Installs the table into locparse.LOC_TABLE and returns the raw dict, or None."""
    import locparse  # available via LIB on sys.path (set in config.py)
    p = WORK / "location-countries.json"
    try:
        if _stale(p):
            _fetch_json("/data/location-countries.json", p, ensure_ascii=False)
        t = json.loads(p.read_text(encoding="utf-8"))
        locparse.LOC_TABLE = t["table"]
        return t
    except Exception:
        return None

"""HTML command — render the search page and write work/search.html.

Calls build_jobs_payload() for all data, then injects the payload into
app/templates/search.html via __PLACEHOLDER__ string substitution (the
same technique as tools/jobs.py) and writes work/search.html.

The template is the verbatim copy of tools/search.html, so the browser-
side logic is unchanged.  Using plain string substitution instead of
Jinja2 avoids conflicts with the abundant {{ }}, [[ ]], and other
bracket patterns in the JavaScript source.

render_payload(payload) is also the shared helper used by the page route
(Step 25): call it directly when the payload is already built from cache.
"""
import json
import os
from pathlib import Path

from app.config import WORK

# ---------------------------------------------------------------------------
# Template path
# ---------------------------------------------------------------------------

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "search.html"


# ---------------------------------------------------------------------------
# Render helper (shared by html command and page route)
# ---------------------------------------------------------------------------

def _J(x) -> str:
    """JSON-encode *x* and escape </script> sequences, matching tools/jobs.py."""
    return json.dumps(x).replace("</", "<\\/")


def render_payload(payload: dict) -> str:
    """Render search.html from an already-built payload dict.

    Parameters
    ----------
    payload:
        Dict returned by build_jobs_payload() plus an optional
        ``ideal_text`` key.  If ``ideal_text`` is absent the source
        file path is resolved from work/ideal.json.

    Returns
    -------
    Rendered HTML string.
    """
    if "ideal_text" not in payload:
        try:
            d_raw = json.loads(
                (WORK / "ideal.json").read_text(encoding="utf-8")
            )
            src = d_raw.get("source")
            ideal_text = Path(src).read_text(encoding="utf-8") if src and os.path.exists(src) else ""
        except Exception:
            ideal_text = ""
    else:
        ideal_text = payload["ideal_text"]

    tmpl = _TEMPLATE.read_text(encoding="utf-8")
    return (
        tmpl
        .replace("__PREF_REMOTE_ONLY__", "true" if payload["pref_remote_only"] else "false")
        .replace("__PREF__",             _J(payload["pref"]))
        .replace("__GROUPS3__",          _J(payload["G3"]))
        .replace("__GROUPS__",           _J(payload["groups"]))
        .replace("__JOBS__",             _J(payload["jobs"]))
        .replace("__IDEAL__",            _J(payload["ideal"]))
        .replace("__INIT_LABELS__",      _J(payload["labels"]))
        .replace("__IDEAL_TEXT__",       _J(ideal_text))
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run(out: str | None = None, jd_chars: int = 4000) -> None:
    """Build the payload and write the rendered search page to *out*.

    Parameters
    ----------
    out:
        Destination path; defaults to work/search.html.
    jd_chars:
        Maximum JD characters per job passed to build_jobs_payload().

    Raises
    ------
    FileNotFoundError
        If work/ideal.json or work/jobs.parquet are absent.
    """
    from app.services.data import build_jobs_payload

    payload = build_jobs_payload(jd_chars=jd_chars)

    try:
        d_raw = json.loads((WORK / "ideal.json").read_text(encoding="utf-8"))
        src = d_raw.get("source")
        payload["ideal_text"] = (
            Path(src).read_text(encoding="utf-8")
            if src and os.path.exists(src)
            else ""
        )
    except Exception:
        payload["ideal_text"] = ""

    html = render_payload(payload)

    dest = Path(out) if out else WORK / "search.html"
    dest.write_text(html, encoding="utf-8")
    n = len(payload["jobs"])
    mb = dest.stat().st_size / 1e6
    print(
        f"wrote {dest}: {n:,} jobs ({mb:.1f} MB). "
        f"Open it directly, or `serve` to record interactions."
    )

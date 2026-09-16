"""HTML command — render the search page and write work/search.html.

Calls build_jobs_payload() for all data, then renders
app/templates/search.html via Jinja2 and writes the result to
work/search.html (or a caller-specified path).

The Jinja2 template receives the same variables that tools/jobs.py
injected via __PLACEHOLDER__ string substitution, so the browser-side
logic is unchanged.  The |tojson filter handles </script> escaping.

This module is also the render helper used by the page route (Step 25):
call render_payload(payload) directly when the payload is already built.
"""
import json
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import WORK

# ---------------------------------------------------------------------------
# Jinja2 environment (shared with the page route)
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),  # JS/HTML escaping done via |tojson
    keep_trailing_newline=True,
)


def _ideal_text(source: str | None) -> str:
    """Return the raw text of the ideal-JD source file, or '' if absent."""
    if source and os.path.exists(source):
        return Path(source).read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Render helper (shared by html command and page route)
# ---------------------------------------------------------------------------

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
    # ideal_text may be pre-supplied (avoids a second disk read on serve)
    if "ideal_text" not in payload:
        try:
            import json as _json
            d_raw = _json.loads((WORK / "ideal.json").read_text(encoding="utf-8"))
            ideal_text = _ideal_text(d_raw.get("source"))
        except Exception:
            ideal_text = ""
    else:
        ideal_text = payload["ideal_text"]

    tmpl = _env.get_template("search.html")
    return tmpl.render(
        jobs=payload["jobs"],
        ideal=payload["ideal"],
        groups=payload["groups"],
        G3=payload["G3"],
        init_labels=payload["labels"],
        pref=payload["pref"],
        pref_remote_only=payload["pref_remote_only"],
        ideal_text=ideal_text,
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

    # Resolve ideal_text here so render_payload doesn't need a second read
    try:
        d_raw = json.loads((WORK / "ideal.json").read_text(encoding="utf-8"))
        payload["ideal_text"] = _ideal_text(d_raw.get("source"))
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

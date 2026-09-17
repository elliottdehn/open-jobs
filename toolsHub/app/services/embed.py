"""Embed command — POST the ideal JD text to the backend and write work/ideal.json.

Mirrors tools/jobs.py cmd_embed() but raises on failure instead of
calling sys.exit(), making it safe to call from both the CLI dispatcher
and (future) API routes.
"""
import json
import time

from app.config import WORK
from app.services.models import post


def run(
    file: str,
    title: str = "",
    location: str = "",
) -> dict:
    """Embed the text at *file* and write work/ideal.json.

    Parameters
    ----------
    file:
        Path to the ideal-JD source file (plain text or Markdown).
    title:
        Optional job title passed to the backend recipe selector.
    location:
        Optional location string stored in ideal.json for eligibility
        filtering; not sent to the backend.

    Returns
    -------
    The dict written to work/ideal.json.

    Raises
    ------
    FileNotFoundError
        If *file* does not exist.
    RuntimeError
        If the backend returns a non-2xx response.
    """
    text = open(file, encoding="utf-8").read()

    status, resp = post(
        "/embed",
        {"text": text, "title": title or "", "location": location or ""},
    )
    if status >= 300:
        raise RuntimeError(
            f"embed failed: HTTP {status} — "
            + str(resp)[:200]
        )

    out = {
        "vector": resp["vector"],
        "recipe": resp["recipe"],
        "source": file,
        "embedded_at": int(time.time() * 1000),
        "title": title or None,
        "location": location or None,
    }
    (WORK / "ideal.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8"
    )
    print(f"embedded {file} -> {WORK}/ideal.json ({resp['recipe']})")
    return out

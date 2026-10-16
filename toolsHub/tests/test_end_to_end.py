"""Step 29 — End-to-end integration test.

Verifies that the full toolsHub render pipeline produces an HTML page that:
  1. Contains the __JOBS__ payload (no stray placeholder tokens remain).
  2. Contains the labels from work/interactions.jsonl.
  3. Serves a 200 response at GET / via the FastAPI test client.

Requires work/jobs.parquet and work/ideal.json to be present.
Run from the repo root:
  uv run python toolsHub/tests/test_end_to_end.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

WORK = ROOT / "work"
MISSING = [p for p in (WORK / "jobs.parquet", WORK / "ideal.json") if not p.exists()]


def test_html_render() -> None:
    """build_jobs_payload + render_payload produce valid HTML with no leftovers."""
    from toolsHub.app.services.data import build_jobs_payload
    from toolsHub.app.services.html import render_payload

    payload = build_jobs_payload(jd_chars=500)
    html = render_payload(payload)

    # No un-substituted placeholders
    for token in ("__JOBS__", "__IDEAL__", "__GROUPS__", "__GROUPS3__",
                  "__PREF__", "__INIT_LABELS__", "__IDEAL_TEXT__", "__PREF_REMOTE_ONLY__"):
        assert token not in html, f"placeholder {token!r} was not substituted"

    # HTML looks plausible
    assert "<html" in html.lower(), "output does not look like HTML"
    assert "open-jobs" in html, "brand text missing"
    print(f"  render: OK ({len(html):,} bytes, {len(payload['jobs'])} jobs)")


def test_labels_in_payload() -> None:
    """Labels from interactions.jsonl appear in the payload's 'labels' dict."""
    from toolsHub.app.services.data import build_jobs_payload
    from toolsHub.app.services.labels import load_interaction_labels

    expected = load_interaction_labels()
    if not expected:
        print("  labels: SKIP (no labels in interactions.jsonl)")
        return

    payload = build_jobs_payload(jd_chars=100)
    actual = payload.get("labels", {})
    missing = {k: v for k, v in expected.items() if actual.get(k) != v}
    assert not missing, f"labels missing from payload: {missing}"
    print(f"  labels: OK ({len(expected)} label(s) all present in payload)")


def test_fastapi_get() -> None:
    """GET / returns 200 and HTML content."""
    from fastapi.testclient import TestClient
    from toolsHub.app.server import app

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/")
    assert r.status_code == 200, f"GET / returned {r.status_code}"
    assert "text/html" in r.headers.get("content-type", ""), "response is not HTML"
    assert "<html" in r.text.lower(), "response body does not look like HTML"
    print(f"  GET /: OK (HTTP 200, {len(r.content):,} bytes)")


def main() -> None:
    if MISSING:
        names = ", ".join(str(p.relative_to(ROOT)) for p in MISSING)
        print(f"SKIP — required files missing: {names}")
        return

    ok = True
    for fn in (test_html_render, test_labels_in_payload, test_fastapi_get):
        try:
            print(f"[{fn.__name__}]")
            fn()
        except Exception as exc:
            print(f"  FAIL: {exc}")
            ok = False

    if ok:
        print("PASS — all end-to-end checks passed")
    else:
        print("FAIL — see above")
        sys.exit(1)


if __name__ == "__main__":
    main()

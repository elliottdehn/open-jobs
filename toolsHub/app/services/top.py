"""Top command — write work/top.html and work/top.md for agent consumption.

Mirrors tools/jobs.py cmd_top() / freshness_of() exactly.
Raises instead of sys.exit().
"""
import base64
import html as H
import json
import re
import time
from pathlib import Path

import numpy as np

from app.config import WORK
from app.services.data import ideal, load_jobs, nice_company
from app.services.models import age_model, age_predict, location_table


# ---------------------------------------------------------------------------
# Freshness helper (shared with status.py)
# ---------------------------------------------------------------------------

def freshness_of(agm, r) -> tuple[str, float | None]:
    """Grade a job row's age.

    Returns (verdict, typical_days) where verdict is one of:
    "fresh", "stale", "re-stamped", "ghost", "unknown".
    Mirrors tools/jobs.py freshness_of() exactly.
    """
    agp = None
    if agm is not None:
        vec = np.frombuffer(base64.b64decode(r[11]), dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) + 1e-9)
        agp = age_predict(agm, vec)
    now = time.time() * 1000
    p = r[12] if len(r) > 12 else None
    s = r[7]
    if not p and not s:
        return "unknown", agp
    age = max((now - p) if p else 0, (now - s) if s else 0) / 864e5
    if p and s and p - s > 7 * 864e5:
        return "re-stamped", agp
    if age > 365:
        return "ghost", agp
    if agp is None:
        return "unknown", agp
    return ("fresh" if age <= agp else "stale"), agp


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_CHIP = {
    "fresh":      ("🌱 Fresh",       "#14301F", "#4FD98A"),
    "stale":      ("🥀 Stale",       "#332512", "#E0A659"),
    "re-stamped": ("🔁 Re-stamped",  "#361A18", "#E58680"),
    "ghost":      ("👻 Ghost",       "#251F3A", "#A995E3"),
    "unknown":    ("age unknown",    "#202C26", "#8FA396"),
}

_CSS = (
    'body{margin:0;background:#121915;color:#E7EFE9;font:15.5px/1.5 -apple-system,"Segoe UI",Helvetica,sans-serif}'
    ".w{max-width:900px;margin:0 auto;padding:32px 20px 80px}"
    "h1{font-family:Georgia,serif;font-weight:400;font-size:30px;margin:0 0 6px}"
    "h1 em{font-style:italic;color:#4FD98A}.sub{color:#8FA396;margin:0 0 28px}"
    ".j{background:#1A2420;border:1px solid #26312A;border-radius:12px;padding:16px 18px;margin-bottom:10px}"
    ".j .n{color:#5F7166;font-family:ui-monospace,Menlo,monospace;font-size:12px}"
    ".j h2{font-size:17px;margin:2px 0 4px}.j h2 a{color:#E7EFE9;text-decoration:none}"
    ".j h2 a:hover{text-decoration:underline;text-decoration-color:#4FD98A}"
    ".m{color:#8FA396;font-size:13.5px}.c{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}"
    ".v{font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}"
    ".p{color:#8FA396;background:#202C26;font-weight:500}"
    ".why{color:#E7EFE9;margin:6px 0 0;font-size:14px}.why b{color:#4FD98A;font-weight:600}"
    ".x{color:#8FA396;font-size:13.5px;margin:6px 0 0}"
    ".open{display:inline-block;margin-top:10px;border:1px solid #4FD98A;color:#4FD98A;"
    "border-radius:8px;padding:5px 12px;text-decoration:none;font-size:13.5px}"
    ".foot{color:#5F7166;font-size:12.5px;margin-top:28px}"
)


def run(
    n: int = 50,
    notes_path: str | None = None,
    freshness: str = "fresh",
    out: str | None = None,
) -> None:
    """Write work/top.html and work/top.md.

    Parameters
    ----------
    n:
        Maximum number of jobs to include.
    notes_path:
        Path to a JSON file mapping job key -> one-line note string.
    freshness:
        Comma-separated freshness verdicts to include (default "fresh").
    out:
        Destination HTML path; defaults to work/top.html.

    Raises
    ------
    FileNotFoundError
        If work/ideal.json or work/jobs.parquet are absent.
    """
    from locparse import eligibility as loc_eligibility

    d, v = ideal()
    rows = load_jobs()
    agm = age_model()
    location_table()  # install LOC_TABLE for loc_eligibility
    pref = (d.get("location") or "").strip()

    notes: dict = {}
    if notes_path and Path(notes_path).exists():
        notes = json.loads(Path(notes_path).read_text(encoding="utf-8"))

    want = set(freshness.split(","))

    picked: list = []
    skipped = {"ineligible": 0, "freshness": 0}
    for r in rows:  # already sorted by sim desc and deduped
        el, elr = loc_eligibility(pref, r[5], r[8], r[3]) if pref else (None, "")
        if el is False:
            skipped["ineligible"] += 1
            continue
        fr, agp = freshness_of(agm, r)
        if fr not in want:
            skipped["freshness"] += 1
            continue
        picked.append((r, el, elr, fr, agp))
        if len(picked) >= n:
            break

    def days(ms):
        return f"{max(0, int((time.time() * 1000 - ms) / 864e5))} d ago" if ms else "n/a"

    title = d.get("title") or "your ideal job"
    where = pref or "anywhere"
    when = time.strftime("%Y-%m-%d")

    # --- HTML ---
    parts = [
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Top {len(picked)} matches: {H.escape(title)}</title>"
        f"<style>{_CSS}</style></head><body><div class=\"w\">"
        f'<h1>Top {len(picked)} matches for <em>{H.escape(title)}</em></h1>'
        f'<p class="sub">{H.escape(where)} · fresh postings only, ranked by similarity'
        f' to your ideal job description · built {when} from'
        f' <a href="https://github.com/elliottdehn/open-jobs" style="color:#8FA396">'
        f"open-jobs</a></p>"
    ]

    for i, (r, el, elr, fr, agp) in enumerate(picked, 1):
        key = f"{r[0]}/{r[1]}#{r[2]}"
        lab, bg, fg = _CHIP[fr]
        tip = f"postings like this are typically ~{agp:.0f} days old" if agp else ""
        why = notes.get(key)
        jd = re.sub(r"\s+", " ", r[8] or "").strip()
        pub_chip = (
            f'<span class="v p">posted {days(r[12])}</span>'
            if len(r) > 12 and r[12]
            else ""
        )
        elr_chip = f'<span class="v p">{H.escape(elr)}</span>' if elr else ""
        why_p = (
            f'<p class="why"><b>Why it fits:</b> {H.escape(why)}</p>' if why else ""
        )
        co = nice_company(r[4])
        meta = (co + (" · " if co and r[5] else "") + (r[5] or ""))
        parts.append(
            f'<div class="j">'
            f'<div class="n">#{i} · match {r[10] * 100:.0f}%</div>'
            f'<h2><a href="{H.escape(r[6])}" target="_blank" rel="noopener">'
            f"{H.escape(r[3])}</a></h2>"
            f'<div class="m">{H.escape(meta)}</div>'
            f'<div class="c">'
            f'<span class="v" style="background:{bg};color:{fg}" title="{H.escape(tip)}">'
            f"{lab}</span>"
            f'{pub_chip}<span class="v p">first seen {days(r[7])}</span>{elr_chip}'
            f"</div>"
            f"{why_p}"
            f'<p class="x">{H.escape(jd[:420])}{"…" if len(jd) > 420 else ""}</p>'
            f'<a class="open" href="{H.escape(r[6])}" target="_blank" rel="noopener">'
            f"Open the posting ↗</a></div>"
        )

    parts.append(
        f'<p class="foot">Eligible = fits "{H.escape(where)}" by location or remote'
        f" policy (unclear ones kept). Fresh = younger than postings with this content"
        f" typically are, on the crawler's own clock."
        f" {skipped['ineligible']} ineligible and {skipped['freshness']} not-fresh"
        f" postings were left out.</p></div></body></html>"
    )

    hp = Path(out) if out else WORK / "top.html"
    hp.write_text("\n".join(parts), encoding="utf-8")

    # --- Markdown (full JD text for agent consumption) ---
    mp = hp.with_suffix(".md")
    with mp.open("w", encoding="utf-8") as f:
        f.write(
            f"# Top {len(picked)} matches for {title} ({where}), {when}\n\n"
            "Read every posting below, then write work/top-notes.json as "
            '{\"<key>\": \"one line on why it fits\"} and re-run '
            "`top --notes work/top-notes.json`.\n\n"
        )
        for i, (r, el, elr, fr, agp) in enumerate(picked, 1):
            pub = days(r[12]) if len(r) > 12 and r[12] else "n/a"
            age_note = f" (typical ~{agp:.0f} d)" if agp else ""
            elr_note = f" · {elr}" if elr else ""
            f.write(
                f"## {i}. {r[3]}\n"
                f"- key: `{r[0]}/{r[1]}#{r[2]}`\n"
                f"- company: {nice_company(r[4])}\n"
                f"- location: {r[5]}\n"
                f"- url: {r[6]}\n"
                f"- match: {r[10] * 100:.0f}% · {fr}{age_note}"
                f" · posted {pub} · first seen {days(r[7])}{elr_note}\n\n"
                + (r[8] or "").strip()
                + "\n\n"
            )

    no_notes_hint = (
        "" if notes
        else " No notes yet: read the .md, write work/top-notes.json, re-run with --notes."
    )
    print(
        f"wrote {hp} ({len(picked)} jobs) and {mp} (full text, for reading). "
        f"Left out: {skipped['ineligible']} ineligible, "
        f"{skipped['freshness']} not in {sorted(want)}.{no_notes_hint}"
    )
    for i, (r, el, elr, fr, agp) in enumerate(picked[:10], 1):
        co = nice_company(r[4])
        print(
            f"  {i:>2}. {r[10] * 100:3.0f}%  {r[3][:56]} | {co[:28]} | {(r[5] or '')[:28]}"
        )

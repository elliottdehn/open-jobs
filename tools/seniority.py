"""Mechanical seniority from a job title. extract(title) -> one of LEVELS or None (title doesn't say).
Deterministic, no network. "mid" is never stated in titles, so it is the client's default when neither the
title nor the published estimator says otherwise (the same trick as onsite for work arrangement)."""
import re

LEVELS = ["intern", "junior", "senior", "staff", "lead", "manager", "director", "executive"]
# highest-ranking match wins ("Senior Manager" is a manager, "Director of Engineering" is a director)
_PAT = [
    ("executive", re.compile(r"\b(?:vp|svp|evp|avp|vice president|chief|c[etfoi]o|president|founder|partner|general manager|gm)\b", re.I)),
    ("director",  re.compile(r"\bdirector\b", re.I)),
    ("manager",   re.compile(r"\b(?:manager|head of|head,|mgr)\b", re.I)),
    ("lead",      re.compile(r"\b(?:lead|team lead|tech lead|supervisor|foreman|forewoman)\b", re.I)),
    ("staff",     re.compile(r"\b(?:staff|principal|distinguished|fellow|architect)\b", re.I)),  # tech ladder only: see _TECH below
    ("senior",    re.compile(r"\b(?:senior|sr\.?|iii|iv|v)\b", re.I)),
    ("junior",    re.compile(r"\b(?:junior|jr\.?|entry[- ]level|graduate|new grad|trainee|apprentice|associate|assistant|\bi\b)\b", re.I)),
    ("intern",    re.compile(r"\b(?:intern|internship|co-?op|summer analyst|working student)\b", re.I)),
]
# "Staff Pharmacist" and "Principal" (school) are not the tech staff/principal rung
_TECH = re.compile(r"\b(?:engineer(?:ing)?|developer|scientist|researcher|designer|architect|analyst|sre|devops|programmer|technologist|data|software|security|product|platform|infrastructure|ml|ai)\b", re.I)
_NOT_MANAGER = re.compile(r"\b(?:account|product|project|program|community|social media|content|marketing|brand|sales|customer success|relationship|case|property|office|store|restaurant|shift|assistant|facilities|regional|district|territory|portfolio|wealth|risk|supply chain|construction|warehouse|operations|it)\s+manager\b", re.I)

def extract(title):
    """Seniority stated in the title, or None. Individual-contributor 'X Manager' titles (Product Manager,
    Account Manager…) are roles, not levels: they fall through to whatever else the title says."""
    t = (title or "").strip()
    if not t: return None
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)  # "(Remote)", "[Hybrid]"
    for level, pat in _PAT:
        if level == "manager" and _NOT_MANAGER.search(t):
            continue
        if level == "staff" and not _TECH.search(t): continue
        if level == "lead" and re.search(r"\bassistant manager\b", t, re.I): return "lead"  # first-rung management
        if level == "junior" and re.search(r"\bassociate (?:director|vp|vice president|principal|partner|professor|dean)\b", t, re.I): continue
        if level == "senior" and re.search(r"\bsenior (?:manager|director|vp|vice president|lead|staff|principal)\b", t, re.I): continue  # handled above
        if pat.search(t): return level
    return None

# enrichment (jobschema SENIORITY) -> these coarser levels; "mid" stays "mid"
FROM_ENRICH = {"intern": "intern", "entry": "junior", "junior": "junior", "mid": "mid", "senior": "senior", "staff": "staff", "principal": "staff",
               "lead": "lead", "manager": "manager", "senior_manager": "manager", "director": "director", "vp": "executive", "c_level": "executive"}

if __name__ == "__main__":
    import sys
    for t in sys.argv[1:] or ["Senior Software Engineer", "Staff Engineer, Platform", "Engineering Manager", "Product Manager", "Software Engineer II",
                              "Software Engineer III", "Director of Engineering", "VP Engineering", "Software Engineering Intern", "Jr. Developer", "Software Engineer"]:
        print(f"{t:40s} -> {extract(t)}")

# /// script
# requires-python = ">=3.10"
# ///
"""`uv run tools/test_seniority.py` — the title→seniority spec. Add a case whenever a title parses wrong."""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from seniority import extract
CASES = [
    ("Senior Software Engineer", "senior"), ("Sr. Backend Engineer", "senior"), ("Software Engineer III", "senior"), ("Software Engineer II", None),
    ("Software Engineer", None), ("Registered Nurse", None), ("Product Manager", None), ("Account Manager", None), ("Senior Product Manager", "senior"),
    ("Engineering Manager", "manager"), ("Head of Platform", "manager"), ("Senior Engineering Manager", "manager"), ("Assistant Manager (08941) Surf City, NC", "lead"),
    ("Staff Engineer, Platform", "staff"), ("Principal Software Engineer", "staff"), ("Solutions Architect", "staff"), ("Staff Pharmacist", None), ("Principal", None),
    ("Distinguished Engineer", "staff"), ("Tech Lead, Payments", "lead"), ("Lead Data Scientist", "lead"), ("Director of Engineering", "director"),
    ("Associate Director, Clinical Ops", "director"), ("VP Engineering", "executive"), ("Chief Technology Officer", "executive"), ("CTO", "executive"),
    ("Software Engineering Intern", "intern"), ("Summer Analyst", "intern"), ("Student Success Coordinator", None), ("Working Student - Data", "intern"),
    ("Jr. Developer", "junior"), ("Entry Level Software Engineer", "junior"), ("New Grad Software Engineer", "junior"), ("Personal Banker I", "junior"),
    ("Associate, Credit Risk", "junior"), ("Senior Software Engineer (Remote)", "senior"), ("Engineer IV", "senior"),
]
fails = 0
for t, want in CASES:
    got = extract(t)
    if got != want: fails += 1; print(f"FAIL {t!r}: {got} (want {want})")
print(f"{len(CASES)-fails}/{len(CASES)} passed" if not fails else f"{fails} FAILED of {len(CASES)}"); sys.exit(1 if fails else 0)

# /// script
# requires-python = ">=3.10"
# ///
"""Ambiguity tests for locparse: `uv run tools/test_locparse.py`. Plain asserts, no framework.
Add a case whenever a location parses wrong; the table is the spec."""
import sys, os
# Windows consoles default to cp1252; our output has ✓ · – etc. Reconfigure stdout/stderr to UTF-8 (no-op elsewhere).
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
sys.path.insert(0, os.path.dirname(__file__))
from locparse import parse, eligibility, find_places, COUNTRIES, ISO3_ALL

CASES = [  # (location, jd, expected countries, expected regions)
    # Georgia: US state unless the segment names the country or a Georgian city
    ("Atlanta, Georgia", "", {"US"}, {"GA"}),
    ("Georgia", "", {"US"}, {"GA"}),
    ("Tbilisi, Georgia", "", {"GE"}, set()),
    ("Georgia (country)", "", {"GE"}, set()),
    ("Remote - Republic of Georgia", "", {"GE"}, set()),
    ("Tbilisi", "", {"GE"}, set()),
    # US state names that contain or resemble country names
    ("Albuquerque, New Mexico", "", {"US"}, {"NM"}),
    ("Mexico City, Mexico", "", {"MX"}, set()),
    ("Washington, DC", "", {"US"}, {"DC"}),
    # 2-letter codes stay US states in "City, XX" form (CA, CO, DE, IN, ID, MA, PA, MT, MD, AL, AR, LA, GA, NE, SC, IL, MN, MS, KY)
    ("San Jose, CA", "", {"US"}, {"CA"}),
    ("Denver, CO", "", {"US"}, {"CO"}),
    ("Wilmington, DE", "", {"US"}, {"DE"}),
    ("Indianapolis, IN", "", {"US"}, {"IN"}),
    ("Boise, ID", "", {"US"}, {"ID"}),
    ("Boston, MA", "", {"US"}, {"MA"}),
    # ...while the country names are countries
    ("Toronto, Canada", "", {"CA"}, set()),
    ("Bogotá, Colombia", "", {"CO"}, set()),
    ("Berlin, Germany", "", {"DE"}, set()),
    ("Bengaluru, India", "", {"IN"}, set()),
    ("Jakarta, Indonesia", "", {"ID"}, set()),
    ("Casablanca, Morocco", "", {"MA"}, set()),
    ("Panama City, Panama", "", {"PA"}, set()),
    ("Valletta, Malta", "", {"MT"}, set()),
    ("Chișinău, Moldova", "", {"MD"}, set()),
    ("Tirana, Albania", "", {"AL"}, set()),
    ("Vientiane, Laos", "", {"LA"}, set()),
    ("Libreville, Gabon", "", {"GA"}, set()),
    ("Niamey, Niger", "", {"NE"}, set()),
    ("Ulaanbaatar, Mongolia", "", {"MN"}, set()),
    # ISO coverage beyond the hand-written aliases
    ("Remote, Estonia", "", {"EE"}, set()),
    ("Tallinn, Eesti", "", {"EE"}, set()),  # native-language names
    ("Luxembourg", "", {"LU"}, set()),
    ("Reykjavik, Iceland", "", {"IS"}, set()),
    ("Kathmandu, Nepal", "", {"NP"}, set()),
    ("Accra, Ghana", "", {"GH"}, set()),
    ("Lima, PER", "", {"PE"}, set()),
    # alpha-3 codes vs US time zones and words
    ("Remote (US: PST or EST)", "", {"US"}, set()),
    ("Remote - EST hours", "", set(), set()),
    ("Remote, CST timezone", "", set(), set()),
    ("Tallinn, Estonia", "", {"EE"}, set()),  # "EST" alone is a time zone, not Estonia; use the name
    ("Remote and Hybrid", "", set(), set()),
    ("Remote (Côte d'Ivoire)", "", {"CI"}, set()),
    ("Bolivia", "", {"BO"}, set()),
    ("Viet Nam", "", {"VN"}, set()),
    ("Czechia", "", {"CZ"}, set()),
    ("Türkiye", "", {"TR"}, set()),
    # near-duplicates
    ("Belfast, Northern Ireland", "", {"GB"}, set()),
    ("Dublin, Ireland", "", {"IE"}, set()),
    ("Port Moresby, Papua New Guinea", "", {"PG"}, set()),
    ("Conakry, Guinea", "", {"GN"}, set()),
    ("Bissau, Guinea-Bissau", "", {"GW"}, set()),
    ("Juba, South Sudan", "", {"SS"}, set()),
    ("Khartoum, Sudan", "", {"SD"}, set()),
    ("Santo Domingo, Dominican Republic", "", {"DO"}, set()),
    ("Roseau, Dominica", "", {"DM"}, set()),
    ("Pago Pago, American Samoa", "", {"AS"}, set()),
    ("Apia, Samoa", "", {"WS"}, set()),
    ("Lagos, Nigeria", "", {"NG"}, set()),
    ("Seoul, South Korea", "", {"KR"}, set()),
    ("Kinshasa, Democratic Republic of the Congo", "", {"CD"}, set()),
    # parser misses from the corpus
    ("Rapid City, SD USA", "", {"US"}, {"SD"}),
    ("Store 7010 - Lebanon, TN 37090", "", {"US"}, {"TN"}),
    ("Alexandria, MN 56308", "", {"US"}, {"MN"}),
    ("US Remote", "", {"US"}, set()),
    ("USA Remote", "", {"US"}, set()),
    ("Germany Remote", "", {"DE"}, set()),
    ("Boston-MA", "", {"US"}, {"MA"}),
    ("Utrecht, Utrecht, Nederland", "", {"NL"}, set()),
    ("Charleston", "", {"US"}, set()),           # bare city, unambiguous in the corpus
    ("Shanghai", "", {"CN"}, set()),
    ("Warsaw", "", {"PL"}, set()),               # hand-written alias wins over the (ambiguous) corpus stat
    ("Hamilton", "", set(), set()),              # CA/US/NZ: stays unknown
    ("Main Campus", "", set(), set()),
]

fails = 0
for loc, jd, want_c, want_r in CASES:
    got = parse(loc, jd)
    gc, gr = set(got["countries"]), {r for r in got["regions"] if len(r) == 2}
    if gc != want_c or (want_r and gr != want_r):
        fails += 1; print(f"FAIL {loc!r}: countries {sorted(gc)} (want {sorted(want_c)}), regions {sorted(gr)} (want {sorted(want_r)})")

# find_places (restriction phrases in JDs): longest match wins and is consumed
FP = [
    ("must be located in New Mexico", {"US"}),
    ("candidates must reside in Northern Ireland", {"GB"}),
    ("open to applicants in Papua New Guinea only", {"PG"}),
    ("authorized to work in Guinea", {"GN"}),
    ("based in South Sudan", {"SS"}),
    ("authorized to work in the United States", {"US"}),
    ("eligible to work in the UK", {"GB"}),
    ("hiring in Georgia and Florida", {"US"}),  # prose "Georgia" = the state
]
for text, want in FP:
    cs, _ = find_places(text)
    if cs != want: fails += 1; print(f"FAIL find_places {text!r}: {sorted(cs)} (want {sorted(want)})")

# eligibility: an international preference must not collapse into a US region
EL = [
    ("Remote, Estonia", "Remote - Estonia", "", True),
    ("Remote, Estonia", "Remote - United States", "", False),
    ("Georgia (country)", "Tbilisi, Georgia", "", True),
    ("Georgia (country)", "Atlanta, GA", "", False),
    ("Atlanta, Georgia", "Atlanta, GA", "", True),
    ("Remote, Panama", "Remote - Panama", "", True),
    ("Remote, Panama", "Philadelphia, PA", "", False),
    ("Remote, US", "Remote - US", "", True),
    ("Remote, US", "Remote (US: PST or EST)", "", True),
]
for pref, loc, jd, want in EL:
    got, why = eligibility(pref, loc, jd)
    if got is not want: fails += 1; print(f"FAIL eligibility pref={pref!r} loc={loc!r}: {got} ({why}), want {want}")

# coverage: every ISO alpha-2 is reachable by at least one name
reach = set(COUNTRIES.values()); missing = sorted(set(ISO3_ALL.values()) - reach)
if missing: fails += 1; print(f"FAIL ISO coverage: unreachable {missing}")

n = len(CASES) + len(FP) + len(EL) + 1
print(f"{n - fails}/{n} passed" if not fails else f"{fails} FAILED of {n}")
sys.exit(1 if fails else 0)

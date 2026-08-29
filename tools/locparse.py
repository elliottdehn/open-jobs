"""Turn free-text job locations (+ JD text) into structured fields for faceting.
parse(location, jd) -> {"remote": "remote|hybrid|onsite|unknown", "countries": [...], "regions": [...], "cities": [...]}
Heuristic, deterministic, no network. Countries are ISO-3166 alpha-2; US regions are state codes."""
import re

US_STATES = {"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"District of Columbia"}
STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}
CA_PROV = {"ON":"Ontario","QC":"Quebec","BC":"British Columbia","AB":"Alberta","MB":"Manitoba","SK":"Saskatchewan","NS":"Nova Scotia","NB":"New Brunswick"}
PROV_BY_NAME = {v.lower(): k for k, v in CA_PROV.items()}
COUNTRIES = {
 "united states":"US","usa":"US","u.s.":"US","u.s.a.":"US","us":"US","america":"US","united states of america":"US",
 "united kingdom":"GB","uk":"GB","england":"GB","scotland":"GB","wales":"GB","great britain":"GB","northern ireland":"GB",
 "canada":"CA","germany":"DE","deutschland":"DE","france":"FR","spain":"ES","españa":"ES","italy":"IT","italia":"IT","netherlands":"NL","the netherlands":"NL","belgium":"BE","switzerland":"CH","austria":"AT","sweden":"SE","norway":"NO","denmark":"DK","finland":"FI","ireland":"IE","poland":"PL","polska":"PL","portugal":"PT","czech republic":"CZ","czechia":"CZ","hungary":"HU","romania":"RO","greece":"GR","ukraine":"UA","türkiye":"TR","turkey":"TR",
 "india":"IN","china":"CN","japan":"JP","south korea":"KR","korea":"KR","singapore":"SG","hong kong":"HK","taiwan":"TW","australia":"AU","new zealand":"NZ","philippines":"PH","indonesia":"ID","malaysia":"MY","thailand":"TH","vietnam":"VN","pakistan":"PK","bangladesh":"BD","sri lanka":"LK",
 "mexico":"MX","méxico":"MX","brazil":"BR","brasil":"BR","argentina":"AR","colombia":"CO","chile":"CL","peru":"PE","costa rica":"CR","uruguay":"UY",
 "israel":"IL","united arab emirates":"AE","uae":"AE","dubai":"AE","saudi arabia":"SA","qatar":"QA","egypt":"EG","south africa":"ZA","nigeria":"NG","kenya":"KE","morocco":"MA",
}
CITY_COUNTRY = {  # frequent cities without an explicit country
 "london":"GB","manchester":"GB","birmingham":"GB","edinburgh":"GB","dublin":"IE","paris":"FR","berlin":"DE","munich":"DE","münchen":"DE","hamburg":"DE","frankfurt":"DE","amsterdam":"NL","rotterdam":"NL","brussels":"BE","zurich":"CH","zürich":"CH","geneva":"CH","vienna":"AT","wien":"AT","stockholm":"SE","oslo":"NO","copenhagen":"DK","helsinki":"FI","warsaw":"PL","warszawa":"PL","krakow":"PL","kraków":"PL","prague":"CZ","praha":"CZ","lisbon":"PT","lisboa":"PT","madrid":"ES","barcelona":"ES","milan":"IT","milano":"IT","rome":"IT","athens":"GR","tel aviv":"IL","bangalore":"IN","bengaluru":"IN","hyderabad":"IN","pune":"IN","mumbai":"IN","chennai":"IN","gurgaon":"IN","gurugram":"IN","noida":"IN","delhi":"IN","new delhi":"IN","singapore":"SG","tokyo":"JP","seoul":"KR","sydney":"AU","melbourne":"AU","toronto":"CA","vancouver":"CA","montreal":"CA","montréal":"CA","ottawa":"CA","calgary":"CA","mexico city":"MX","são paulo":"BR","sao paulo":"BR","buenos aires":"AR","bogotá":"CO","bogota":"CO","santiago":"CL","lima":"PE","dubai":"AE","hong kong":"HK","taipei":"TW","manila":"PH","jakarta":"ID","kuala lumpur":"MY","bangkok":"TH","ho chi minh city":"VN","hanoi":"VN","cape town":"ZA","johannesburg":"ZA","lagos":"NG","nairobi":"KE",
 "new york":"US","new york city":"US","nyc":"US","san francisco":"US","sf":"US","los angeles":"US","seattle":"US","austin":"US","boston":"US","chicago":"US","denver":"US","atlanta":"US","miami":"US","dallas":"US","houston":"US","washington":"US","san jose":"US","san diego":"US","portland":"US","phoenix":"US","philadelphia":"US","minneapolis":"US","salt lake city":"US","raleigh":"US","charlotte":"US","nashville":"US","pittsburgh":"US","detroit":"US","columbus":"US","san francisco bay area":"US","bay area":"US","silicon valley":"US","remote - usa":"US",
}
REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
ONSITE_RE = re.compile(r"\b(on-?site|in-?office|in person)\b", re.I)

ISO3 = {"usa":"US","gbr":"GB","ind":"IN","can":"CA","deu":"DE","fra":"FR","aus":"AU","nld":"NL","esp":"ES","ita":"IT","pol":"PL","prt":"PT","irl":"IE","swe":"SE","che":"CH","sgp":"SG","jpn":"JP","bra":"BR","mex":"MX","isr":"IL","are":"AE","phl":"PH","idn":"ID","mys":"MY","vnm":"VN","kor":"KR","chn":"CN","hkg":"HK","twn":"TW","nzl":"NZ","arg":"AR","col":"CO","chl":"CL","zaf":"ZA","nga":"NG","ken":"KE","ukr":"UA","tur":"TR","rou":"RO","hun":"HU","cze":"CZ","grc":"GR","aut":"AT","bel":"BE","dnk":"DK","nor":"NO","fin":"FI"}
COUNTRIES.update(ISO3)

def _split(loc):
    # hyphenated forms: "Mexico-Remote", "US - Remote (…)", "IND-Pune-Smartworks" -> split on hyphens too when a
    # side is a known country/code (keeps "Winston-Salem" intact)
    def hy(m):
        a, b = m.group(1), m.group(2)
        return f"{a}; {b}" if (a.lower() in COUNTRIES or b.lower() in COUNTRIES or REMOTE_RE.fullmatch(a) or REMOTE_RE.fullmatch(b)) else m.group(0)
    loc = re.sub(r"\b([A-Za-z.]+(?: [A-Za-z.]+)?)\s*[-–]\s*([A-Za-z.]+)\b", hy, loc)  # also "Czech Republic-Prague"
    loc = loc.replace(">", ";")  # "Hungary > Budapest"
    loc = re.sub(r"[()\[\]]", ";", loc)  # "Remote (United States)" -> two segments
    loc = re.sub(r"\b(remote|hybrid|on-?site|work from home|wfh)\b\s*[-–:]?\s*", lambda m: m.group(1) + ";", loc, flags=re.I)  # "Hybrid - Austin" -> "hybrid; Austin"
    parts = re.split(r"\s*(?:;|\||/|\bor\b|\band\b|&|•)\s*", loc)
    return [p.strip(" -–,") for p in parts if p and p.strip(" -–,")]

def _one(seg, out):
    s = seg.strip().strip("()[]")
    if not s: return
    low = s.lower()
    if REMOTE_RE.search(low) and len(low) < 40: out["remote_hint"] = True
    # icims style: US-CA-San Jose / US-Remote
    m = re.match(r"^([A-Z]{2})-([A-Z]{2})-(.+)$", s)
    if m and m.group(1) == "US" and m.group(2) in US_STATES:
        out["countries"].add("US"); out["regions"].add(m.group(2)); out["cities"].add(m.group(3).strip().title()); return
    m = re.match(r"^([A-Z]{2,3})-(.+)$", s)
    if m and m.group(1).lower() in COUNTRIES:
        out["countries"].add(COUNTRIES[m.group(1).lower()]); rest = m.group(2).split("-")[0].strip()
        if rest.lower() != "remote": out["cities"].add(rest.title()); return
        return
    toks = [t.strip() for t in re.split(r",", s) if t.strip()]
    if len(toks) == 1 and REMOTE_RE.fullmatch(toks[0].strip().lower()): return  # bare "Remote"
    city = None
    explicit = set(); inferred = set()
    for t in toks:
        tl = t.lower().strip(". ")
        if tl in COUNTRIES: explicit.add(COUNTRIES[tl]); continue
        if t.upper() in US_STATES and (len(t) == 2): explicit.add("US"); out["regions"].add(t.upper()); continue
        if tl in STATE_BY_NAME: explicit.add("US"); out["regions"].add(STATE_BY_NAME[tl]); continue
        if t.upper() in CA_PROV and len(t) == 2: explicit.add("CA"); out["regions"].add("CA-" + t.upper()); continue
        if tl in PROV_BY_NAME: explicit.add("CA"); out["regions"].add("CA-" + PROV_BY_NAME[tl]); continue
        if tl in CITY_COUNTRY:
            inferred.add(CITY_COUNTRY[tl]); out["cities"].add(t.title()); continue
        if REMOTE_RE.search(tl) or tl in ("worldwide", "global", "emea", "apac", "latam", "europe", "eastern europe", "western europe", "north america", "south america", "asia", "africa"):
            if not REMOTE_RE.search(tl): out["regions"].add(tl.title())
            continue
        if city is None and len(t) < 40 and not re.search(r"\d", t) and not REMOTE_RE.search(tl) and not HYBRID_RE.search(tl): city = t
    if city: out["cities"].add(city.title())
    # an explicit country/state wins over a country inferred from a city name (Vienna, VA is not Austria)
    out["countries"] |= explicit if explicit else inferred

def parse(location, jd="", title=""):
    out = {"countries": set(), "regions": set(), "cities": set(), "remote_hint": False}
    for seg in _split(location or ""): _one(seg, out)
    loc = ((location or "") + " | " + (title or "")).lower(); text = (jd or "").lower(); head = text[:2500]
    # An explicit in-office requirement anywhere in the JD beats a "Remote" location (many "Remote - US"
    # postings then say "anchor days Mon/Tue/Fri" or "Monday-Thursday onsite"). Requirement-shaped phrases
    # only, so "hybrid cloud", "onsite interview", "for in-office employees" don't count.
    ONSITE_REQ = re.compile(r"\b(?:\d|one|two|three|four|five)\+? days?(?: a| per| each)? week (?:in|at|from) (?:the |our )?office|anchor days?|"
                            r"\b(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri)\b[^.\n]{0,40}\b(?:on-?site|in[- ]office|in the office)|"
                            r"\b(?:on-?site|in[- ]office|in the office)\b[^.\n]{0,30}\b(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri)\b|"
                            r"\b(?:will )?requires? [^.\n]{0,40}\b(?:on-?site|in[- ]office|in the office)|\bability to work on-?site in\b|"
                            r"\b(?:this|the) (?:role|position) is (?:a )?(?:hybrid|on-?site|in-?office)|\bhybrid (?:role|position|schedule|work(?:ing)? model|model)\b", re.I)
    FULL_ONSITE = re.compile(r"\b(?:fully|100%|entirely) (?:on-?site|in[- ]office)\b|\b(?:5|five) days?(?: a| per)? week (?:in|at) (?:the )?office\b|\bno remote\b|\bnot (?:a )?remote\b|"
                            r"\b(?:work(?:ing)? (?:from|out of|in)|based (?:in|out of)|join (?:us|the team) (?:in|at)) our [^.\n]{0,40}\boffices?\b|\bin[- ]person\b[^.\n]{0,30}\boffice\b", re.I)
    if HYBRID_RE.search(loc): remote = "hybrid"
    elif FULL_ONSITE.search(text) and not HYBRID_RE.search(head): remote = "onsite"
    elif ONSITE_REQ.search(text): remote = "hybrid"
    elif HYBRID_RE.search(head): remote = "hybrid"
    elif out["remote_hint"] or REMOTE_RE.search(loc): remote = "remote"
    elif ONSITE_RE.search(loc) or re.search(r"\b(on-?site|in-?office) (role|position|\d+ days)\b|\bthis (role|position) is (on-?site|in-?office)\b", head): remote = "onsite"
    # a role-specific remote statement in the JD counts anywhere; a company blurb ("remote-first", "fully remote team")
    # counts only when the location isn't a specific city
    elif re.search(r"\b(?:100%|fully|entirely) remote (?:role|position|job|opportunity)\b|\bremote (role|position|job)\b|\bthis (role|position) is (?:fully |100% )?remote\b|\bwork(?:ing)? remotely from anywhere\b|\bwork location:?\s*(?:fully |100% )?remote\b|\b(?:fully|100%) remote\s*(?:[—–\-:,.!]|\n|we hire|must|you)", head): remote = "remote"
    elif not out["cities"] and re.search(r"\b(fully|100%|entirely) remote\b|\bremote[- ]first\b", head): remote = "remote"
    else: remote = "unknown"
    return {"remote": remote, "countries": sorted(out["countries"]), "regions": sorted(out["regions"]), "cities": sorted(out["cities"])[:6]}

if __name__ == "__main__":
    import sys, json
    for t in ["US-CA-San Jose", "Remote - USA", "Paris; Warsaw; Lisboa; Berlin", "Vienna, VA, USA", "Bengaluru, India", "New York City; Remote / San Francisco", "London, United Kingdom", "Hybrid - Austin, TX", "Toronto, ON", "Berlin, Germany; Frankfurt, Germany", "Remote (United States)"]:
        print(f"{t:45s} -> {json.dumps(parse(t))}")


# ---- eligibility against the user's stated location preference ----
RESTRICT_RE = re.compile(r"\b(must (?:be|reside|live|be located|be based)[^.]{0,40}?\b(in|within)\b|(?:only|exclusively) (?:for|open to)? ?(?:candidates|applicants|residents)?[^.]{0,20}?(?:in|based in|located in|from)\b|(?:authori[sz]ed|eligible) to work in|(?:based|located|residing) in|(?:us|u\.s\.|united states)[- ]?(?:only|based)|(?:latam|emea|apac|europe|india|canada|uk)[- ]?only)\b[^.\n]{0,60}", re.I)
MACRO = {"Latam": {"MX","BR","AR","CO","CL","PE","CR","UY"}, "Emea": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","SE","NO","DK","FI","IE","PL","PT","CZ","HU","RO","GR","UA","TR","IL","AE","SA","QA","EG","ZA","NG","KE","MA"}, "Apac": {"IN","CN","JP","KR","SG","HK","TW","AU","NZ","PH","ID","MY","TH","VN","PK","BD","LK"}, "Europe": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","SE","NO","DK","FI","IE","PL","PT","CZ","HU","RO","GR","UA"}, "Eastern Europe": {"PL","CZ","HU","RO","UA","GR"}, "Western Europe": {"GB","DE","FR","ES","IT","NL","BE","CH","AT","IE","PT"}, "North America": {"US","CA","MX"}, "South America": {"BR","AR","CO","CL","PE","UY"}, "Asia": {"IN","CN","JP","KR","SG","HK","TW","PH","ID","MY","TH","VN","PK","BD","LK"}, "Africa": {"ZA","NG","KE","EG","MA"}}

_PLACE_KEYS = sorted(COUNTRIES.keys(), key=len, reverse=True)
_MACRO_KEYS = sorted(MACRO.keys(), key=len, reverse=True)
def find_places(text):
    """Countries (ISO-2) and macro-regions named anywhere in a phrase, e.g. 'must be located in the United States'."""
    t = " " + re.sub(r"[^a-z0-9. ]+", " ", (text or "").lower()) + " "
    cs, rs = set(), set()
    for k in _PLACE_KEYS:
        if len(k) < 3 and k not in ("uk", "us"): continue  # skip 2-letter noise except uk/us
        if f" {k} " in t or f" {k}. " in t: cs.add(COUNTRIES[k])
    for k in _MACRO_KEYS:
        if f" {k.lower()} " in t: rs.add(k)
    for st in STATE_BY_NAME:  # US state names imply US
        if f" {st} " in t: cs.add("US")
    return cs, rs

def eligibility(pref, location, jd="", title="", arrangement=None):
    """Is a job eligible for someone with location preference `pref` (e.g. "Remote, US", "Berlin",
    "US or Canada")? Returns (eligible: bool|None, reason). None = can't tell (no location info).
    Rules: the job's countries must intersect the user's; a remote job with no country and no
    restricting phrase is eligible; region-restricted remotes (Remote - LATAM, India-only) are not;
    JD phrases like "must be located in the US" / "authorized to work in the UK" restrict too.
    A remote-only preference (no city, e.g. "Remote, US") makes onsite and hybrid jobs ineligible;
    `arrangement` overrides the parsed one (pass the enrichment's work_arrangement when known)."""
    p = parse(pref or "")
    j = parse(location or "", jd, title)
    rm = arrangement if arrangement in ("remote", "hybrid", "onsite") else j["remote"]
    if p["remote"] == "remote" and not p["cities"]:
        if rm in ("onsite", "hybrid"): return False, f"{rm} (you asked for remote)"
        if rm == "unknown" and j["cities"] and not REMOTE_RE.search(jd or "") and not HYBRID_RE.search(jd or ""):
            return False, f"{j['cities'][0]}, no remote mention"
    want = set(p["countries"])
    for r in p["regions"]: want |= MACRO.get(r, set())
    if not want: return None, "no country in preference"
    have = set(j["countries"])
    for r in j["regions"]: have |= MACRO.get(r, set())
    # restricting phrases in the JD text add countries/regions to `have`
    for m in RESTRICT_RE.finditer((jd or "")[:6000]):
        cs, rs = find_places(m.group(0))
        have |= cs
        for r in rs: have |= MACRO.get(r, set())
    if have & want: return True, "location matches"
    if have: return False, f"restricted to {', '.join(sorted(have))[:40]}"
    if j["remote"] == "remote": return True, "remote, no stated region"
    return None, "no location info"

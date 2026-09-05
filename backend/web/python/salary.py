"""Mechanical salary extraction from job description text. No LLM.
extract(text) -> {"min": float, "max": float, "currency": "USD", "period": "year|month|week|day|hour", "raw": str} | None
Picks the most plausible stated range (pay-transparency style), annualized in `annual_min/annual_max`."""
import re
import sys
# Windows consoles default to cp1252; our output has ✓ · – etc. Reconfigure stdout/stderr to UTF-8 (no-op elsewhere).
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

CUR = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "cad": "CAD", "c$": "CAD", "ca$": "CAD", "aud": "AUD", "a$": "AUD", "inr": "INR", "₹": "INR", "rs.": "INR", "rs": "INR", "chf": "CHF", "sgd": "SGD", "s$": "SGD", "nzd": "NZD", "mxn": "MXN", "brl": "BRL", "r$": "BRL", "pln": "PLN", "zł": "PLN", "sek": "SEK", "dkk": "DKK", "nok": "NOK", "jpy": "JPY", "¥": "JPY", "hkd": "HKD", "aed": "AED"}
# thousands groups (incl. Indian 2-2-3 grouping), optional cents; must not start mid-number
NUM = r"(?<![\d,.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?)\s*([kK])?(?![\d,])"
CURPAT = r"(us\$|usd|ca\$|c\$|cad|a\$|aud|s\$|sgd|nzd|inr|rs\.?|chf|mxn|brl|r\$|pln|sek|dkk|nok|jpy|hkd|aed|eur|gbp|\$|€|£|₹|¥|zł)"
RANGE = re.compile(rf"{CURPAT}?\s*{NUM}\s*(?:{CURPAT})?\s*(?:-|–|—|to|and|through)\s*{CURPAT}?\s*{NUM}\s*({CURPAT})?", re.I)
SINGLE = re.compile(rf"{CURPAT}\s*{NUM}(?:\s*({CURPAT}))?", re.I)
PERIOD = re.compile(r"(per|/|an?|each)\s*(hour|hr|h|day|week|wk|month|mo|year|yr|annum|annual|a)\b|\b(hourly|annually|yearly|monthly|weekly|daily|per year|per hour)\b", re.I)
CONTEXT = re.compile(r"(salary|compensation|pay|base|range|ote|total cash|rate|wage|remuneration)", re.I)

def _num(s, k):
    v = float(s.replace(",", ""))
    return v * 1000 if k else v

def _period(after, before):
    m = PERIOD.search(after[:40]) or PERIOD.search(before[-40:])
    if not m: return None
    t = (m.group(2) or m.group(3) or "").lower()
    if t.startswith(("hour", "hr", "h")): return "hour"
    if t.startswith(("day", "dai")): return "day"
    if t.startswith(("week", "wk")): return "week"
    if t.startswith(("month", "mo")): return "month"
    return "year"

# plausibility caps depend on the currency's magnitude (INR/JPY/MXN annual figures run into the millions)
BIG = {"INR": 100, "JPY": 150, "MXN": 20, "PHP": 55, "BRL": 5, "PLN": 4, "SEK": 10, "DKK": 7, "NOK": 10, "HKD": 8, "ZAR": 18, "AED": 4, "KRW": 1300, "TWD": 32}
def _cap(cur): return BIG.get(cur, 1)

def extract(text):
    if not text: return None
    t = text.replace(" ", " ")
    best = None
    for m in RANGE.finditer(t):
        c = (m.group(1) or m.group(4) or m.group(5) or m.group(8) or "").lower()
        lo, hi = _num(m.group(2), m.group(3)), _num(m.group(6), m.group(7))
        before, after = t[max(0, m.start() - 120):m.start()], t[m.end():m.end() + 80]
        if not c:
            # no currency: accept only "120k-150k" style with a salary word nearby
            if not (m.group(3) and m.group(7) and CONTEXT.search(before)): continue
            c = "$"
        if hi < lo: lo, hi = hi, lo
        period = _period(after, before)
        if period is None: period = "hour" if hi < 500 else "year" if hi >= 20000 else None
        if period is None or hi <= 0: continue
        cur = CUR.get(c, c.upper()); f = _cap(cur)
        # a k-suffixed range is annual whatever the surrounding prose says ("$91k - $105K … on a daily basis")
        if m.group(3) and m.group(7) and hi >= 20000 * f: period = "year"
        if period == "hour" and not (5 * f <= hi <= 500 * f): continue
        if period == "day" and not (20 * f <= hi <= 5_000 * f): continue
        if period == "week" and not (100 * f <= hi <= 25_000 * f): continue
        if period == "year" and not (8000 * f <= hi <= 2_000_000 * f): continue
        if period == "month" and not (500 * f <= hi <= 100_000 * f): continue
        score = 2 + (1 if CONTEXT.search(before) else 0) + (0.5 if hi != lo else 0)
        cand = {"min": lo, "max": hi, "currency": cur, "period": period, "raw": t[m.start():m.end()].strip(), "_s": score}
        if best is None or cand["_s"] > best["_s"]: best = cand
    if best is None:
        for m in SINGLE.finditer(t):
            c = (m.group(1) or "").lower(); v = _num(m.group(2), m.group(3))
            before, after = t[max(0, m.start() - 120):m.start()], t[m.end():m.end() + 80]
            if not CONTEXT.search(before): continue
            # single figures are risky ("$7 billion", "$350 credit"): need an explicit period, or a clearly annual value
            period = _period(after, before)
            cur = CUR.get(c, c.upper()); f = _cap(cur)
            if period is None:
                if v >= 20000 * f: period = "year"
                else: continue
            if m.group(3) and v >= 20000 * f: period = "year"  # "$95k" is annual regardless of nearby prose
            if period == "year" and not (8000 * f <= v <= 2_000_000 * f): continue
            if period == "hour" and not (5 * f <= v <= 500 * f): continue
            if period == "day" and not (20 * f <= v <= 5_000 * f): continue
            if period == "week" and not (100 * f <= v <= 25_000 * f): continue
            if period == "month" and not (500 * f <= v <= 100_000 * f): continue
            best = {"min": v, "max": v, "currency": cur, "period": period, "raw": t[m.start():m.end()].strip(), "_s": 1}; break
    if not best: return None
    mult = {"hour": 2080, "day": 260, "week": 52, "month": 12, "year": 1}[best["period"]]
    best["annual_min"], best["annual_max"] = round(best["min"] * mult), round(best["max"] * mult)
    if best["annual_max"] < 8000 * _cap(best["currency"]): return None  # stipends, per-diems, allowances
    best.pop("_s", None)
    return best

if __name__ == "__main__":
    for s in ["The base salary range for this role is $180,000 - $220,000 per year plus equity.", "Compensation: $85–$110/hour, W2", "Salary: £70,000 to £90,000 depending on experience", "Pay: €65k - €80k", "USD 150K - 190K annually", "We were founded in 2015 and serve 2,000 - 5,000 customers", "Base pay range: $190,000.00 - $250,000.00", "Rate: $65 per hour", "Total compensation of ₹25,00,000 - ₹40,00,000", "salary 120k-150k",
              "OTE compensation: $91k - $105K (base + variable) Work on a daily basis with the founding team", "Pay: $200/day plus expenses", "Stipend of $50 weekly"]:
        print(f"{s[:60]:60s} -> {extract(s)}")

# /// script
# requires-python = ">=3.10"
# ///
"""Discover big-company career sites and classify their recruiting platform.

For each company name (Fortune-500-ish list piped in), asks OpenAI (web search, structured output)
for the official careers site, then verifies mechanically: Phenom (/widgets) and Jibe (/api/jobs)
fingerprints on the careers host, and standard-ATS hosts (myworkdayjobs / oraclecloud / eightfold)
found in the page source or redirects. Every API answer is cached in export/careers-cache.json keyed
by company, so re-runs only pay for new names.

Usage:  cd backend && uv run scripts/discover-careers.py names.txt   (one company per line)
Output: export/careers-discovered.json  {phenom: [...], jibe: [...], workday: [...], oraclecloud: [...], eightfold: [...], unknown: [...]}
Then: review, merge the confirmed hosts into src/boards.json, deploy, POST /fetch-all?ats=…"""
import json, os, re, ssl, sys, urllib.request, concurrent.futures, socket, time

here = os.path.dirname(os.path.abspath(__file__)); root = os.path.join(here, "..")
CACHE = os.path.join(root, "export", "careers-cache.json")
OUT = os.path.join(root, "export", "careers-discovered.json")
KEY = os.environ.get("OPENAI_API_KEY") or open(os.path.join(root, "..", "oai_key.txt")).read().strip()
socket.setdefaulttimeout(10)
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["company", "careers_url", "platform_guess"],
          "properties": {"company": {"type": "string"}, "careers_url": {"type": "string"},
                         "platform_guess": {"type": "string", "enum": ["workday", "oraclecloud", "eightfold", "phenom", "jibe", "icims", "taleo", "successfactors", "custom", "unknown"]}}}

def lookup(name):
    body = {"model": "gpt-5.6-luna", "tools": [{"type": "web_search"}], "max_output_tokens": 1500,
            "instructions": "Find the CURRENT official careers/jobs search site for this large US company (where open jobs are listed, not a marketing page). Guess the recruiting platform from the job URLs if visible. Strict JSON.",
            "input": name, "text": {"format": {"type": "json_schema", "name": "careers", "schema": SCHEMA, "strict": True}}}
    for a in range(4):
        try:
            req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                         headers={"authorization": f"Bearer {KEY}", "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r: res = json.loads(r.read())
            txt = next(x["text"] for o in res["output"] if o["type"] == "message" for x in o["content"] if x["type"] == "output_text")
            return json.loads(txt)
        except Exception as e:
            if a == 3: return {"company": name, "careers_url": "", "platform_guess": "unknown", "error": str(e)[:200]}
            time.sleep(3 * (a + 1))

WIDGET = json.dumps({"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results", "ddoKey": "refineSearch",
                     "sortBy": "", "from": 0, "jobs": True, "counts": True, "all_fields": [], "size": 1, "clearAll": False,
                     "jdsource": "facets", "isSliderEnable": False, "pageId": "page1", "siteType": "external", "keywords": "",
                     "global": True, "selected_fields": {}, "locationData": {}}).encode()

def get(url, data=None, headers=None, cap=400_000):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, context=ctx) as r: return r.read(cap).decode("utf-8", "ignore"), r.geturl()

def probe(host):
    """Returns (kind, slug) or None. kind in phenom|jibe|workday|oraclecloud|eightfold."""
    try:
        body, _ = get(f"https://{host}/api/jobs?page=1&limit=1")
        d = json.loads(body)
        if isinstance(d, dict) and isinstance(d.get("jobs"), list) and d.get("totalCount") is not None: return ("jibe", host)
    except Exception: pass
    try:
        body, _ = get(f"https://{host}/widgets", data=WIDGET, headers={"content-type": "application/json"})
        d = json.loads(body)
        if isinstance(d, dict) and (d.get("refineSearch") or {}).get("data") is not None: return ("phenom", host)
    except Exception: pass
    try:
        body, final = get(f"https://{host}/")
        for pat, kind in ((r"([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)", "workday"),
                          (r"([a-z0-9-]+\.fa\.[a-z0-9.]+\.oraclecloud\.com)", "oraclecloud"),
                          (r"([a-z0-9-]+)\.eightfold\.ai", "eightfold")):
            m = re.search(pat, body) or re.search(pat, final)
            if m: return (kind, m.group(1) if kind != "eightfold" else m.group(1))
    except Exception: pass
    return None

def main():
    names = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    todo = [n for n in names if n not in cache]
    print(f"{len(names)} companies, {len(names) - len(todo)} cached, {len(todo)} to look up")
    spent_hint = len(todo) * 0.012
    if todo: print(f"(~${spent_hint:.2f} of web-search + tokens)")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        for name, res in zip(todo, ex.map(lookup, todo)):
            cache[name] = res; done += 1
            if done % 10 == 0:
                json.dump(cache, open(CACHE, "w"), indent=1); print(f"  {done}/{len(todo)} looked up", flush=True)
    json.dump(cache, open(CACHE, "w"), indent=1)
    # candidate hosts per company: the discovered host + conventional guesses on the same domain
    cands = {}
    for name in names:
        c = cache.get(name) or {}
        url = c.get("careers_url") or ""
        m = re.match(r"https?://([^/]+)", url)
        hosts = set()
        if m:
            h = m.group(1).lower()
            hosts.add(h)
            dm = re.sub(r"^(www|careers|jobs|apply|talent)\.", "", h)
            hosts |= {f"careers.{dm}", f"jobs.{dm}"}
        cands[name] = sorted(hosts)
    results = {"phenom": [], "jibe": [], "workday": [], "oraclecloud": [], "eightfold": [], "unknown": []}
    seen = set()
    def work(item):
        name, hosts = item
        for h in hosts:
            r = probe(h)
            if r: return (name, r)
        return (name, None)
    with concurrent.futures.ThreadPoolExecutor(24) as ex:
        for name, r in ex.map(work, cands.items()):
            if r is None: results["unknown"].append(name); continue
            kind, slug = r
            if slug in seen: continue
            seen.add(slug); results[kind].append({"company": name, "slug": slug})
            print(f"  {kind:11s} {slug:44s} ({name})", flush=True)
    json.dump(results, open(OUT, "w"), indent=1)
    print("wrote", OUT, {k: len(v) for k, v in results.items()})

if __name__ == "__main__": main()

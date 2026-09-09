# /// script
# requires-python = ">=3.10"
# ///
"""Cloudflare usage meters for this account, per day, with a list-price estimate: Durable Object duration, SQLite
rows written/read, storage, requests; R2 storage and operations; Workers. This is what the invoice is made of.

  uv run scripts/cf-usage.py [--days 31]

Auth: CLOUDFLARE_API_TOKEN (Account Analytics: Read) or, failing that, the wrangler login's OAuth token on disk.
Account id from `wrangler whoami` (CLOUDFLARE_ACCOUNT_ID overrides).
"""
import argparse, datetime, json, os, re, subprocess, sys, urllib.request

ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=31); a = ap.parse_args()
tok = os.environ.get("CLOUDFLARE_API_TOKEN")
if not tok:
    for p in (os.path.expanduser("~/Library/Preferences/.wrangler/config/default.toml"), os.path.expanduser("~/.wrangler/config/default.toml"), os.path.expanduser("~/.config/.wrangler/config/default.toml")):
        if os.path.exists(p):
            m = re.search(r'oauth_token\s*=\s*"([^"]+)"', open(p).read())
            if m: tok = m.group(1); break
if not tok: sys.exit("no CLOUDFLARE_API_TOKEN and no wrangler login found")
acc = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
if not acc:
    out = subprocess.run(["npx", "wrangler", "whoami"], capture_output=True, text=True, cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")).stdout
    m = re.search(r"([0-9a-f]{32})", out); acc = m.group(1) if m else sys.exit("account id not found; set CLOUDFLARE_ACCOUNT_ID")

def gql(q):
    r = urllib.request.Request("https://api.cloudflare.com/client/v4/graphql", data=json.dumps({"query": q}).encode(), headers={"Authorization": f"Bearer {tok}", "content-type": "application/json"})
    d = json.load(urllib.request.urlopen(r, timeout=120))
    if d.get("errors"): sys.exit(json.dumps(d["errors"])[:800])
    return d["data"]["viewer"]["accounts"][0]
since = (datetime.date.today() - datetime.timedelta(days=a.days)).isoformat()
d = gql(f'''{{ viewer {{ accounts(filter:{{accountTag:"{acc}"}}) {{
  per: durableObjectsPeriodicGroups(limit:400, filter:{{date_geq:"{since}"}}, orderBy:[date_ASC]) {{ dimensions {{ date }} sum {{ duration rowsRead rowsWritten storageReadUnits storageWriteUnits storageDeletes }} }}
  inv: durableObjectsInvocationsAdaptiveGroups(limit:400, filter:{{date_geq:"{since}"}}, orderBy:[date_ASC]) {{ dimensions {{ date }} sum {{ requests }} }}
  sql: durableObjectsSqlStorageGroups(limit:400, filter:{{date_geq:"{since}"}}, orderBy:[date_ASC]) {{ dimensions {{ date }} max {{ storedBytes }} }}
  r2s: r2StorageAdaptiveGroups(limit:5, filter:{{date_geq:"{since}"}}, orderBy:[date_DESC]) {{ dimensions {{ date }} max {{ payloadSize objectCount }} }}
  r2o: r2OperationsAdaptiveGroups(limit:40, filter:{{date_geq:"{since}"}}) {{ dimensions {{ actionType }} sum {{ requests }} }}
  wk: workersInvocationsAdaptive(limit:5, filter:{{date_geq:"{since}"}}) {{ sum {{ requests cpuTimeUs }} }}
}} }} }}''')
inv = {x["dimensions"]["date"]: x["sum"]["requests"] for x in d["inv"]}
sql = {x["dimensions"]["date"]: x["max"]["storedBytes"] for x in d["sql"]}
print(f"{'date':10}  {'GB-s':>9}  {'rowsW(M)':>8}  {'rowsR(M)':>8}  {'reqs(M)':>7}  {'SQL GB':>6}   {'$/day':>6}")
tot = {"duration": 0, "rowsWritten": 0, "rowsRead": 0, "reqs": 0}; days = 0
for x in d["per"]:
    s = x["sum"]; dt = x["dimensions"]["date"]; days += 1
    cost = s["duration"] * 12.5 / 1e6 + s["rowsWritten"] / 1e6 + s["rowsRead"] * 0.001 / 1e6 + inv.get(dt, 0) * 0.15 / 1e6
    for k in ("duration", "rowsWritten", "rowsRead"): tot[k] += s[k]
    tot["reqs"] += inv.get(dt, 0)
    print(f"{dt}  {s['duration']:>9.0f}  {s['rowsWritten']/1e6:>8.2f}  {s['rowsRead']/1e6:>8.1f}  {inv.get(dt,0)/1e6:>7.2f}  {sql.get(dt,0)/1e9:>6.1f}   {cost:>6.2f}")
gb = max(sql.values()) / 1e9 if sql else 0
est = {
    "DO duration ($12.50/M GB-s, 400k free)": max(0, tot["duration"] - 4e5) * 12.5 / 1e6,
    "DO rows written ($1/M, 50M free)": max(0, tot["rowsWritten"] - 5e7) / 1e6,
    "DO rows read ($0.001/M, 25B free)": max(0, tot["rowsRead"] - 25e9) * 0.001 / 1e6,
    "DO requests ($0.15/M, 1M free)": max(0, tot["reqs"] - 1e6) * 0.15 / 1e6,
    f"DO SQLite storage ($0.20/GB-mo, 5 GB free; {gb:.0f} GB)": max(0, gb - 5) * 0.2 * days / 30,
}
r2 = d["r2s"][0]["max"] if d["r2s"] else {"payloadSize": 0, "objectCount": 0}
ops = {x["dimensions"]["actionType"]: x["sum"]["requests"] for x in d["r2o"]}
a_ops = sum(v for k, v in ops.items() if k not in ("GetObject", "HeadObject", "ListObjects")); b_ops = sum(v for k, v in ops.items() if k in ("GetObject", "HeadObject", "ListObjects"))
est[f"R2 storage ($0.015/GB-mo, 10 GB free; {r2['payloadSize']/1e9:.0f} GB, {r2['objectCount']:,} objects)"] = max(0, r2["payloadSize"] / 1e9 - 10) * 0.015 * days / 30
est[f"R2 class A ops ($4.50/M, 1M free; {a_ops/1e6:.2f}M)"] = max(0, a_ops - 1e6) * 4.5 / 1e6
est[f"R2 class B ops ($0.36/M, 10M free; {b_ops/1e6:.2f}M)"] = max(0, b_ops - 1e7) * 0.36 / 1e6
wk = d["wk"][0]["sum"] if d["wk"] else {"requests": 0, "cpuTimeUs": 0}
est[f"Workers ({wk['requests']/1e6:.2f}M requests, {wk['cpuTimeUs']/1e9:.1f}k CPU-s)"] = max(0, wk["requests"] - 1e7) * 0.3 / 1e6 + max(0, wk["cpuTimeUs"] / 1e3 - 3e7) * 0.02 / 1e6
print(f"\nestimate for the last {days} days at list prices (Workers Paid plan; the invoice window differs):")
for k, v in est.items(): print(f"  {v:>8.2f}  {k}")
print(f"  {sum(est.values()):>8.2f}  total")

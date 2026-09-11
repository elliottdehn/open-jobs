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

ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=31); ap.add_argument("--egress", action="store_true", help="bytes served to users by the backend Worker: rolling 24 h windows (T-24h, T-48h, T-72h) and UTC day buckets over --days"); ap.add_argument("--hours", type=int, help="per-hour view of the last N hours instead of per-day (steady-state check)"); a = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__))
tok = os.environ.get("CLOUDFLARE_API_TOKEN")
whoami = ""
if not tok:
    # wrangler refreshes its OAuth token on use; run it first so the token on disk is current
    whoami = subprocess.run(["npx", "wrangler", "whoami"], capture_output=True, text=True, cwd=os.path.join(HERE, "..")).stdout
    for p in (os.path.expanduser("~/Library/Preferences/.wrangler/config/default.toml"), os.path.expanduser("~/.wrangler/config/default.toml"), os.path.expanduser("~/.config/.wrangler/config/default.toml")):
        if os.path.exists(p):
            m = re.search(r'oauth_token\s*=\s*"([^"]+)"', open(p).read())
            if m: tok = m.group(1); break
if not tok: sys.exit("no CLOUDFLARE_API_TOKEN and no wrangler login found")
acc = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
if not acc:
    out = whoami or subprocess.run(["npx", "wrangler", "whoami"], capture_output=True, text=True, cwd=os.path.join(HERE, "..")).stdout
    m = re.search(r"([0-9a-f]{32})", out); acc = m.group(1) if m else sys.exit("account id not found; set CLOUDFLARE_ACCOUNT_ID")

def gql(q):
    r = urllib.request.Request("https://api.cloudflare.com/client/v4/graphql", data=json.dumps({"query": q}).encode(), headers={"Authorization": f"Bearer {tok}", "content-type": "application/json"})
    d = json.load(urllib.request.urlopen(r, timeout=120))
    if d.get("errors"): sys.exit(json.dumps(d["errors"])[:800])
    return d["data"]["viewer"]["accounts"][0]
if a.egress:
    # Egress = the Worker's response bodies (tar, parquet, group files, API), all free on Workers + R2. The R2-side
    # "responseObjectSize" is the size of the object touched, not bytes moved (a range read of the 13 GB tar counts
    # 13 GB), so it is not used. Container reads via the S3 API are not in here either.
    now = datetime.datetime.utcnow().replace(microsecond=0)
    def win(h0, h1):
        s_, e_ = (now - datetime.timedelta(hours=h0)).isoformat() + "Z", (now - datetime.timedelta(hours=h1)).isoformat() + "Z"
        g = gql(f'{{ viewer {{ accounts(filter:{{accountTag:"{acc}"}}) {{ w: workersInvocationsAdaptive(limit:5, filter:{{scriptName:"backend", datetime_geq:"{s_}", datetime_lt:"{e_}"}}) {{ sum {{ requests responseBodySize }} }} }} }} }}')["w"]
        r = sum(x["sum"]["requests"] for x in g); b = sum(x["sum"]["responseBodySize"] for x in g); return r, b
    print(f"backend Worker egress (bytes to users), rolling 24 h windows ending {now.isoformat()}Z:")
    for label, h0, h1 in (("T-24h..now", 24, 0), ("T-48h..T-24h", 48, 24), ("T-72h..T-48h", 72, 48)):
        r, b = win(h0, h1); print(f"  {label:14} {r:>10,} requests  {b/1e9:8.1f} GB")
    days = min(a.days, 31); since = (now - datetime.timedelta(days=days)).date().isoformat()
    g = gql(f'{{ viewer {{ accounts(filter:{{accountTag:"{acc}"}}) {{ w: workersInvocationsAdaptive(limit:100, filter:{{scriptName:"backend", date_geq:"{since}"}}, orderBy:[date_ASC]) {{ dimensions {{ date }} sum {{ requests responseBodySize }} }} }} }} }}')["w"]
    print(f"\nUTC day buckets (the current day is partial):")
    for x in g: print(f"  {x['dimensions']['date']}   {x['sum']['requests']:>10,} requests  {x['sum']['responseBodySize']/1e9:8.1f} GB")
    sys.exit(0)
if a.hours:
    t0 = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=a.hours)).replace(minute=0, second=0, microsecond=0)
    f = f'datetime_geq:"{t0.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    h = gql(f'''{{ viewer {{ accounts(filter:{{accountTag:"{acc}"}}) {{
      per: durableObjectsPeriodicGroups(limit:200, filter:{{{f}}}, orderBy:[datetimeHour_ASC]) {{ dimensions {{ datetimeHour }} sum {{ duration rowsRead rowsWritten }} }}
      inv: durableObjectsInvocationsAdaptiveGroups(limit:200, filter:{{{f}}}, orderBy:[datetimeHour_ASC]) {{ dimensions {{ datetimeHour }} sum {{ requests }} }}
    }} }} }}''')
    inv = {x["dimensions"]["datetimeHour"]: x["sum"]["requests"] for x in h["inv"]}
    print(f"{'hour (UTC)':16}  {'GB-s':>8}  {'rowsW(M)':>8}  {'rowsR(M)':>8}  {'reqs(k)':>7}  {'$/hour':>6}  {'=> $/day':>8}")
    for x in h["per"]:
        s_ = x["sum"]; dt = x["dimensions"]["datetimeHour"]
        cost = s_["duration"] * 12.5 / 1e6 + s_["rowsWritten"] / 1e6 + s_["rowsRead"] * 0.001 / 1e6 + inv.get(dt, 0) * 0.15 / 1e6
        print(f"{dt[:13]:16}  {s_['duration']:>8.0f}  {s_['rowsWritten']/1e6:>8.2f}  {s_['rowsRead']/1e6:>8.1f}  {inv.get(dt,0)/1e3:>7.1f}  {cost:>6.2f}  {cost*24:>8.2f}")
    print("the current hour is partial; analytics lag a few minutes. Storage (~$0.03/hour) is not in these rows.")
    sys.exit(0)
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

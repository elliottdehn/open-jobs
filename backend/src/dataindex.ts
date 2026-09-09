// GET /data/  -> the human-readable index of the public files, ordered the way a reader uses them: take all of
// today's data, keep it current, then the history and the search index. Rendered from the bucket listing and the
// same index files a mirror reads (diffs/index.json, ledger/index.json, changes/latest.json, manifest.json), so it
// can never disagree with them. Cached an hour like everything else under /data/.

const REPO = "https://github.com/elliottdehn/open-jobs";
const BASE = "https://backend.dehnbostele.workers.dev/data/";

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);
const gb = (n?: number) => !n ? "" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${(n / 1e3).toFixed(0)} kB`;
const num = (x?: number) => x === undefined || x === null ? "" : x.toLocaleString("en-US");
const when = (ms?: number) => ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "";
const parts = (dir: string, ps?: { file: string }[]) => (ps ?? []).map((p) => `<a href="/data/${esc(dir)}${esc(p.file)}">${esc(p.file)}</a>`).join(" ");

async function json(env: { DATA: R2Bucket }, key: string): Promise<any | null> {
	const o = await env.DATA.get(key);
	return o ? o.json() : null;
}
async function listFiles(env: { DATA: R2Bucket }, prefix: string): Promise<{ name: string; size: number }[]> {
	const r = await env.DATA.list({ prefix, delimiter: "/" });
	return r.objects.map((o) => ({ name: o.key.slice(prefix.length), size: o.size })).filter((o) => o.name);
}

export async function dataIndex(env: { DATA: R2Bucket }, cors: Record<string, string>): Promise<Response> {
	const [ledger, diffs, feed, manifestHead, exportDays] = await Promise.all([
		json(env, "ledger/index.json"), json(env, "diffs/index.json"), json(env, "changes/latest.json"),
		env.DATA.head("manifest.json"), env.DATA.list({ prefix: "exports/", delimiter: "/" }),
	]);
	const days = exportDays.delimitedPrefixes.map((p) => p.slice("exports/".length, -1)).sort();
	const head: string = days.includes(diffs?.head) ? diffs.head : days.at(-1) ?? "";
	const [jobs, boards] = head ? await Promise.all([listFiles(env, `exports/${head}/jobs/`), listFiles(env, `exports/${head}/boards/`)]) : [[], []];
	const jobsBytes = jobs.reduce((a, f) => a + f.size, 0);

	// the manifest is 20+ MB; its small scalar fields sit in the first few hundred bytes
	const manifest: Record<string, any> = {};
	if (manifestHead) {
		const o = await env.DATA.get("manifest.json", { range: { offset: 0, length: 400 } });
		const text = o ? await o.text() : "";
		for (const k of ["recipe", "dims", "jobs", "nodes", "leaves", "built_at"]) {
			const m = text.match(new RegExp(`"${k}"\\s*:\\s*("[^"]*"|\\d+)`));
			if (m) manifest[k] = m[1].startsWith('"') ? m[1].slice(1, -1) : Number(m[1]);
		}
	}

	const diffEntries = ((diffs?.entries ?? []) as any[]).slice().reverse();
	const latestDiff = diffEntries[0];
	const ledgerEntries = ((ledger?.entries ?? []) as any[]).slice().reverse();
	const feedPages = (feed?.pages ?? []) as any[];
	const feedBytes = feedPages.reduce((a, p) => a + (p.bytes ?? 0), 0);
	const atsList = jobs.map((f) => f.name.replace(/\.parquet$/, ""));

	const jobRows = jobs.map((f) => `<tr><td><a href="/data/exports/${esc(head)}/jobs/${esc(f.name)}">${esc(f.name)}</a></td><td class="n">${gb(f.size)}</td>` +
		`<td><a href="/data/exports/${esc(head)}/boards/${esc(f.name)}">boards/${esc(f.name)}</a></td></tr>`).join("");
	const diffRows = diffEntries.map((e) =>
		`<tr><td>${esc(e.from)} → ${esc(e.to)}</td><td class="n">${num(e.counts?.added)}</td><td class="n">${num(e.counts?.removed)}</td><td class="n">${num(e.counts?.changed)}</td>` +
		`<td class="n">${gb(e.lite?.bytes)}</td><td>${parts(e.lite?.dir ?? "", e.lite?.parts)}</td><td class="n">${gb(e.bytes)}</td><td>${parts(e.dir, e.parts)}</td>` +
		`<td><a href="/data/${esc(e.sidecar)}">json</a></td></tr>`).join("");
	const ledgerRows = ledgerEntries.map((e) => `<tr><td>${esc(e.date)}</td><td class="n">${gb(e.bytes)}</td><td>${parts(e.dir, e.parts)}</td></tr>`).join("");

	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open Jobs data</title>
<style>
:root{--bg:#f6f7f9;--ink:#15181d;--soft:#5b6270;--rule:#dfe3ea;--panel:#eceff4;--acc:#0f6e63;--acc-ink:#0b5a51}
@media (prefers-color-scheme:dark){:root{--bg:#131518;--ink:#e8eaee;--soft:#9aa3b2;--rule:#2a2f38;--panel:#1c2026;--acc:#4fc3b4;--acc-ink:#7fd9cc}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:48px 24px 80px}
a{color:var(--acc-ink);text-decoration:none;border-bottom:1px solid transparent}a:hover{border-bottom-color:currentColor}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 10px}
.gh{display:inline-flex;align-items:center;gap:9px;padding:9px 13px;border:1px solid var(--rule);border-radius:6px;font-size:13.5px;color:var(--ink);white-space:nowrap}
.gh:hover{border-color:var(--acc);border-bottom-color:var(--acc)}.gh svg{width:16px;height:16px;fill:currentColor}
.gh b{font-weight:500;color:var(--acc-ink);border-left:1px solid var(--rule);padding-left:9px;font-variant-numeric:tabular-nums}
h1{font-size:34px;line-height:1.15;margin:0;letter-spacing:-.01em}
.lede{font-size:18px;max-width:68ch;margin:0 0 22px;color:var(--ink)}
.stats{display:flex;flex-wrap:wrap;gap:0 36px;margin:0 0 48px;padding:16px 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.stats div{padding:4px 0}.stats b{display:block;font-size:22px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}.stats span{font-size:13px;color:var(--soft);text-transform:uppercase;letter-spacing:.06em}
section{margin:0 0 56px}
.eyebrow{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--acc);margin:0 0 6px;font-weight:600}
h2{font-size:24px;margin:0 0 10px;letter-spacing:-.01em;text-wrap:balance}
h3{font-size:16px;margin:26px 0 6px}
p{max-width:72ch;margin:8px 0}.soft{color:var(--soft)}
code{font:13.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--panel);padding:1px 5px;border-radius:4px}
pre.hero{font-size:17px;line-height:1.9;padding:22px 26px;border-left:3px solid var(--acc);margin:16px 0 18px}
.ln{display:inline-block;width:2.2em;color:var(--soft);user-select:none}
pre{font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--panel);padding:14px 16px;border-radius:6px;overflow-x:auto;margin:12px 0 0;max-width:100%}
.wrap{overflow-x:auto;margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 14px 6px 0;border-bottom:1px solid var(--rule);vertical-align:top;white-space:nowrap}
th{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--soft);font-weight:600}
td.n,th.n{text-align:right}td a{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
details{margin-top:12px}summary{cursor:pointer;color:var(--acc-ink)}
.cols{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--soft);max-width:none}
footer{border-top:1px solid var(--rule);padding-top:20px;font-size:14px;color:var(--soft)}footer p{max-width:none}
</style></head><body><main>
<div class="top"><h1>Open Jobs data</h1>
<a class="gh" href="${REPO}" target="_blank" rel="noopener" title="Open Jobs on GitHub"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg><span>Star on GitHub</span><b id="stars">★</b></a></div>
<p class="lede">Every job posting on about 65,000 company career sites, crawled every night, with full descriptions and
an embedding for each. Static files under CC0. No key, no account. Every URL supports CORS and HTTP Range, so DuckDB
and pandas read them in place.</p>
<div class="stats">
<div><b>${num(manifest.jobs)}</b><span>open postings</span></div>
<div><b>${num(atsList.length)}</b><span>applicant tracking systems</span></div>
<div><b>${esc(head)}</b><span>today's export</span></div>
<div><b>${when(diffs?.snapshot_built_at ?? manifest.built_at)}</b><span>built</span></div>
</div>

<section>
<p class="eyebrow">Start here</p>
<h2>Take all of today's data</h2>
<pre class="hero"><span class="ln">1</span>git clone ${REPO}
<span class="ln">2</span>cd open-jobs
<span class="ln">3</span>uv run tools/jobs.py export</pre>
<p>Three commands and the whole dataset is on your disk: every open posting, full description text, the
${esc(manifest.recipe)} embedding, and the company fields, as ${num(atsList.length)} parquet files (${gb(jobsBytes)}) under
<code>work/export/${esc(head)}/</code>. The download resumes if interrupted. Then
<code>uv run tools/jobs.py sql "SELECT title, company, location FROM jobs LIMIT 20"</code> queries it with DuckDB, with
<code>jobs</code> and <code>boards</code> as views.</p>
<p>That is the full export: one parquet file per applicant tracking system, ${num(atsList.length)} files, one row per open
posting with the description text and its ${esc(manifest.recipe)} embedding, plus <code>boards/</code> with one row per
career site and its company fields. The files live at <a href="/data/exports/${esc(head)}/jobs/">exports/${esc(head)}/jobs/</a>;
tomorrow's export replaces today's under tomorrow's date, and the previous day stays up for one more night.</p>
<p class="cols">ats, slug, id, title, location, url, departments[], published_at, updated_at, content, detail_status,
content_hash, first_seen_at, last_seen_at, changed_at, removed_at, is_open, enrich_status, enriched_at,
enrichment_json, embed_status, embed_model, embedding FLOAT[${esc(manifest.dims)}]</p>
<p class="soft">Too big? The <a href="#history">ledger</a> has every posting's dates and status without text or vectors
in ${gb(ledgerEntries[0]?.bytes)}. The <a href="#index">group files</a> have text plus vectors for open postings in
pieces of a few MB.</p>
<details><summary>Read it in place instead, or pick files</summary>
<pre>import duckdb
base = "${BASE}exports/${esc(head)}/jobs/"
ats = [${atsList.map((a) => `"${esc(a)}"`).join(", ")}]
jobs = duckdb.read_parquet([f"{base}{a}.parquet" for a in ats])
duckdb.sql("SELECT ats, count(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC")</pre>
<div class="wrap"><table><tr><th>jobs</th><th class="n">size</th><th>boards</th></tr>${jobRows}</table></div></details>
</section>

<section>
<p class="eyebrow">Then every night</p>
<h2>Keep it current</h2>
<p>Each night publishes one diff, <code>diffs/&lt;yesterday&gt;__&lt;today&gt;/</code>, with one row per event:
<code>op</code> is <code>added</code>, <code>removed</code>, <code>changed</code>, <code>changed_prev</code>
(the previous version of a changed row), or <code>carried</code>. Added and changed rows carry the posting's full
fields. <b>Lite</b> parts drop the embedding and raw JSON; <b>full</b> parts keep them. Every diff names its parent's
content hash, so a chain of diffs verifies. Apply the diff whose <code>from</code> equals the export you hold.</p>
${latestDiff ? `<p>Latest: <b>${esc(latestDiff.from)} → ${esc(latestDiff.to)}</b>, ${num(latestDiff.counts?.added)} added,
${num(latestDiff.counts?.removed)} removed, ${num(latestDiff.counts?.changed)} changed; ${gb(latestDiff.lite?.bytes)} lite,
${gb(latestDiff.bytes)} full. Removed rows say why: <code>closed</code> (the site dropped it), <code>left_dataset</code>,
or <code>unknown</code>.</p>` : ""}
<pre>import duckdb, json, urllib.request
idx = json.load(urllib.request.urlopen("${BASE}diffs/index.json"))
d = idx["entries"][-1]                       # the newest diff; idx["head"] is the export it produces
lite = [f"${BASE}{d['lite']['dir']}{p['file']}" for p in d["lite"]["parts"]]
ev = duckdb.read_parquet(lite)
duckdb.sql("SELECT op, count(*) FROM ev GROUP BY 1")</pre>
<h3>Or a change feed, if you keep a database</h3>
<p>The same changes as hashed pages of newline-delimited JSON upserts and removes, replayable in order, with a
generation id so a mirror knows when to re-bootstrap. Head: <a href="/data/changes/latest.json">changes/latest.json</a>
(${esc(feed?.kind)} ${esc(feed?.cursor)}, ${num(feedPages.length)} pages, ${gb(feedBytes)}, ${num(feed?.counts?.upsert)} upserts,
${num(feed?.counts?.remove)} removes). Protocol and a reference consumer: <a href="${REPO}/blob/main/backend/JOB-CHANGES.md">JOB-CHANGES.md</a>.</p>
</section>

<section id="history">
<p class="eyebrow">Looking back</p>
<h2>History</h2>
<h3>Ledger: every posting ever recorded</h3>
<p>One row per posting the crawler has ever seen, open or removed, as of that day, with no text and no vectors:
<code>ats, slug, id, title, location, url, published_at, content_hash, first_seen_at, last_seen_at, changed_at,
removed_at, is_open, detail_status, embed_status</code>. <code>first_seen_at</code> is the crawler's own first
sighting, which the job board cannot re-stamp; <code>removed_at</code> is when the site stopped listing it. This is
the file for posting lifetimes and survival curves. Newer days supersede older ones; read all parts of a day together.
Index: <a href="/data/ledger/index.json">ledger/index.json</a>.</p>
<div class="wrap"><table><tr><th>day</th><th class="n">size</th><th>parts</th></tr>${ledgerRows}</table></div>
<h3>Every diff since the start</h3>
<p>Kept indefinitely and never rewritten once listed. Index with counts, per-part sha256, and parent hashes:
<a href="/data/diffs/index.json">diffs/index.json</a>.</p>
<div class="wrap"><table><tr><th>diff</th><th class="n">added</th><th class="n">removed</th><th class="n">changed</th><th class="n">lite</th><th>parts</th><th class="n">full</th><th>parts</th><th>sidecar</th></tr>${diffRows}</table></div>
</section>

<section id="index">
<p class="eyebrow">What the search page reads</p>
<h2>The search index</h2>
<p>The current snapshot arranged for similarity search without a server: <a href="/data/manifest.json">manifest.json</a>
(${gb(manifestHead?.size)}) is a tree of ${num(manifest.nodes)} nodes over ${num(manifest.leaves)} groups of similar
postings; <a href="/data/centroids.bin">centroids.bin</a> holds the unit centroids as float16 in the tree's order, so a
client walks the tree with byte-range reads; <code>groups/&lt;id&gt;.json</code> is one group of postings with text,
fields, company, and exact float32 vectors. Estimators applied on the client: <a href="/data/age-model.json">age</a>,
<a href="/data/salary-model.json">salary</a>, <a href="/data/arrangement-model.json">arrangement</a>,
<a href="/data/seniority-model.json">seniority</a>, <a href="/data/location-countries.json">locations</a>.
These files are rewritten nightly under the same names. Layout and API: <a href="${REPO}/blob/main/backend/DOCS.md">DOCS.md</a>.</p>
</section>

<footer>
<p>Retention: ${esc(diffs?.retention)}</p>
<p>Fields and enrichment: <a href="${REPO}/blob/main/backend/FIELDS.md">FIELDS.md</a>. Search page: <a href="/">backend.dehnbostele.workers.dev</a>.
Source and issues: <a href="${REPO}">${REPO.replace("https://", "")}</a>. CC0 1.0.</p>
</footer>
</main>
<script>
(async () => {
  const el = document.getElementById("stars"), KEY = "open-jobs-stars", fmt = (n) => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n);
  try { const c = JSON.parse(localStorage.getItem(KEY) || "null"); if (c && Date.now() - c.at < 3600e3) { el.textContent = "★ " + fmt(c.n); return; } } catch {}
  try {
    const r = await fetch("https://api.github.com/repos/elliottdehn/open-jobs", { headers: { accept: "application/vnd.github+json" } });
    const n = (await r.json()).stargazers_count;
    if (typeof n === "number") { el.textContent = "★ " + fmt(n); try { localStorage.setItem(KEY, JSON.stringify({ n, at: Date.now() })); } catch {} }
  } catch {}
})();
</script>
</body></html>`;
	return new Response(html, { headers: { ...cors, "content-type": "text/html;charset=UTF-8", "cache-control": "public, max-age=3600" } });
}

// GET /data/  -> the human-readable index of the public files, ordered the way a reader uses them: take all of
// today's data, keep it current, then the history and the search index. Rendered from the bucket listing and the
// same index files a mirror reads (diffs/index.json, ledger/index.json, changes/latest.json, manifest.json), so it
// can never disagree with them. Cached an hour like everything else under /data/.

import { dataIndexStyle } from "./dataindex-style";

const REPO = "https://github.com/elliottdehn/open-jobs";
const BASE = "https://backend.dehnbostele.workers.dev/data/";

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);
const gb = (n?: number) => !n ? "" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${(n / 1e3).toFixed(0)} kB`;
const num = (x?: number) => x === undefined || x === null ? "" : x.toLocaleString("en-US");
const when = (ms?: number) => ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "";
const parts = (dir: string, ps?: { file: string }[]) => (ps ?? []).map((p) => `<a class="part-link" href="/data/${esc(dir)}${esc(p.file)}">${esc(p.file)}</a>`).join("");

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
		for (const k of ["recipe", "dims", "jobs", "nodes", "leaves", "groups", "built_at"]) {
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

	const pills = jobs.map((f) => `<a class="dl" href="/data/exports/${esc(head)}/jobs/${esc(f.name)}" download><b>${esc(f.name.replace(/\.parquet$/, ""))}</b><span>${gb(f.size)}</span></a>`).join("");
	const jobRows = jobs.map((f) => `<tr><td><a href="/data/exports/${esc(head)}/jobs/${esc(f.name)}" download>${esc(f.name)}</a></td><td class="n">${gb(f.size)}</td>` +
		`<td><a href="/data/exports/${esc(head)}/boards/${esc(f.name)}" download>boards/${esc(f.name)}</a></td></tr>`).join("");
	const diffRows = diffEntries.map((e) =>
		`<tr><td class="diff-date">${esc(e.to)}<small>from ${esc(e.from)}</small></td><td class="n added">${num(e.counts?.added)}</td><td class="n removed">${num(e.counts?.removed)}</td><td class="n">${num(e.counts?.changed)}</td>` +
		`<td><span class="file-size">${gb(e.lite?.bytes)}</span>${parts(e.lite?.dir ?? "", e.lite?.parts)}</td><td><span class="file-size">${gb(e.bytes)}</span>${parts(e.dir, e.parts)}</td>` +
		`<td><a href="/data/${esc(e.sidecar)}">json</a></td></tr>`).join("");
	const ledgerRows = ledgerEntries.map((e) => `<tr><td>${esc(e.date)}</td><td class="n">${gb(e.bytes)}</td><td>${parts(e.dir, e.parts)}</td></tr>`).join("");

	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Public data · Open Jobs</title>
<meta name="description" content="Download the Open Jobs dataset. Full exports, daily changes, posting history, and the search index. Public files under CC0.">
<meta name="theme-color" content="#142a20">
<style>${dataIndexStyle}</style></head><body>
<a class="skip" href="#main">Skip to the data</a>
<header class="masthead"><div class="shell">
<div class="top"><a class="brand" href="/" aria-label="Open Jobs home"><svg viewBox="0 0 26 30" aria-hidden="true"><path d="M2 28V13a11 11 0 0 1 22 0v15H2Z"/><path d="M7 28V13a6 6 0 0 1 12 0v15"/><path d="M15 20h1"/></svg><span>Open <em>Jobs</em></span></a><nav class="header-links" aria-label="Main navigation"><a class="search-link" href="/">Find a job</a>
<a class="gh" href="${REPO}" target="_blank" rel="noopener" title="Open Jobs on GitHub"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg><span>GitHub</span><b id="stars">★</b></a></nav></div>
<div class="hero-head"><div><div class="path">/data/</div><h1>Public data.</h1></div><div>
<p class="lede">Job postings from about 65,000 company career sites. Full descriptions, embeddings, and a record of what changes each day.</p>
<div class="access"><span>CC0 1.0</span><span>No account</span><span>No API key</span></div>
</div></div>
<div class="stats">
<div><b>${num(manifest.jobs)}</b><span>postings in the search index</span></div>
<div><b>${num(atsList.length)}</b><span>ATS export files</span></div>
<div><b class="date-stat">${esc(head) || "Not published"}</b><span>latest export</span></div>
<div><b class="built-stat">${when(diffs?.snapshot_built_at ?? manifest.built_at) || "Not published"}</b><span>index built</span></div>
</div>
</div></header>
<div class="shell"><div class="layout">
<nav class="contents" aria-label="On this page"><div class="contents-label">The files</div>
<a href="#download"><span>01</span>Download</a><a href="#changes"><span>02</span>Daily changes</a><a href="#history"><span>03</span>History</a><a href="#index"><span>04</span>Search index</a>
<div class="technical-note">Parquet · JSON · NDJSON<br>CORS + HTTP Range</div></nav>
<main id="main" tabindex="-1">
<section id="download">
<p class="eyebrow">01 / Start here</p>
<h2>Take a copy.</h2>
<pre class="hero"><span class="ln">1</span>git clone ${REPO}
<span class="ln">2</span>cd open-jobs
<span class="ln">3</span>uv run tools/jobs.py export</pre>
<p class="soft">Needs <a href="https://docs.astral.sh/uv/">uv</a>, one line to install: <code>curl -LsSf https://astral.sh/uv/install.sh | sh</code> (or <code>brew install uv</code>; on Windows <code>winget install astral-sh.uv</code>).</p>
<div class="export-note"><span><b>${gb(jobsBytes) || "Not published"}</b> · ${num(atsList.length)} parquet files</span><span>Resumes interrupted downloads</span></div>
<p>The full export puts every open posting on your disk, with description text, available vectors, and
company fields. One jobs file per applicant tracking system, plus <code>boards/</code> for the company records.</p>
<p>The command saves to <code>work/export/${esc(head)}/</code> and makes <code>jobs</code> and <code>boards</code>
available as DuckDB views. The files also live at <a href="/data/exports/${esc(head)}/jobs/">exports/${esc(head)}/jobs/</a>.
Each new export gets its own date; the previous day stays up for one more night.</p>
<h3>Or click, one file at a time</h3>
<p class="soft">Each file is every open posting on one applicant tracking system, as of ${esc(head)}, parquet, no account. Company records for each are in the table below.</p>
<div class="dls">${pills || '<span class="soft">Not published yet.</span>'}</div>
<h3>A few things to ask it</h3>
<p class="soft">Each is one command. <code>jobs</code> and <code>boards</code> are views over the export you just pulled.</p>
<div class="inc">
<details open><summary>Count what landed</summary>
<pre>uv run tools/jobs.py sql "SELECT count(*) postings, count(DISTINCT slug) career_sites, count(*) FILTER (embedding IS NOT NULL) with_vectors FROM jobs"</pre></details>
<details><summary>Ads still posted as open a year after the date they show</summary>
<pre>uv run tools/jobs.py sql "SELECT title, url, published_at::date AS says_posted FROM jobs WHERE published_at &lt; now() - INTERVAL 1 YEAR ORDER BY 3 LIMIT 20"</pre></details>
<details><summary>What the boards claim, by month</summary>
<pre>uv run tools/jobs.py sql "SELECT date_trunc('month', published_at)::date AS month, count(*) FROM jobs GROUP BY 1 ORDER BY 1 DESC LIMIT 12"</pre></details>
<details><summary>The most common titles</summary>
<pre>uv run tools/jobs.py sql "SELECT title, count(*) n FROM jobs GROUP BY 1 ORDER BY 2 DESC LIMIT 25"</pre></details>
<details><summary>Career sites with the most open roles</summary>
<pre>uv run tools/jobs.py sql "SELECT ats, slug, count(*) open_roles FROM jobs GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25"</pre></details>
<details><summary>How many postings state a dollar figure</summary>
<pre>uv run tools/jobs.py sql "SELECT ats, round(100.0 * count(*) FILTER (regexp_matches(content, '[$][0-9]{2,3},[0-9]{3}')) / count(*), 1) pct_with_pay FROM jobs GROUP BY 1 ORDER BY 2 DESC"</pre></details>
<details><summary>Full-text search across every description</summary>
<pre>uv run tools/jobs.py sql "SELECT title, url FROM jobs WHERE content ILIKE '%kubernetes%' AND title ILIKE '%engineer%' LIMIT 20"</pre></details>
<details><summary>Nearest postings by vector, no index needed</summary>
<pre>uv run tools/jobs.py sql "WITH q AS (SELECT embedding FROM jobs WHERE title ILIKE '%data engineer%' AND embedding IS NOT NULL LIMIT 1) SELECT title, round(list_cosine_similarity(embedding, (SELECT embedding FROM q)), 3) sim FROM jobs WHERE embedding IS NOT NULL ORDER BY 2 DESC LIMIT 20"</pre></details>
</div>
<h3>Inside each jobs file</h3>
<p class="soft">Embedding recipe: <code>${esc(manifest.recipe) || "Not published"}</code>.</p>
<p class="cols">ats, slug, id, title, location, url, departments[], published_at, updated_at, content, detail_status,
content_hash, first_seen_at, last_seen_at, changed_at, removed_at, is_open, enrich_status, enriched_at,
enrichment_json, embed_status, embed_model, embedding FLOAT[${esc(manifest.dims)}]</p>
<p class="soft">Too big? The <a href="#history">ledger</a> has every posting's dates and status without text or vectors
in ${gb(ledgerEntries[0]?.bytes)}. The <a href="#index">group files</a> have text plus vectors for open postings in
pieces of a few MB.</p>
<details><summary>Read remotely, or the company records per ATS</summary>
<p class="soft" style="padding-inline:20px">Every file supports CORS and HTTP Range. DuckDB and pandas can read the URLs directly.</p>
<pre>import duckdb
base = "${BASE}exports/${esc(head)}/jobs/"
ats = [${atsList.map((a) => `"${esc(a)}"`).join(", ")}]
jobs = duckdb.read_parquet([f"{base}{a}.parquet" for a in ats])
duckdb.sql("SELECT ats, count(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC")</pre>
<div class="wrap" tabindex="0" role="region" aria-label="Export files"><table><thead><tr><th scope="col">Jobs</th><th scope="col" class="n">Size</th><th scope="col">Boards</th></tr></thead><tbody>${jobRows || '<tr><td colspan="3" class="empty">No export files published yet.</td></tr>'}</tbody></table></div></details>
</section>

<section id="changes">
<p class="eyebrow">02 / Daily changes</p>
<h2>Keep it current.</h2>
<p>Each night publishes one diff, <code>diffs/&lt;yesterday&gt;__&lt;today&gt;/</code>, with one row per event:
<code>op</code> is <code>added</code>, <code>removed</code>, <code>changed</code>, <code>changed_prev</code>
(the previous version of a changed row), or <code>carried</code>. Added and changed rows carry the posting's full
fields. <b>Lite</b> parts drop the embedding and raw JSON; <b>full</b> parts keep them. Every diff names its parent's
content hash, so a chain of diffs verifies. Apply the diff whose <code>from</code> equals the export you hold.</p>
${latestDiff ? `<div class="latest"><div class="period"><span>Latest diff</span><span>${esc(latestDiff.from)} → ${esc(latestDiff.to)}</span></div>
<div class="delta-counts"><div class="added"><b>${num(latestDiff.counts?.added)}</b><span>added</span></div><div><b>${num(latestDiff.counts?.removed)}</b><span>removed</span></div><div><b>${num(latestDiff.counts?.changed)}</b><span>changed</span></div><div><b>${gb(latestDiff.lite?.bytes)}</b><span>lite</span></div><div><b>${gb(latestDiff.bytes)}</b><span>full</span></div></div></div>` : '<p class="soft">No daily diffs published yet.</p>'}
<p class="soft">Removed rows say why: <code>closed</code> (the site dropped it), <code>left_dataset</code>, or <code>unknown</code>.</p>
<pre>import duckdb, json, urllib.request
idx = json.load(urllib.request.urlopen("${BASE}diffs/index.json"))
d = idx["entries"][-1]                       # the newest diff; idx["head"] is the export it produces
lite = [f"${BASE}{d['lite']['dir']}{p['file']}" for p in d["lite"]["parts"]]
ev = duckdb.read_parquet(lite)
duckdb.sql("SELECT op, count(*) FROM ev GROUP BY 1")</pre>
<h3>For a database, use the change feed</h3>
<p>The same changes as hashed pages of newline-delimited JSON: upserts and removes, replayed in order.
A generation ID records your place. The <a href="${REPO}/blob/main/backend/JOB-CHANGES.md">protocol and reference consumer</a>
cover verification, checkpoints, and recovery.</p>
${feed ? `<div class="feed-head"><a href="/data/changes/latest.json">changes/latest.json ↗</a>
<div class="soft">${esc(feed.kind)} · ${esc(feed.cursor)} · ${num(feedPages.length)} pages · ${gb(feedBytes) || "0 kB"}<br>${num(feed.counts?.upsert)} upserts · ${num(feed.counts?.remove)} removes</div></div>` : '<p class="soft">The change feed has not been published yet.</p>'}
</section>

<section id="history">
<p class="eyebrow">03 / Looking back</p>
<h2>The record over time.</h2>
<h3>Ledger: every posting ever recorded</h3>
<p>One row per posting the crawler has ever seen, open or removed, as of that day, with no text and no vectors:
<code>ats, slug, id, title, location, url, published_at, content_hash, first_seen_at, last_seen_at, changed_at,
removed_at, is_open, detail_status, embed_status</code>. <code>first_seen_at</code> is the crawler's own first
sighting, which the job board cannot re-stamp; <code>removed_at</code> is when the site stopped listing it. This is
the file for posting lifetimes and survival curves. Newer days supersede older ones; read all parts of a day together.
Index: <a href="/data/ledger/index.json">ledger/index.json</a>.</p>
<div class="wrap" tabindex="0" role="region" aria-label="Ledger downloads"><table><thead><tr><th scope="col">Day</th><th scope="col" class="n">Size</th><th scope="col">Parts</th></tr></thead><tbody>${ledgerRows || '<tr><td colspan="3" class="empty">No ledger days published yet.</td></tr>'}</tbody></table></div>
<h3>Every diff since the start</h3>
<p>Kept indefinitely and never rewritten once listed. Index with counts, per-part sha256, and parent hashes:
<a href="/data/diffs/index.json">diffs/index.json</a>.</p>
<div class="wrap" tabindex="0" role="region" aria-label="Daily diff downloads"><table><thead><tr><th scope="col">To / from</th><th scope="col" class="n">Added</th><th scope="col" class="n">Removed</th><th scope="col" class="n">Changed</th><th scope="col">Lite</th><th scope="col">Full</th><th scope="col">Sidecar</th></tr></thead><tbody>${diffRows || '<tr><td colspan="7" class="empty">No daily diffs published yet.</td></tr>'}</tbody></table></div>
</section>

<section id="index">
<p class="eyebrow">04 / Similarity search</p>
<h2>The search index.</h2>
<p>The current snapshot, arranged for similarity search on your own machine.
Download a few groups of related postings, or walk the tree with byte-range reads.</p>
<div class="file-index">
<div class="file-entry"><a href="/data/manifest.json">manifest.json ↗</a><p>${gb(manifestHead?.size) || "Not published"} · A tree of ${num(manifest.nodes)} nodes across ${num(manifest.leaves)} groups.</p></div>
<div class="file-entry"><a href="/data/centroids.bin">centroids.bin ↗</a><p>Unit centroids as float16, in tree order. Read only the ranges you need.</p></div>
<div class="file-entry"><code>${esc(manifest.groups ?? "groups/")}&lt;id&gt;.json</code><p>One group of postings with text, fields, company data, and exact float32 vectors. A few MB per file.</p></div>
</div>
<h3>Client estimators</h3>
<div class="estimators"><a href="/data/age-model.json">Age</a><a href="/data/salary-model.json">Salary</a><a href="/data/arrangement-model.json">Arrangement</a><a href="/data/seniority-model.json">Seniority</a><a href="/data/location-countries.json">Locations</a></div>
<p class="soft">These files are rewritten nightly under the same names. <a href="${REPO}/blob/main/backend/DOCS.md">Layout and API documentation ↗</a></p>
</section>

</main></div>
<footer><div class="footer-line"><span>Open Jobs · CC0 1.0</span><div class="footer-links"><a href="${REPO}/blob/main/backend/FIELDS.md">Fields &amp; enrichment</a><a href="/">Find a job</a><a href="${REPO}">Source &amp; issues ↗</a></div></div>
<p>Retention: ${esc(diffs?.retention) || "See the published indexes for retention details."}</p>
</footer></div><div id="copy-status" class="sr-only" role="status"></div>
<script>
(() => {
  const status = document.getElementById('copy-status');
  document.querySelectorAll('.code-panel').forEach(panel => {
    const pre = panel.querySelector('pre');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'copy'; button.textContent = 'Copy';
    button.setAttribute('aria-label', 'Copy command');
    panel.querySelector('.code-head').append(button);
    button.addEventListener('click', async () => {
      const clone = pre.cloneNode(true);
      clone.querySelectorAll('.ln').forEach(n => n.remove());
      const text = clone.textContent;
      try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
        else {
          const area = document.createElement('textarea'); area.value = text;
          area.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.append(area); area.select();
          const copied = document.execCommand('copy'); area.remove(); button.focus();
          if (!copied) throw new Error('Clipboard unavailable');
        }
        button.textContent = 'Copied'; status.textContent = 'Command copied to clipboard.';
      } catch {
        const range = document.createRange(); range.selectNodeContents(pre);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        button.textContent = 'Select & copy'; status.textContent = 'Use your copy shortcut to copy the selected command.';
      }
      setTimeout(() => { button.textContent = 'Copy'; }, 2000);
    });
  });
  const sections = [...document.querySelectorAll('main > section')];
  const links = [...document.querySelectorAll('.contents a')];
  let queued = false;
  function track() {
    let current = sections[0];
    for (const section of sections) { if (section.getBoundingClientRect().top < 160) current = section; }
    links.forEach(link => {
      if (link.hash === '#' + current.id) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    queued = false;
  }
  addEventListener('scroll', () => { if (!queued) { queued = true; requestAnimationFrame(track); } }, {passive:true});
  track();
})();
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
	const page = html.replace(/<pre([^>]*)>([\s\S]*?)<\/pre>/g, (_match, attrs: string, content: string) =>
		`<div class="code-panel"><div class="code-head"><span>${content.startsWith("import ") ? "Python" : "Terminal"}</span></div><pre${attrs} tabindex="0">${content}</pre></div>`);
	return new Response(page, { headers: { ...cors, "content-type": "text/html;charset=UTF-8", "cache-control": "public, max-age=3600" } });
}

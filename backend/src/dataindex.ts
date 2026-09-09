// GET /data/  -> a plain HTML index of the public files: what is there, how big, when it was built, and how to read it.
// Rendered from the same indexes a mirror uses (ledger/index.json, diffs/index.json, changes/latest.json, manifest.json),
// so it can never disagree with them. Cached an hour like everything else under /data/.

const REPO = "https://github.com/elliottdehn/open-jobs";

function esc(s: unknown): string {
	return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);
}
function gb(n: number | undefined): string {
	if (!n) return "";
	return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${(n / 1e3).toFixed(0)} kB`;
}
function day(ms: number | undefined): string {
	return ms ? new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "";
}
function n(x: number | undefined): string {
	return x === undefined ? "" : x.toLocaleString("en-US");
}
function links(dir: string, parts: { file: string }[] | undefined): string {
	return (parts ?? []).map((p) => `<a href="/data/${esc(dir)}${esc(p.file)}">${esc(p.file)}</a>`).join(" ");
}

async function json(env: { DATA: R2Bucket }, key: string): Promise<any | null> {
	const o = await env.DATA.get(key);
	return o ? o.json() : null;
}

export async function dataIndex(env: { DATA: R2Bucket }, cors: Record<string, string>): Promise<Response> {
	const [ledger, diffs, feed, manifestHead] = await Promise.all([
		json(env, "ledger/index.json"),
		json(env, "diffs/index.json"),
		json(env, "changes/latest.json"),
		env.DATA.head("manifest.json"),
	]);
	// the manifest is 20+ MB; read only its small header fields from the first bytes
	let manifest: any = {};
	if (manifestHead) {
		const head = await env.DATA.get("manifest.json", { range: { offset: 0, length: 400 } });
		const text = head ? await head.text() : "";
		for (const k of ["recipe", "jobs", "nodes", "leaves", "built_at"]) {
			const m = text.match(new RegExp(`"${k}"\\s*:\\s*("[^"]*"|\\d+)`));
			if (m) manifest[k] = m[1].startsWith('"') ? m[1].slice(1, -1) : Number(m[1]);
		}
	}
	const ledgerRows = ((ledger?.entries ?? []) as any[]).slice().reverse().map((e) =>
		`<tr><td>${esc(e.date)}</td><td class="num">${gb(e.bytes)}</td><td>${links(e.dir, e.parts)}</td></tr>`).join("");
	const diffRows = ((diffs?.entries ?? []) as any[]).slice().reverse().map((e) =>
		`<tr><td>${esc(e.from)} → ${esc(e.to)}</td><td class="num">${n(e.counts?.added)}</td><td class="num">${n(e.counts?.removed)}</td><td class="num">${n(e.counts?.changed)}</td>` +
		`<td class="num">${gb(e.bytes)}</td><td>${links(e.dir, e.parts)}</td><td class="num">${gb(e.lite?.bytes)}</td><td>${links(e.lite?.dir ?? "", e.lite?.parts)}</td>` +
		`<td><a href="/data/${esc(e.sidecar)}">json</a></td></tr>`).join("");
	const feedPages = (feed?.pages ?? []) as any[];
	const feedBytes = feedPages.reduce((a, p) => a + (p.bytes ?? 0), 0);
	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open Jobs data</title>
<style>
body{margin:0;padding:32px 24px 64px;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1c1c1c;background:#fbfaf7;max-width:1100px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 8px}p{margin:6px 0;max-width:72ch}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 10px 5px 0;border-bottom:1px solid #e4e1d9;vertical-align:top}th{font-weight:600;color:#555}
td.num,th.num{text-align:right;white-space:nowrap}
a{color:#1a5fb4}code{background:#efece4;padding:1px 4px;border-radius:3px;font-size:13px}
pre{background:#efece4;padding:10px 12px;border-radius:4px;overflow-x:auto;font-size:13px;line-height:1.4}
.wrap{overflow-x:auto}.muted{color:#666}
@media (prefers-color-scheme:dark){body{color:#e6e3dc;background:#16171a}th{color:#aaa}th,td{border-color:#2e3036}a{color:#8ab4f8}code,pre{background:#23252b}.muted{color:#999}}
</style></head><body>
<h1>Open Jobs data</h1>
<p>Every file the <a href="${REPO}">Open Jobs</a> crawler publishes: about ${n(manifest.jobs)} current job postings from
tens of thousands of company career sites, rebuilt nightly, with full descriptions and embeddings, plus the history
behind them. Static files, CC0, no key, no account. Every URL below supports CORS and HTTP Range, so DuckDB and
pandas read them in place. Field-level documentation: <a href="${REPO}/blob/main/backend/FIELDS.md">FIELDS.md</a>;
layout and API: <a href="${REPO}/blob/main/backend/DOCS.md">DOCS.md</a>.</p>

<h2>Ledger <span class="muted">— every posting ever recorded, one row each</span></h2>
<p><code>ats, slug, id, title, location, url, published_at, content_hash, first_seen_at, last_seen_at, changed_at, removed_at,
is_open, detail_status, embed_status</code>. No text, no vectors. Read all parts of a day together; newer days supersede
older ones. Index: <a href="/data/ledger/index.json">ledger/index.json</a>.</p>
<div class="wrap"><table><tr><th>day</th><th class="num">size</th><th>parts</th></tr>${ledgerRows}</table></div>

<h2>Daily diffs <span class="muted">— what changed between two consecutive nightly exports</span></h2>
<p>One row per event: <code>op</code> = added, removed, changed, changed_prev (the previous version), carried; the job's
fields ride along, including the description on added and changed rows. Full parts carry the embedding; lite parts drop
it and the raw JSON. Each diff names its parent's hash, so the chain verifies. Head export: <b>${esc(diffs?.head)}</b>,
built ${day(diffs?.snapshot_built_at)}. Index: <a href="/data/diffs/index.json">diffs/index.json</a>.</p>
<div class="wrap"><table><tr><th>diff</th><th class="num">added</th><th class="num">removed</th><th class="num">changed</th><th class="num">full</th><th>parts</th><th class="num">lite</th><th>parts</th><th>sidecar</th></tr>${diffRows}</table></div>

<h2>Current snapshot <span class="muted">— the search index</span></h2>
<p><a href="/data/manifest.json">manifest.json</a> (${gb(manifestHead?.size)}, built ${day(manifest.built_at)}): ${n(manifest.jobs)} jobs
in ${n(manifest.leaves)} groups of similar postings, a tree of ${n(manifest.nodes)} nodes over ${esc(manifest.recipe)} embeddings.
Each group is one JSON file at <code>/data/groups/&lt;id&gt;.json</code> with the postings' text, fields, and float32 vectors;
<code>/data/centroids.bin</code> holds the tree's centroids for byte-range search. The manifest and the group files are
rewritten nightly under the same names.</p>

<h2>Change feed <span class="muted">— for mirrors that want a database, not files</span></h2>
<p>Hashed newline-delimited JSON pages of upserts and removes, replayable in order. Head: <a href="/data/changes/latest.json">changes/latest.json</a>
(${esc(feed?.kind)} ${esc(feed?.cursor)}, ${n(feedPages.length)} pages, ${gb(feedBytes)}, ${n(feed?.counts?.upsert)} upserts,
${n(feed?.counts?.remove)} removes). Protocol: <a href="${REPO}/blob/main/backend/JOB-CHANGES.md">JOB-CHANGES.md</a>.</p>

<h2>Reading it</h2>
<pre>import duckdb
led = duckdb.read_parquet("https://backend.dehnbostele.workers.dev/data/ledger/${esc((ledger?.entries ?? []).at(-1)?.date ?? "<day>")}/data_*.parquet")
duckdb.sql("SELECT count(*) FILTER (is_open) AS open, count(*) FILTER (NOT is_open) AS removed FROM led")</pre>
<p class="muted">Retention: ${esc(diffs?.retention)}</p>
<p class="muted">Search page: <a href="/">backend.dehnbostele.workers.dev</a>. Source and issues: <a href="${REPO}">${REPO.replace("https://", "")}</a>.</p>
</body></html>`;
	return new Response(html, { headers: { ...cors, "content-type": "text/html;charset=UTF-8", "cache-control": "public, max-age=3600" } });
}

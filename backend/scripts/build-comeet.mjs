// Resolves comeet company slugs -> career-page uid ("B4.007") once, offline, via the Common Crawl
// index (same crawl slugs.json came from), and writes src/comeet-uids.json.
// Re-run whenever slugs.json changes: node scripts/build-comeet.mjs [--refresh] [--via=https://worker-url]
//   --via routes lookups through the deployed worker's /comeet/resolve/:slug (Cloudflare's edge can reach
//   the Common Crawl index / DDG when this machine can't).
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = JSON.parse(readFileSync(join(here, "../../slugs.json"), "utf8"));
const CC_INDEX = src.crawl ?? "https://index.commoncrawl.org/CC-MAIN-2026-21-index";
const outPath = join(here, "../src/comeet-uids.json");
const refresh = process.argv.includes("--refresh");
const via = process.argv.find((a) => a.startsWith("--via="))?.slice(6) ?? process.env.WORKER_URL;
const map = !refresh && existsSync(outPath) ? JSON.parse(readFileSync(outPath, "utf8")) : {};
const slugs = src.ats.comeet.filter((s) => !(s in map));
const UID_RE = /comeet\.com\/jobs\/([^/"'&?\s]+)\/([0-9A-Za-z]{2}\.[0-9A-Za-z]{3})/g;

function findUid(text, slug) {
	const want = slug.toLowerCase();
	for (const m of text.matchAll(UID_RE)) if (m[1].toLowerCase() === want) return { name: m[1], uid: m[2].toUpperCase() };
	return null;
}

async function withRetry(fn, tries = 4) {
	for (let i = 0; ; i++) {
		try {
			return await fn();
		} catch (e) {
			if (i >= tries - 1) throw e;
			await new Promise((r) => setTimeout(r, 1000 * 2 ** i));
		}
	}
}

async function resolve(slug) {
	if (via) {
		return withRetry(async () => {
			const res = await fetch(`${via}/comeet/resolve/${encodeURIComponent(slug)}`, {
				headers: { "user-agent": "open-jobs-cli", ...(process.env.ADMIN_TOKEN ? { authorization: `Bearer ${process.env.ADMIN_TOKEN}` } : {}) },
			});
			if (!res.ok) throw new Error(`worker ${res.status}`);
			return res.json();
		});
	}
	const cc = await withRetry(async () => {
		const url = `${CC_INDEX}?url=${encodeURIComponent(`comeet.com/jobs/${slug}/*`)}&output=json&limit=20&fl=url`;
		const res = await fetch(url, { headers: { "user-agent": "open-jobs/0.1" } });
		if (res.status === 404) return ""; // "No Captures found"
		if (res.status === 503 || res.status === 429) throw new Error(`CC ${res.status}`);
		return res.text();
	});
	const found = findUid(cc, slug);
	if (found) return found;
	const ddg = await fetch(`https://html.duckduckgo.com/html/?q=${encodeURIComponent(`site:comeet.com/jobs/${slug}`)}`, {
		headers: { "user-agent": "Mozilla/5.0 open-jobs/0.1" },
	}).then((r) => (r.ok ? r.text() : ""), () => "");
	return findUid(decodeURIComponent(ddg), slug);
}

let done = 0, hit = 0;
const CONC = 4;
await Promise.all(Array.from({ length: CONC }, async () => {
	for (;;) {
		const slug = slugs.shift();
		if (!slug) return;
		try {
			const r = await resolve(slug);
			map[slug] = r; // null = unresolved (kept so we don't retry every build)
			if (r) hit++;
		} catch (e) {
			process.stderr.write(`\n${slug}: ${e.message}\n`);
		}
		done++;
		process.stderr.write(`\r${done} resolved, ${hit} found`);
	}
}));
const sorted = Object.fromEntries(Object.entries(map).sort(([a], [b]) => a.localeCompare(b)));
writeFileSync(outPath, JSON.stringify(sorted, null, 1));
const total = Object.values(sorted).filter(Boolean).length;
process.stderr.write(`\nwrote ${outPath}: ${total}/${Object.keys(sorted).length} slugs resolved\n`);

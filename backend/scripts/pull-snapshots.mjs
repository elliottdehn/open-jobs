// Download per-board R2 parquet snapshots (written by the Board DOs; see src/snapshot.ts) for one or
// more ATSes into <out>/snapshots/<ats>/. This replaces the /export JSON pull for consolidation:
// static R2 objects, no DO wake, vectors already binary — the whole corpus in minutes.
// Usage: node scripts/pull-snapshots.mjs [worker-url] [--out=DIR] [--ats=a,b,...] [--exclude=a,b] [--concurrency=N]
//   env ADMIN_TOKEN for the /snapshots listing endpoint; objects themselves come via public /data/.
// Listing and objects are keyed snapshots/{ats}/{encodeURIComponent(slug)}.parquet; local files keep
// the encoded basename (filesystem-safe). Boards with no open jobs have no object, so a fresh --out
// per day naturally contains only live boards.
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";

const argv = process.argv.slice(2);
const flags = Object.fromEntries(argv.filter((a) => a.startsWith("--")).map((a) => { const [k, v] = a.slice(2).split("="); return [k, v ?? "1"]; }));
const base = argv.find((a) => !a.startsWith("--")) ?? process.env.WORKER_URL ?? "https://backend.dehnbostele.workers.dev";
const outDir = flags.out || "export";
const conc = Math.max(1, Number(flags.concurrency || 24));
const headers = process.env.ADMIN_TOKEN ? { authorization: `Bearer ${process.env.ADMIN_TOKEN}` } : {};

// default: every ATS in boards.json (snapshots exist only where boards wrote them; empty lists are fine)
const excluded = new Set((flags.exclude ?? "").split(",").filter(Boolean));
const atses = (flags.ats
	? flags.ats.split(",").filter(Boolean)
	: Object.keys(JSON.parse(readFileSync(new URL("../src/boards.json", import.meta.url), "utf8")))
).filter((a) => !excluded.has(a));

async function listSnapshots(ats) {
	const objects = [];
	let cursor = null;
	do {
		const qs = new URLSearchParams({ ats });
		if (cursor) qs.set("cursor", cursor);
		const res = await fetch(`${base}/snapshots?${qs}`, { headers });
		if (!res.ok) throw new Error(`list ${ats}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
		const d = await res.json();
		objects.push(...d.objects);
		cursor = d.cursor;
	} while (cursor);
	return objects;
}

async function download(key, dest, etag) {
	// skip when we already have this exact object (etag recorded beside the file)
	const tag = `${dest}.etag`;
	if (existsSync(dest) && existsSync(tag) && readFileSync(tag, "utf8") === etag) return false;
	for (let attempt = 0; ; attempt++) {
		try {
			// keys store slugs encodeURIComponent-ed; the URL path decodes once, so encode each
			// segment again or a slug containing "/" (stored as %2F) resolves to the wrong key
			const res = await fetch(`${base}/data/${key.split("/").map(encodeURIComponent).join("/")}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
			writeFileSync(tag, etag);
			return true;
		} catch (e) {
			if (attempt >= 3) throw new Error(`${key}: ${e.message}`);
			await new Promise((r) => setTimeout(r, 2000 * 2 ** attempt));
		}
	}
}

let totalObjects = 0, totalBytes = 0, downloaded = 0, failed = 0;
for (const ats of atses) {
	const objects = await listSnapshots(ats);
	if (!objects.length) { process.stderr.write(`${ats}: no snapshots\n`); continue; }
	const dir = `${outDir}/snapshots/${ats}`;
	mkdirSync(dir, { recursive: true });
	let done = 0, got = 0;
	const queue = [...objects];
	await Promise.all(Array.from({ length: Math.min(conc, queue.length) }, async () => {
		for (;;) {
			const o = queue.shift();
			if (!o) return;
			const dest = `${dir}/${o.key.split("/").pop()}`;
			try {
				if (await download(o.key, dest, o.etag)) got++;
			} catch (e) {
				failed++; process.stderr.write(`\nSKIP ${e.message}\n`);
			}
			done++;
			if (done % 200 === 0 || done === objects.length) process.stderr.write(`\r${ats}: ${done}/${objects.length} (${got} fetched)`);
		}
	}));
	totalObjects += objects.length; downloaded += got;
	totalBytes += objects.reduce((a, o) => a + o.size, 0);
	process.stderr.write(`\r${ats}: ${objects.length} snapshots, ${got} fetched\n`);
}
console.log(`snapshots: ${totalObjects} boards, ${(totalBytes / 1e9).toFixed(2)} GB listed, ${downloaded} downloaded${failed ? `, ${failed} FAILED (those boards ride yesterday's data or the fallback)` : ""} -> ${outDir}/snapshots/`);

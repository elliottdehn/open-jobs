// Pull every board for an ATS from the deployed worker into export/<ats>.ndjson.
// Usage: node scripts/export.mjs <ats> [worker-url] [--status=open|removed|all] [--enrich=pending|done|error]
//                                [--since=<epoch ms>] [--slim] [--skip-empty] [--embed] [--out=DIR] [--resume]
//   --resume keeps the complete pages already in <out>/<ats>.ndjson and continues from the next page.
//   --embed includes each job's 1536-float embedding (large: ~3 KB/job in JSON).
//   env ADMIN_TOKEN=... if the worker has one configured; env WORKER_URL as default base.
//   e.g. node scripts/export.mjs greenhouse https://x.workers.dev --status=open --skip-empty
import { mkdirSync, createWriteStream, createReadStream, existsSync, readFileSync, writeFileSync, truncateSync } from "node:fs";
import { createInterface } from "node:readline";

const argv = process.argv.slice(2);
const flags = Object.fromEntries(argv.filter((a) => a.startsWith("--")).map((a) => {
	const [k, v] = a.slice(2).split("=");
	return [k, v ?? "1"];
}));
const [ats = "greenhouse", base = process.env.WORKER_URL ?? "http://localhost:8787"] = argv.filter((a) => !a.startsWith("--"));
const headers = process.env.ADMIN_TOKEN ? { authorization: `Bearer ${process.env.ADMIN_TOKEN}` } : {};
const outDir = flags.out || "export";
mkdirSync(outDir, { recursive: true });
const pageSize = flags.embed ? 5 : 200;
const file = `${outDir}/${ats}.ndjson`;

let offset = 0;
let boards = 0;
let jobs = 0;
let errors = 0;
if (flags.resume && existsSync(file)) {
	// Keep whole pages only. Stream the file (it can be many GB), tracking the byte offset of each line, and
	// truncate after the last complete page (page size recorded in <file>.page by the run that wrote it).
	const prevPage = Number((existsSync(`${file}.page`) ? readFileSync(`${file}.page`, "utf8") : "").trim() || pageSize);
	const rl = createInterface({ input: createReadStream(file), crlfDelay: Infinity });
	const seen = new Set(); let bytes = 0; let keepBytes = 0; let lastBoundary = 0; let lineNo = 0;
	for await (const l of rl) {
		bytes += Buffer.byteLength(l, "utf8") + 1; lineNo++;
		const k = l.indexOf('"slug":"'); if (k >= 0) seen.add(l.slice(k + 8, l.indexOf('"', k + 8)));
		if (seen.size % prevPage === 0 && seen.size > 0) { keepBytes = bytes; lastBoundary = seen.size; }
	}
	truncateSync(file, keepBytes);
	offset = lastBoundary; boards = lastBoundary;
	process.stderr.write(`resuming ${ats} at offset ${offset} (kept ${keepBytes} bytes / ${lastBoundary} boards)\n`);
}
writeFileSync(`${file}.page`, String(pageSize));
const out = createWriteStream(file, { flags: flags.resume ? "a" : "w" });
for (;;) {
	// With embeddings each job is ~8 KB of JSON; keep pages small so a page of big boards stays well under Worker limits.
	const qs = new URLSearchParams({ offset: String(offset), limit: String(pageSize) });
	if (flags.status) qs.set("status", flags.status);
	if (flags.enrich) qs.set("enrich", flags.enrich);
	if (flags.since) qs.set("since", flags.since);
	if (flags.slim) qs.set("slim", "1");
	if (flags["skip-empty"]) qs.set("skipEmpty", "1");
	if (flags.embed) qs.set("embed", "1");
	// One page can be hundreds of MB with vectors: stream the body line by line (never buffer it in one string).
	let res, lines;
	for (let attempt = 0; ; attempt++) {
		try {
			res = await fetch(`${base}/export/${ats}?${qs}`, { headers });
			if (res.status >= 500 || res.status === 429) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
			if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}: ${await res.text()}`), { fatal: true });
			lines = [];
			let carry = "";
			const dec = new TextDecoder();
			for await (const chunk of res.body) {
				carry += dec.decode(chunk, { stream: true });
				let nl;
				while ((nl = carry.indexOf("\n")) >= 0) { const l = carry.slice(0, nl); carry = carry.slice(nl + 1); if (l) lines.push(l); }
			}
			carry += dec.decode(); if (carry.trim()) lines.push(carry);
			// A DO failure mid-stream truncates the body without an HTTP error; verify the page is complete.
			// Boards may span several lines ({part, more}) when vectors are included; count distinct boards.
			const got = new Set(lines.map((l) => { const i = l.indexOf('"slug":"'); return i < 0 ? l : l.slice(i, l.indexOf('"', i + 8)); })).size;
			const expected = Number(res.headers.get("x-page-boards") ?? got);
			if (!flags["skip-empty"] && got !== expected) throw new Error(`truncated page: ${got}/${expected} boards`);
			break;
		} catch (e) {
			if (e.fatal || attempt >= 4) throw e;
			process.stderr.write(`\n${ats} offset ${offset}: ${e.message}; retrying\n`);
			await new Promise((r) => setTimeout(r, 3000 * 2 ** attempt));
		}
	}
	for (const line of lines) {
		const b = JSON.parse(line);
		if (!b.part) boards++;
		if (b.error) errors++;
		jobs += b.jobs.length;
		out.write(line + "\n");
	}
	const next = res.headers.get("x-next-offset");
	process.stderr.write(`\r${boards}/${res.headers.get("x-total")} boards, ${jobs} jobs`);
	if (!next) break;
	offset = Number(next);
}
out.end();
process.stderr.write("\n");
console.log(`wrote ${outDir}/${ats}.ndjson (${boards} boards, ${jobs} jobs${errors ? `, ${errors} board errors` : ""})`);

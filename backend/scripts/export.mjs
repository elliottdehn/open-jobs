// Pull every board for an ATS from the deployed worker into export/<ats>.ndjson.
// Usage: node scripts/export.mjs <ats> [worker-url] [--status=open|removed|all] [--enrich=pending|done|error]
//                                [--since=<epoch ms>] [--slim] [--skip-empty] [--embed] [--out=DIR] [--resume] [--workers=N]
//   --resume keeps the complete pages already in <out>/<ats>.ndjson and continues from the next page.
//   --workers N pulls N pages concurrently (default 8), writing them in offset order so resume stays correct.
//     (falls back to a sequential cursor walk when --skip-empty makes page offsets non-deterministic.)
//   --embed includes each job's 1536-float embedding (large: ~3 KB/job in JSON).
//   env ADMIN_TOKEN=... if the worker has one configured; env WORKER_URL as default base.
//   e.g. node scripts/export.mjs greenhouse https://x.workers.dev --status=open --skip-empty
import { mkdirSync, createWriteStream, createReadStream, existsSync, readFileSync, writeFileSync, truncateSync, unlinkSync, readdirSync } from "node:fs";
import { createInterface } from "node:readline";
import { pipeline } from "node:stream/promises";

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
const workers = Math.max(1, Number(flags.workers || 8));
const file = `${outDir}/${ats}.ndjson`;

let offset = 0;
let boards = 0;
let jobs = 0;
let errors = 0;
if (flags.resume && existsSync(file)) {
	// Keep whole pages only. Stream the file (it can be many GB), tracking the byte offset of each line, and
	// truncate after the last complete page (page size recorded in <file>.page by the run that wrote it).
	const prevPage = Number((existsSync(`${file}.page`) ? readFileSync(`${file}.page`, "utf8") : "").trim() || pageSize);
	// Scan for the last complete page boundary WITHOUT holding whole lines: dark lines can be multi-GB
	// (a huge aggregator board with vectors), so we inspect only the first HEAD bytes of each line — the
	// slug lives at the start (`{"ats":…,"slug":"X",…}`) — and just track byte positions. O(1) memory.
	const HEAD = 512; const head = Buffer.alloc(HEAD); let headLen = 0;
	const seen = new Set(); let keepBytes = 0; let lastBoundary = 0; let filePos = 0;
	const endLine = (posAfterNL) => {
		const s = head.toString("utf8", 0, headLen);
		const k = s.indexOf('"slug":"'); if (k >= 0) { const e = s.indexOf('"', k + 8); seen.add(e >= 0 ? s.slice(k + 8, e) : s.slice(k + 8)); }
		if (seen.size > 0 && seen.size % prevPage === 0) { keepBytes = posAfterNL; lastBoundary = seen.size; }
		headLen = 0;
	};
	for await (const chunk of createReadStream(file, { highWaterMark: 1 << 22 })) {
		let seg = 0, nl;
		while ((nl = chunk.indexOf(10, seg)) >= 0) { // native newline search (fast over many GB)
			if (headLen < HEAD) { const t = Math.min(HEAD - headLen, nl - seg); chunk.copy(head, headLen, seg, seg + t); headLen += t; }
			endLine(filePos + nl + 1);
			seg = nl + 1;
		}
		if (seg < chunk.length && headLen < HEAD) { const t = Math.min(HEAD - headLen, chunk.length - seg); chunk.copy(head, headLen, seg, seg + t); headLen += t; }
		filePos += chunk.length;
	}
	truncateSync(file, keepBytes);
	offset = lastBoundary; boards = lastBoundary;
	process.stderr.write(`resuming ${ats} at offset ${offset} (kept ${keepBytes} bytes / ${lastBoundary} boards)\n`);
}
writeFileSync(`${file}.page`, String(pageSize));
const out = createWriteStream(file, { flags: flags.resume ? "a" : "w" });
// Fetch one page of boards at `off`, with the same retry + mid-stream-truncation check as before.
// Returns { lines, boards, total, next } (next = x-next-offset, or null at the end).
async function fetchPage(off) {
	const qs = new URLSearchParams({ offset: String(off), limit: String(pageSize) });
	if (flags.status) qs.set("status", flags.status);
	if (flags.enrich) qs.set("enrich", flags.enrich);
	if (flags.since) qs.set("since", flags.since);
	if (flags.slim) qs.set("slim", "1");
	if (flags["skip-empty"]) qs.set("skipEmpty", "1");
	if (flags.embed) qs.set("embed", "1");
	for (let attempt = 0; ; attempt++) {
		try {
			const res = await fetch(`${base}/export/${ats}?${qs}`, { headers });
			if (res.status >= 500 || res.status === 429) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
			if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}: ${await res.text()}`), { fatal: true });
			// One page can be hundreds of MB with vectors: stream the body line by line (never buffer it in one string).
			const lines = []; let carry = ""; const dec = new TextDecoder();
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
			const next = res.headers.get("x-next-offset");
			return { lines, boards: got, total: Number(res.headers.get("x-total") ?? 0), next: next != null ? Number(next) : null };
		} catch (e) {
			if (e.fatal || attempt >= 4) throw e;
			process.stderr.write(`\n${ats} offset ${off}: ${e.message}; retrying\n`);
			await new Promise((r) => setTimeout(r, 3000 * 2 ** attempt));
		}
	}
}

function writePage(lines) {
	for (const line of lines) {
		const b = JSON.parse(line);
		if (!b.part) boards++;
		if (b.error) errors++;
		jobs += b.jobs.length;
		out.write(line + "\n");
	}
}

// Memory-safe page fetch for the parallel path: stream the body straight to a temp file (never hold the
// whole page in RAM, never JSON.parse the vectors), counting boards/jobs from the raw text. Returns the
// temp file path to be concatenated in offset order. `null` tmp = empty page (nothing to append).
async function fetchPageToTmp(off) {
	const qs = new URLSearchParams({ offset: String(off), limit: String(pageSize) });
	if (flags.status) qs.set("status", flags.status);
	if (flags.enrich) qs.set("enrich", flags.enrich);
	if (flags.since) qs.set("since", flags.since);
	if (flags.slim) qs.set("slim", "1");
	if (flags["skip-empty"]) qs.set("skipEmpty", "1");
	if (flags.embed) qs.set("embed", "1");
	for (let attempt = 0; ; attempt++) {
		const tmp = `${file}.part.${off}`;
		try {
			const res = await fetch(`${base}/export/${ats}?${qs}`, { headers });
			if (res.status >= 500 || res.status === 429) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
			if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}: ${await res.text()}`), { fatal: true });
			const ws = createWriteStream(tmp);
			const seen = new Set(); let njobs = 0, nerr = 0, carry = ""; const dec = new TextDecoder();
			const handle = (l) => {
				if (!l) return;
				const i = l.indexOf('"slug":"'); if (i >= 0) seen.add(l.slice(i + 8, l.indexOf('"', i + 8)));
				// count without parsing: each job carries "embedStatus"; board-level errors carry "error":true
				let x = 0; while ((x = l.indexOf('"embedStatus":', x)) >= 0) { njobs++; x += 14; }
				if (l.indexOf('"error":true') >= 0) nerr++;
				ws.write(l + "\n");
			};
			for await (const chunk of res.body) {
				carry += dec.decode(chunk, { stream: true });
				let nl; while ((nl = carry.indexOf("\n")) >= 0) { handle(carry.slice(0, nl)); carry = carry.slice(nl + 1); }
			}
			carry += dec.decode(); if (carry.trim()) handle(carry);
			await new Promise((r, j) => ws.end((e) => (e ? j(e) : r())));
			const got = seen.size;
			const expected = Number(res.headers.get("x-page-boards") ?? got);
			if (!flags["skip-empty"] && got !== expected) throw new Error(`truncated page: ${got}/${expected} boards`);
			return { off, tmp: got ? tmp : null, boards: got, jobs: njobs, errors: nerr, total: Number(res.headers.get("x-total") ?? 0) };
		} catch (e) {
			try { unlinkSync(tmp); } catch { /* nothing written */ }
			if (e.fatal || attempt >= 4) throw e;
			process.stderr.write(`\n${ats} offset ${off}: ${e.message}; retrying\n`);
			await new Promise((r) => setTimeout(r, 3000 * 2 ** attempt));
		}
	}
}

if (flags["skip-empty"]) {
	// --skip-empty drops empty boards, so page offsets are non-deterministic: keep the sequential
	// cursor walk driven by x-next-offset.
	for (let cur = offset, total = 0; ; ) {
		const p = await fetchPage(cur);
		writePage(p.lines); total = p.total;
		process.stderr.write(`\r${boards}/${total} boards, ${jobs} jobs`);
		if (p.next == null) break;
		cur = p.next;
	}
} else {
	// Without skip-empty every page holds exactly `pageSize` boards, so offsets are deterministic
	// (offset, +pageSize, +2*pageSize, …). Pull `workers` pages concurrently — each streams to its own
	// temp file — then concatenate them in offset order into the output. The file always ends on a page
	// boundary, so --resume (which truncates to the last full page) stays correct. Streaming to disk
	// keeps memory flat regardless of how big the aggregator pages are.
	for (const p of readdirSync(outDir)) if (p.startsWith(`${ats}.ndjson.part.`)) try { unlinkSync(`${outDir}/${p}`); } catch { /* ignore */ }
	let total = Infinity;
	for (let cur = offset; cur < total; ) {
		const offs = [];
		const cap = total === Infinity ? cur + workers * pageSize : total;
		for (let i = 0; i < workers && cur + i * pageSize < cap; i++) offs.push(cur + i * pageSize);
		const pages = await Promise.all(offs.map(fetchPageToTmp));  // in-flight concurrency = workers, each streamed to disk
		if (total === Infinity) total = pages[0].total || 0;
		for (const p of pages) {                                    // offs is ascending -> concatenated in order
			if (p.tmp) { await pipeline(createReadStream(p.tmp), out, { end: false }); unlinkSync(p.tmp); }
			boards += p.boards; jobs += p.jobs; errors += p.errors;
		}
		cur += offs.length * pageSize;
		process.stderr.write(`\r${boards}/${total} boards, ${jobs} jobs (x${workers})`);
	}
}
out.end();
process.stderr.write("\n");
console.log(`wrote ${outDir}/${ats}.ndjson (${boards} boards, ${jobs} jobs${errors ? `, ${errors} board errors` : ""})`);

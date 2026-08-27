// Fetch boards from this machine (for providers that block Cloudflare IPs).
//   --ingest=<worker-url>  POST each board's snapshot to the Worker (/boards/:ats/:slug/ingest) so the
//                          Board DO runs the normal pipeline (diff, embeddings, enrichment) and the
//                          board is exported like any other. This is what pull-all.sh does.
//   (no --ingest)          write export/<ats>.ndjson directly in the /export shape (no DO state).
// Run: node --experimental-strip-types scripts/fetch-local.mjs [--ingest=URL] [ats...]
// With no ATS args, fetches every ATS listed in `localOnlyAts` (src/ats/index.ts).
import { mkdirSync, createWriteStream } from "node:fs";

import { readFileSync } from "node:fs";

// Keep in sync with `localOnlyAts` in src/ats/index.ts (imported directly here because Node's
// ESM loader needs explicit .ts extensions that the Worker bundle doesn't use).
const LOCAL_ONLY = ["jobscore"];
const boards = JSON.parse(readFileSync(new URL("../src/boards.json", import.meta.url), "utf8"));
const slugsFor = (ats) => boards[ats] ?? [];
const args = process.argv.slice(2);
const ingest = args.find((a) => a.startsWith("--ingest="))?.slice(9) ?? (args.includes("--ingest") ? process.env.WORKER_URL : undefined);
const atses = args.filter((a) => !a.startsWith("--")).length ? args.filter((a) => !a.startsWith("--")) : LOCAL_ONLY;
const CONCURRENCY = 4;
const headers = { "content-type": "application/json", "user-agent": "open-jobs-cli", ...(process.env.ADMIN_TOKEN ? { authorization: `Bearer ${process.env.ADMIN_TOKEN}` } : {}) };
mkdirSync("export", { recursive: true });

async function postIngest(ats, slug, body) {
	for (let attempt = 0; ; attempt++) {
		const res = await fetch(`${ingest}/boards/${ats}/${encodeURIComponent(slug)}/ingest`, { method: "POST", headers, body: JSON.stringify(body) });
		if (res.ok) return res.json();
		if (attempt >= 3) throw new Error(`ingest ${slug}: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
		await new Promise((r) => setTimeout(r, 2000 * 2 ** attempt));
	}
}

for (const ats of atses) {
	const mod = await import(`../src/ats/${ats}.ts`);
	const fetcher = mod[ats] ?? Object.values(mod)[0];
	if (!fetcher?.fetchJobs) throw new Error(`no fetcher for ${ats}`);
	const slugs = slugsFor(ats);
	const out = ingest ? null : createWriteStream(`export/${ats}.ndjson`);
	let done = 0, ok = 0, jobsTotal = 0;
	const queue = [...slugs];
	await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
		for (;;) {
			const slug = queue.shift();
			if (slug === undefined) return;
			const now = Date.now();
			if (ingest) {
				try {
					const r = await fetcher.fetchJobs(slug);
					const m = await postIngest(ats, slug, r);
					if (m.lastStatus === "ok") { ok++; jobsTotal += m.jobCount; }
				} catch (e) {
					try { await postIngest(ats, slug, { status: "error", error: e instanceof Error ? e.message : String(e) }); } catch {}
				}
				done++;
				process.stderr.write(`\r${ats}: ${done}/${slugs.length} boards ingested, ${ok} ok, ${jobsTotal} jobs`);
				continue;
			}
			const meta = { name: `${ats}/${slug}`, ats, slug, source: "local", slotMs: null, lastRunAt: now, lastOkAt: null, lastStatus: null, lastError: null, consecutiveFailures: 0, jobCount: 0, nextFetchAt: null, nextAlarmAt: null };
			let jobs = [];
			try {
				const r = await fetcher.fetchJobs(slug);
				if (r.status === "gone") meta.lastStatus = "gone";
				else {
					meta.lastStatus = "ok"; meta.lastOkAt = now; meta.jobCount = r.jobs.length; ok++;
					jobs = r.jobs.map((j) => ({ ...j, contentHash: null, firstSeenAt: now, lastSeenAt: now, changedAt: now, removedAt: null, enrichStatus: "pending", enrichedAt: null, enrichment: null, enrichError: null }));
				}
			} catch (e) {
				meta.lastStatus = "error"; meta.lastError = e instanceof Error ? e.message : String(e);
			}
			jobsTotal += jobs.length;
			out.write(JSON.stringify({ ats, slug, meta, jobs }) + "\n");
			done++;
			process.stderr.write(`\r${ats}: ${done}/${slugs.length} boards, ${ok} ok, ${jobsTotal} jobs`);
		}
	}));
	if (out) {
		await new Promise((r) => out.end(r));
		process.stderr.write(`\nwrote export/${ats}.ndjson\n`);
	} else process.stderr.write(`\ningested ${ats} into ${ingest}\n`);
}

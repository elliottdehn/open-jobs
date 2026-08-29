import { enabledAts, boardName, slugsFor, fetchers } from "./ats";
import { resolveJobUrl } from "./probe";
export { Board } from "./board";
export { RateLimit } from "./ratelimit";
export { Budget } from "./budget";
import type { BoardState, EnrichJobsResult, JobQuery, StoredJob } from "./board";
import { discoverUid } from "./ats/comeet";
import type { SyncMode } from "./registry";
import { EMBED_TAG, embedQueryText } from "./openai";
export { Registry } from "./registry";

const EXPORT_CONCURRENCY = 20;

function unauthorized(): Response {
	return new Response("unauthorized", { status: 401 });
}

const norm = (s: string) => (s ?? "").toLowerCase().replace(/\/+$/, "").replace(/^https?:\/\/(www\.)?/, "");

function authorized(request: Request, env: Env): boolean {
	if (!env.ADMIN_TOKEN) return true; // no token configured (local dev)
	return request.headers.get("authorization") === `Bearer ${env.ADMIN_TOKEN}`;
}

/** Parse job filters from the query string: status=open|removed|all, enrich=pending|done|error, since=<ms>, slim=1 */
function jobQuery(url: URL): JobQuery {
	const q = url.searchParams;
	const status = q.get("status");
	const enrich = q.get("enrich");
	return {
		status: status === "open" || status === "removed" || status === "all" ? status : undefined,
		enrich: enrich === "pending" || enrich === "done" || enrich === "error" ? enrich : undefined,
		since: q.get("since") ? Number(q.get("since")) : undefined,
		slim: q.get("slim") === "1",
		embed: q.get("embed") === "1",
		detailRaw: q.get("detailRaw") === "1",
		ids: q.get("ids")?.split(",").filter(Boolean),
	};
}

async function syncAll(
	env: Env,
	opts: { mode?: SyncMode; skipRecentMs?: number; only?: string[] } = {},
): Promise<Record<string, unknown>> {
	const out: Record<string, unknown> = {};
	for (const ats of opts.only ?? enabledAts) {
		if (!enabledAts.includes(ats)) continue;
		out[ats] = await env.REGISTRY.getByName(ats).sync(ats, { mode: opts.mode, skipRecentMs: opts.skipRecentMs });
	}
	return out;
}

export default {
	/** Daily sweep: (re)arm every board's alarm. Self-heals boards that lost their alarm. */
	async scheduled(_controller, env, ctx): Promise<void> {
		ctx.waitUntil(syncAll(env));
	},

	async fetch(request, env): Promise<Response> {
		const url = new URL(request.url);
		const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
		const cors = {
			"access-control-allow-origin": "*",
			"access-control-allow-methods": "GET, POST, OPTIONS",
			"access-control-allow-headers": "content-type, authorization",
			"access-control-expose-headers": "content-range, content-length, accept-ranges, x-ratelimit-remaining",
		};
		if (request.method === "OPTIONS") return new Response(null, { headers: cors });

		// ---- public endpoints (no admin token) ----

		// GET /probe?url=<job url>[&board=ats/slug] -> is this posting in the corpus, and how fresh is its board?
		//   {resolved:{ats,slug,id,hint}, crawled, board:{lastOkAt,lastStatus,jobCount,nextFetchAt,slotMs}|null,
		//    job:{found,id,title,url,location,status,firstSeenAt,lastSeenAt,removedAt,detailStatus,embedStatus,embedding?}|null}
		//   Public; 60 per 10 min per IP. The laptop tool (jobs.py probe) adds snapshot date, group and rank.
		if (parts[0] === "probe" && parts.length === 1 && request.method === "GET") {
			const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
			const rl = await env.RATELIMIT.getByName(`probe:${ip}`).hit(60, 600_000);
			if (!rl.ok) return Response.json({ error: "rate limited", retryAfterSeconds: Math.ceil(rl.resetMs / 1000) }, { status: 429, headers: cors });
			const jobUrl = (url.searchParams.get("url") ?? "").trim();
			if (!jobUrl) return Response.json({ error: "url required" }, { status: 400, headers: cors });
			try {
			const r = resolveJobUrl(jobUrl);
			const forced = url.searchParams.get("board");
			if (forced && forced.includes("/")) { const [fa, ...rest] = forced.split("/"); r.ats = fa; r.slug = rest.join("/"); r.hint = undefined; }
			if (!r.ats || !fetchers[r.ats]) return Response.json({ resolved: r, crawled: false, board: null, job: null }, { headers: cors });
			if (!r.slug) return Response.json({ resolved: r, crawled: false, board: null, job: null }, { headers: cors });
			const crawled = slugsFor(r.ats).includes(r.slug);
			const stub = env.BOARD.getByName(boardName(r.ats, r.slug));
			const meta = await stub.getMeta();
			if (!meta) return Response.json({ resolved: r, crawled, board: null, job: null }, { headers: cors });
			const full = (await stub.findJob(r.id, jobUrl)) as unknown as StoredJob | null;
			let job: Record<string, unknown>;
			if (full) {
				job = { found: true, id: full.id, title: full.title, url: full.url, location: full.location ?? null, status: full.removedAt ? "removed" : "open",
					firstSeenAt: full.firstSeenAt, lastSeenAt: full.lastSeenAt, removedAt: full.removedAt ?? null, publishedAt: full.publishedAt ?? null,
					detailStatus: full.detailStatus, embedStatus: full.embedStatus, embedding: (full as { embedding?: number[] }).embedding ?? null };
			} else job = { found: false };
			const board = { lastOkAt: meta.lastOkAt, lastRunAt: meta.lastRunAt, lastStatus: meta.lastStatus, lastError: meta.lastError, jobCount: meta.jobCount, nextFetchAt: meta.nextFetchAt, slotMs: meta.slotMs };
			return Response.json({ resolved: r, crawled, board, job }, { headers: cors });
			} catch (e) {
				const msg = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
				return Response.json({ error: msg }, { status: 500, headers: cors });
			}
		}

		// POST /embed  {"text": "...", "title"?: "...", "location"?: "..."} -> {vector: number[1536], recipe}
		// IP rate limited: EMBED_RATE_LIMIT requests per EMBED_RATE_WINDOW_MS (defaults 10 / 10 min).
		if (parts[0] === "embed" && parts.length === 1 && request.method === "POST") {
			const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
			const limit = Number(env.EMBED_RATE_LIMIT || 10);
			const windowMs = Number(env.EMBED_RATE_WINDOW_MS || 600_000);
			const rl = await env.RATELIMIT.getByName(`embed:${ip}`).hit(limit, windowMs);
			if (!rl.ok) {
				return Response.json(
					{ error: "rate limited", retryAfterSeconds: Math.ceil(rl.resetMs / 1000) },
					{ status: 429, headers: { ...cors, "retry-after": String(Math.ceil(rl.resetMs / 1000)), "x-ratelimit-remaining": "0" } },
				);
			}
			const body = (await request.json().catch(() => null)) as { text?: string; title?: string; location?: string } | null;
			const text = body?.text?.trim();
			if (!text || text.length < 20) return Response.json({ error: "text (>= 20 chars) required" }, { status: 400, headers: cors });
			if (text.length > 60_000) return Response.json({ error: "text too long (max 60k chars)" }, { status: 400, headers: cors });
			try {
				const v = await embedQueryText(env, text, { title: body?.title, location: body?.location });
				return Response.json({ vector: Array.from(v), recipe: EMBED_TAG }, { headers: { ...cors, "x-ratelimit-remaining": String(rl.remaining) } });
			} catch (e) {
				const msg = e instanceof Error ? e.message : String(e);
				const busy = /429|rate limit/i.test(msg);
				return Response.json({ error: busy ? "embedding service busy, try again in a moment" : msg }, { status: busy ? 503 : 500, headers: { ...cors, "retry-after": "5" } });
			}
		}

		// POST /enrich  {"jobs":[{"ats","slug","id"}…]} -> public, per-IP metered enrichment of jobs + their boards' companies.
		//   Cached results are free. Uncached work is reserved against the IP's budget (ENRICH_HOUR_USD / ENRICH_DAY_USD),
		//   then settled at actual token + web-search cost. 429 with retry-after when a window is exhausted.
		//   Response: { boards: {name: {company, companyError}}, jobs: {key: {status, enrichment, cached}}, cost: {...} }
		if (parts[0] === "enrich" && parts.length === 1 && request.method === "POST") {
			const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
			const hourLimit = Number(env.ENRICH_HOUR_USD || 5), dayLimit = Number(env.ENRICH_DAY_USD || 50);
			const body = (await request.json().catch(() => null)) as { jobs?: { ats: string; slug: string; id: string }[] } | null;
			const list = (body?.jobs ?? []).filter((j) => j && fetchers[j.ats] && j.slug && j.id).slice(0, 300);
			if (!list.length) return Response.json({ error: "jobs[] required (max 300 per call)" }, { status: 400, headers: cors });
			const byBoard = new Map<string, string[]>();
			for (const j of list) { const name = boardName(j.ats, j.slug); byBoard.set(name, [...(byBoard.get(name) ?? []), j.id]); }
			// dry run: what's cached, what would cost
			const dry = new Map<string, EnrichJobsResult>();
			await Promise.all([...byBoard].map(async ([name, ids]) => { try { dry.set(name, await env.BOARD.getByName(name).enrichJobs(ids, false, true)); } catch { /* skip board */ } }));
			const EST_JOB = 0.0012, EST_COMPANY = 0.015;
			let estimate = 0;
			for (const [, r] of dry) estimate += r.todo.length * EST_JOB + (r.companyCached ? 0 : EST_COMPANY);
			const budget = env.BUDGET.getByName(`ip:${ip}`);
			let reserved = 0;
			if (estimate > 0) {
				const res = await budget.reserve(estimate, hourLimit, dayLimit);
				if (!res.ok) {
					// nothing new admitted: still return the cached part
					const boards: Record<string, unknown> = {}, jobs: Record<string, unknown> = {};
					for (const [name, r] of dry) { boards[name] = { company: r.company, companyError: r.companyError }; for (const [id, x] of Object.entries(r.jobs)) jobs[`${name}#${id}`] = x; }
					return Response.json({ boards, jobs, rateLimited: true, retryAfterSeconds: Math.ceil(res.retryAfterMs / 1000), spent: { hourUsd: res.hourUsd, dayUsd: res.dayUsd, hourLimit, dayLimit } },
						{ status: 429, headers: { ...cors, "retry-after": String(Math.ceil(res.retryAfterMs / 1000)) } });
				}
				reserved = estimate;
			}
			const boards: Record<string, unknown> = {}, jobs: Record<string, unknown> = {};
			let actual = 0;
			await Promise.all([...byBoard].map(async ([name, ids]) => {
				try {
					const r: EnrichJobsResult = await env.BOARD.getByName(name).enrichJobs(ids, false, false);
					actual += r.costUsd;
					boards[name] = { company: r.company, companyError: r.companyError };
					for (const [id, x] of Object.entries(r.jobs)) jobs[`${name}#${id}`] = x;
				} catch (e) {
					const error = e instanceof Error ? e.message : String(e);
					boards[name] = { company: null, companyError: error };
					for (const id of ids) jobs[`${name}#${id}`] = { status: "error", error };
				}
			}));
			if (reserved > 0) await budget.settle(reserved, actual);
			const st = await budget.status(hourLimit, dayLimit);
			return Response.json({ boards, jobs, cost: { thisCallUsd: +actual.toFixed(4), hourUsd: +st.hourUsd.toFixed(4), dayUsd: +st.dayUsd.toFixed(4), hourLimit, dayLimit } }, { headers: cors });
		}

		// POST /ideas -> relays an idea to the #multipenny-ideas Slack channel, formatted (Block Kit).
		//   body: {"file":"tools/jobs.py","line":123,"idea":"…","tags":[...]}  or  {"text":"<file:line> — <idea>"}. No identity is collected; a `via` field is ignored.
		//   webhook is a secret; 30/hour per IP.
		if (parts[0] === "ideas" && parts.length === 1 && request.method === "POST") {
			if (!env.SLACK_IDEAS_WEBHOOK) return Response.json({ error: "idea relay not configured" }, { status: 503, headers: cors });
			const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
			const rl = await env.RATELIMIT.getByName(`ideas:${ip}`).hit(30, 3_600_000);
			if (!rl.ok) return Response.json({ error: "rate limited" }, { status: 429, headers: { ...cors, "retry-after": String(Math.ceil(rl.resetMs / 1000)) } });
			const body = (await request.json().catch(() => null)) as { text?: string; file?: string; line?: number | string; idea?: string; tags?: string[] } | null;
			let file = (body?.file ?? "").toString().trim(), line = (body?.line ?? "").toString().trim(), idea = (body?.idea ?? "").toString().trim();
			if (!idea && body?.text) {
				// "<file:line> — <idea>" (a trailing "_via …_" line, from older agents, is dropped)
				const t = body.text.toString();
				const m = /^\s*([\w./-]+?)(?::(\d+))?\s*[—–-]+\s*([\s\S]*?)\s*(?:\n_via\s+([^_]+)_)?\s*$/.exec(t);
				if (m) { file = m[1] ?? ""; line = m[2] ?? ""; idea = (m[3] ?? "").trim(); } else idea = t.replace(/\n_via\s+[^_]+_\s*$/, "").trim();
			}
			idea = idea.slice(0, 1500);
			if (idea.length < 10) return Response.json({ error: "idea (>= 10 chars) required" }, { status: 400, headers: cors });
			const repo = "https://github.com/elliottdehn/open-jobs/blob/main/";
			const where = file ? (line ? `<${repo}${file}#L${line}|${file}:${line}>` : `<${repo}${file}|${file}>`) : "";
			const tags = (body?.tags ?? []).slice(0, 5).map((x) => "`" + String(x).slice(0, 24) + "`").join(" ");
			const ctx = ["🤖 agent", tags, `<!date^${Math.floor(Date.now() / 1000)}^{date_short} {time}|now>`].filter(Boolean).join("   ·   ");
			// IDEAS_MENTION: a Slack member ID (U…/W…) renders as a real <@mention>; a plain "@name" relies on link_names.
			const who = (env.IDEAS_MENTION ?? "@egd").trim();
			const mention = /^[UW][A-Z0-9]{6,}$/.test(who) ? `<@${who}>` : who;
			const payload = {
				text: `${mention} ${file ? file + (line ? ":" + line : "") + " — " : ""}${idea}`,
				link_names: true,
				blocks: [
					{ type: "section", text: { type: "mrkdwn", text: `${mention} 💡 *${where || "idea"}*` } },
					{ type: "section", text: { type: "mrkdwn", text: idea } },
					{ type: "context", elements: [{ type: "mrkdwn", text: ctx }] },
					{ type: "divider" },
				],
			};
			const r = await fetch(env.SLACK_IDEAS_WEBHOOK, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
			return Response.json({ ok: r.ok }, { status: r.ok ? 200 : 502, headers: cors });
		}

		// GET /enrich/budget -> this IP's spend windows
		if (parts[0] === "enrich" && parts[1] === "budget" && request.method === "GET") {
			const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
			return Response.json(await env.BUDGET.getByName(`ip:${ip}`).status(Number(env.ENRICH_HOUR_USD || 5), Number(env.ENRICH_DAY_USD || 50)), { headers: cors });
		}

		// GET /data/<key>  -> object from the DATA R2 bucket (manifest, group files, parquet) with Range support
		if (parts[0] === "data" && parts.length >= 2 && request.method === "GET") {
			const key = parts.slice(1).join("/");
			const range = request.headers.get("range");
			const obj = await env.DATA.get(key, { range: range ? request.headers : undefined });
			if (!obj) return new Response("not found", { status: 404, headers: cors });
			const headers = new Headers(cors);
			obj.writeHttpMetadata(headers);
			headers.set("etag", obj.httpEtag);
			headers.set("accept-ranges", "bytes");
			headers.set("cache-control", "public, max-age=3600");
			if (range && obj.range && "offset" in obj.range && obj.range.offset !== undefined) {
				const offset = obj.range.offset;
				const end = offset + (obj.range.length ?? obj.size - offset) - 1;
				headers.set("content-range", `bytes ${offset}-${end}/${obj.size}`);
				headers.set("content-length", String(end - offset + 1));
				return new Response(obj.body, { status: 206, headers });
			}
			headers.set("content-length", String(obj.size));
			return new Response(obj.body, { headers });
		}

		// ---- admin endpoints ----
		if (!authorized(request, env)) return unauthorized();

		// GET /ats -> providers fetched by the Worker fleet and their slug counts; ?all=1 adds local-only ones
		if (parts[0] === "ats" && parts.length === 1) {
			const list = url.searchParams.get("all") ? Object.keys(fetchers) : enabledAts;
			return Response.json(Object.fromEntries(list.map((a) => [a, slugsFor(a).length])));
		}

		// POST /sync            -> start registry sweep (arm only) for all enabled ATSes
		// GET  /sync/:ats       -> sweep status
		if (parts[0] === "sync") {
			if (request.method === "POST" && parts.length === 1) return Response.json(await syncAll(env));
			if (parts.length === 2) return Response.json(await env.REGISTRY.getByName(parts[1]).status());
		}

		// POST /backfill[?ats=a,b] -> kick every board with a detail/embed/enrich backlog so it drains now
		//   (minute ticks per board, 150 detail requests / 100 embeddings per tick). Progress: GET /sync/:ats
		//   (`fetched` = boards kicked, `skipped` = boards with nothing to do).
		if (parts[0] === "backfill" && request.method === "POST" && parts.length === 1) {
			const only = url.searchParams.get("ats")?.split(",").filter(Boolean);
			return Response.json(await syncAll(env, { mode: "kick", only }));
		}

		// POST /fetch-all[?ats=a,b][&skipRecent=<ms>] -> on-demand fetch of every board (arms if needed),
		// via the same Registry sweep. Boards that completed a fetch within skipRecent (default 6h; 0 =
		// force) are skipped. Does not change any board's daily slot. Progress: GET /sync/:ats
		if (parts[0] === "fetch-all" && request.method === "POST" && parts.length === 1) {
			const only = url.searchParams.get("ats")?.split(",").filter(Boolean);
			const skipRecentRaw = url.searchParams.get("skipRecent");
			const skipRecentMs = skipRecentRaw === null ? undefined : Number(skipRecentRaw);
			return Response.json(await syncAll(env, { mode: "fetch", skipRecentMs, only }));
		}

		// GET /comeet/resolve/:slug -> {name, uid} | null, resolved from the edge (used by scripts/build-comeet.mjs)
		if (parts[0] === "comeet" && parts[1] === "resolve" && parts.length === 3) {
			return Response.json(await discoverUid(parts[2]));
		}

		// GET  /boards/:ats/:slug[?status=open&enrich=done&since=<ms>&slim=1]  -> meta + filtered jobs
		// GET  /boards/:ats/:slug/runs               -> recent fetch runs with diff counts
		// POST /boards/:ats/:slug/fetch              -> force a fetch now
		// POST /boards/:ats/:slug/retry-enrichment   -> re-queue errored enrichments
		// POST /boards/:ats/:slug/ingest              -> body = FetchResult fetched off-Cloudflare (local-only ATSes)
		if (parts[0] === "boards" && parts.length >= 3) {
			const [, ats, slug, action] = parts;
			if (!fetchers[ats]) return new Response("unknown ats", { status: 404 });
			const name = boardName(ats, slug);
			const stub = env.BOARD.getByName(name);
			if (action === "ingest" && request.method === "POST") {
				const body = (await request.json()) as { status: "ok" | "gone" | "error"; jobs?: unknown[]; error?: string };
				const result =
					body.status === "gone" ? { status: "gone" as const }
					: body.status === "error" ? { status: "error" as const, error: body.error ?? "unknown error" }
					: { status: "ok" as const, jobs: (body.jobs ?? []) as import("./ats/types").Job[] };
				return Response.json(await stub.ingest(name, result));
			}
			if (action === "fetch" && request.method === "POST") {
				await stub.ensureScheduled(name);
				return Response.json(await stub.fetchNow());
			}
			// POST /boards/:ats/:slug/enrich-board[?force=1] -> resolve the company behind the board now
			if (action === "enrich-board" && request.method === "POST") {
				await stub.ensureScheduled(name);
				return Response.json(await stub.enrichBoardNow(url.searchParams.get("force") === "1"));
			}
			if (action === "retry-enrichment" && request.method === "POST") {
				return Response.json({ requeued: await stub.retryEnrichment() });
			}
			if (action === "embed" && request.method === "POST") return Response.json(await stub.embedNow());
			if (action === "runs") return Response.json(await stub.getRuns());
			if (!action) {
				try { return Response.json(await stub.getState(jobQuery(url))); }
				catch (e) { return Response.json({ error: e instanceof Error ? e.message : String(e) }, { status: 500 }); }
			}
		}

		// POST /jobs/enrich  body {"jobs":[{"ats","slug","id"}...], "force"?: bool}
		//   Lazy, idempotent job enrichment: already-enriched jobs come back from storage; the rest
		//   are extracted now (structured output on OPENAI_MODEL). Grouped per board.
		if (parts[0] === "jobs" && parts[1] === "enrich" && request.method === "POST") {
			const body = (await request.json()) as { jobs: { ats: string; slug: string; id: string }[]; force?: boolean };
			const byBoard = new Map<string, string[]>();
			for (const j of body.jobs ?? []) {
				if (!fetchers[j.ats]) continue;
				const name = boardName(j.ats, j.slug);
				byBoard.set(name, [...(byBoard.get(name) ?? []), j.id]);
			}
			// Response: { boards: { "<ats>/<slug>": { company, companyError } }, jobs: { "<ats>/<slug>#<id>": {...} } }
			const boards: Record<string, unknown> = {};
			const jobs: Record<string, unknown> = {};
			await Promise.all(
				[...byBoard].map(async ([name, ids]) => {
					try {
						const res: EnrichJobsResult = await env.BOARD.getByName(name).enrichJobs(ids, body.force === true);
						boards[name] = { company: res.company, companyError: res.companyError };
						for (const [id, r] of Object.entries(res.jobs)) jobs[`${name}#${id}`] = r;
					} catch (e) {
						const error = e instanceof Error ? e.message : String(e);
						boards[name] = { company: null, companyError: error };
						for (const id of ids) jobs[`${name}#${id}`] = { status: "error", error };
					}
				}),
			);
			return Response.json({ boards, jobs });
		}

		// POST /boards/:ats/:slug/embed -> embed every un-embedded job on the board now
		// GET /export/:ats?offset=0&limit=200[&status=open&enrich=done&since=<ms>&slim=1&skipEmpty=1] -> NDJSON, one line per board: {ats, slug, meta, jobs}
		if (parts[0] === "export" && parts.length === 2) {
			const ats = parts[1];
			if (!fetchers[ats]) return new Response("unknown ats", { status: 404 });
			const all = slugsFor(ats);
			const offset = Number(url.searchParams.get("offset") ?? 0);
			const limit = Math.min(Number(url.searchParams.get("limit") ?? 200), 500);
			const slugs = all.slice(offset, offset + limit);
			const query = jobQuery(url);
			const skipEmpty = url.searchParams.get("skipEmpty") === "1";
			const enc = new TextEncoder();
			// Pull-driven stream with backpressure: the client's read pace bounds Worker memory (a page of big
			// boards with vectors + JD bodies is hundreds of MB — eagerly enqueuing it OOMs the isolate).
			// Big boards are paged internally (PAGE jobs per DO call) and emitted as multi-part lines.
			const PAGE = query.embed ? 150 : 0;
			const enc2 = enc;
			let bi = 0; // next board index
			let cur: { slug: string; part: number; done: boolean } | null = null;
			let lookahead: Promise<string | null> | null = null;
			const nextLine = async (): Promise<string | null> => {
				for (;;) {
					if (cur === null) {
						if (bi >= slugs.length) return null;
						cur = { slug: slugs[bi++], part: 0, done: false };
					}
					const slug = cur.slug;
					const stub = env.BOARD.getByName(boardName(ats, slug));
					try {
						if (!PAGE) {
							const st: BoardState = await stub.getState(query);
							cur = null;
							if (skipEmpty && st.jobs.length === 0) continue;
							return JSON.stringify({ ats, slug, meta: st.meta, jobs: st.jobs });
						}
						// Page size adapts per board: a DO RPC response is capped at 32 MiB, and boards with big
						// provider payloads can exceed it at 300 jobs; halve until it fits (min 25).
						const c = cur as { slug: string; part: number; done: boolean; page?: number; off?: number };
						c.page = c.page ?? PAGE; c.off = c.off ?? 0;
						let st: BoardState | null = null;
						for (;;) {
							try {
								st = c.off === 0
									? await stub.getState({ ...query, jobLimit: c.page, jobOffset: 0 })
									: { meta: null, jobs: await stub.getJobs({ ...query, jobLimit: c.page, jobOffset: c.off }) };
								break;
							} catch (e) {
								const msg = e instanceof Error ? e.message : String(e);
								if (/32MiB|limited to 32/i.test(msg) && c.page > 25) { c.page = Math.max(25, Math.floor(c.page / 2)); continue; }
								throw e;
							}
						}
						const part = c.part;
						const more = st.jobs.length === c.page;
						if (more) { c.part++; c.off += c.page; } else cur = null;
						if (part === 0 && skipEmpty && st.jobs.length === 0) continue;
						return JSON.stringify({ ats, slug, meta: st.meta, jobs: st.jobs, part, more });
					} catch (e) {
						cur = null;
						return JSON.stringify({ ats, slug, meta: null, jobs: [], error: e instanceof Error ? e.message : String(e) });
					}
				}
			};
			const stream = new ReadableStream({
				async pull(controller) {
					const line = await (lookahead ?? nextLine());
					lookahead = null;
					if (line === null) {
						controller.close();
						return;
					}
					controller.enqueue(enc2.encode(line + "\n"));
					lookahead = nextLine(); // overlap the next DO call with the client draining this chunk
				},
				cancel() {
					lookahead = null;
				},
			});
			return new Response(stream, {
				headers: {
					"content-type": "application/x-ndjson",
					"x-total": String(all.length),
					"x-page-boards": String(slugs.length),
					"x-next-offset": offset + slugs.length < all.length ? String(offset + slugs.length) : "",
				},
			});
		}

		return new Response("not found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;

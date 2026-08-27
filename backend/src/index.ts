import { enabledAts, boardName, slugsFor, fetchers } from "./ats";
export { Board } from "./board";
export { RateLimit } from "./ratelimit";
import type { BoardState, EnrichJobsResult, JobQuery } from "./board";
import { discoverUid } from "./ats/comeet";
import type { SyncMode } from "./registry";
import { EMBED_TAG, embedQueryText } from "./openai";
export { Registry } from "./registry";

const EXPORT_CONCURRENCY = 20;

function unauthorized(): Response {
	return new Response("unauthorized", { status: 401 });
}

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
			if (obj.range && "offset" in obj.range && obj.range.offset !== undefined) {
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
			if (!action) return Response.json(await stub.getState(jobQuery(url)));
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
			const PAGE = query.embed ? 300 : 0;
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
						const part = cur.part;
						const st: BoardState =
							part === 0
								? await stub.getState({ ...query, jobLimit: PAGE, jobOffset: 0 })
								: { meta: null, jobs: await stub.getJobs({ ...query, jobLimit: PAGE, jobOffset: part * PAGE }) };
						if (part === 0) cur.done = false;
						const more = st.jobs.length === PAGE;
						if (more) cur.part++; else cur = null;
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

import { DurableObject } from "cloudflare:workers";
import { fetchers, parseBoardName } from "./ats";
import type { FetchResult, Job, JobDetail } from "./ats/types";
import { ENRICH_BATCH, ENRICH_CONCURRENCY, enricher, enrichOne, jdText, type JobEnrichment } from "./enrich";
import { EMBED_TAG, embedTexts, EMBED_HEADROOM } from "./openai";
import { deriveCandidates } from "./company";
import { resolveCompany, type CompanyEnrichment } from "./company";
import { usd } from "./pricing";
import { SNAPSHOT_PART_ROWS, buildSnapshotParquet, snapshotKey } from "./snapshot";
import { HttpError } from "./ats/dark";
import { dedupeKey, dedupeShard } from "./dedupe";

const DAY = 86_400_000;
const HOUR = 3_600_000;
const MINUTE = 60_000;
/** After this many consecutive failures/gone results, back off to weekly. */
const BACKOFF_AFTER = 5;
/**
 * A board that completed a fetch (ok or gone) within this window is "fresh": both the daily
 * alarm and force-fetch sweeps skip it, so a force fetch shortly before a board's slot (or
 * vice versa) doesn't hit the provider twice. Errors are never considered fresh.
 */
export const DEFAULT_FRESH_MS = 6 * HOUR;

export type EnrichStatus = "pending" | "done" | "error";
/** Per-job detail fetch: `na` = provider has no detail endpoint or the posting is unavailable. */
export type DetailStatus = "pending" | "done" | "na" | "error";
/** Listing content shorter than this is treated as a snippet: fetchDetail is still run. */
const DETAIL_MIN_CONTENT = 800;
/** Jobs embedded per alarm tick (one embeddings API call), further limited by EMBED_BATCH_CHARS. */
const EMBED_BATCH = 100;
/**
 * Total input chars per embeddings request. Full JDs can be 28k chars (~7k tokens) each, and the
 * API caps a request well below 100 × that; ~240k chars ≈ 60k tokens stays comfortably under.
 */
const EMBED_BATCH_CHARS = 240_000;
/** Per-input ceiling for the embedding model (hard limit 8192 tokens). Estimated conservatively: ASCII at 4 chars per
 *  token, everything else (CJK, accents) at 1.5 tokens per char. One over-long input rejects the whole batch. */
const EMBED_MAX_TOKENS = 7_000;
function clampTokens(s: string, maxTokens: number): string {
	let t = 0;
	for (let i = 0; i < s.length; i++) {
		t += s.charCodeAt(i) < 128 ? 0.25 : 1.5;
		if (t > maxTokens) return s.slice(0, i);
	}
	return s;
}
/** Rows the embed stage still owes: never embedded, pending, a stale recipe, or an error older than a day. A permanent
 *  400 retried every minute kept 13 boards alive around the clock (September 2026). Params: EMBED_TAG, now - DAY. */
const EMBED_TODO = `(embed_status IS NULL OR embed_status = 'pending' OR embed_model != ? OR (embed_status = 'error' AND (embed_tried_at IS NULL OR embed_tried_at < ?)))`;
/**
 * Chars of JD text that go into the embedding. text-embedding-3-small accepts 8,191 tokens per
 * input; ~28k chars of English stays under that. Everything about the job is embedded (company,
 * title, location, departments, terms, full JD) so classifiers can be trained on the vector alone.
 */
const EMBED_TEXT_CHARS = 28_000;
/**
 * Detail fetching is paced by the site, not by us: a tick runs until its wall budget or its subrequest budget is
 * spent, with a per-board concurrency that grows while the site answers cleanly and halves the moment it says
 * 429/503, sends Retry-After, walls us with 403s, or times out. The board then sleeps until the site's own deadline.
 */
const DETAIL_TICK_WALL_MS = 50_000;     // an alarm invocation's wall budget for details (CPU stays far below the limit)
const DETAIL_TICK_SUBREQUESTS = 800;    // under the platform's 1000 subrequests per invocation
const DETAIL_CONC_MIN = 2, DETAIL_CONC_START = 8, DETAIL_CONC_MAX = 32;
const DETAIL_GROW_EVERY = 25;           // clean responses before concurrency grows by half
/** A crawled board with more open rows than this is a national job board, not an employer: it is paused (no fetch,
 *  details, or embeddings) until someone decides what to do with it. Rows already stored stay. */
const DARK_BOARD_MAX_OPEN = 0; // 0 = no pause. The cross-board dedup (src/dedupe.ts) makes copies cheap; the rest is paid for on purpose (budget $300/month, 2026-09-10)

export interface BoardMeta {
	name: string;
	ats: string;
	slug: string;
	/** Fixed daily fetch time, ms offset from UTC midnight. */
	slotMs: number;
	lastRunAt: number | null;
	lastOkAt: number | null;
	lastStatus: "ok" | "gone" | "error" | null;
	lastError: string | null;
	consecutiveFailures: number;
	/** Live (non-removed) jobs after the last successful fetch. */
	jobCount: number;
	/** When the next *fetch* is due. The DO's single alarm may fire earlier for enrichment. */
	nextFetchAt: number | null;
	nextAlarmAt: number | null;
	/** Board-level (company) enrichment. One-shot; see FIELDS.md §1. */
	company?: CompanyEnrichment | null;
	companyError?: string | null;
	companyAttemptedAt?: number | null;
	/** Set when the embeddings API rate-limited us; the next backlog tick waits until then. */
	embedBackoffUntil?: number | null;
	/** Adaptive detail-fetch concurrency for this board, and the site-imposed pause (Retry-After, 429, bot wall). */
	detailConc?: number;
	detailBackoffUntil?: number | null;
	/** The board's best observed mean page latency (ms): the reference for "is the site slowing under our load". */
	detailBaseMs?: number;
	/** Last detail tick: fetched, ok, errors, wall ms, mean latency ms, concurrency at the end (diagnostics). */
	detailTick?: { at: number; fetched: number; ok: number; errors: number; wallMs: number; meanMs: number; conc: number };
	/** First-party boards claim their open postings in the dedup index once, then per fetch for new ones. */
	dedupeSeeded?: boolean;
	/** How many snapshot part files this board last wrote (so shrinking boards delete the leftovers). */
	snapshotParts?: number;
	/**
	 * Board is fetched from outside Cloudflare (provider blocks Worker IPs) and snapshots arrive via
	 * `ingest()`. The alarm never fetches; it only drains detail/embed/enrich backlogs.
	 */
	localOnly?: boolean;
	/** R2 parquet snapshot of current open jobs (see snapshot.ts). */
	snapshotAt?: number | null;
	/** Open set changed since the last snapshot; written once embeds drain (or after 48h regardless). */
	snapshotDirty?: boolean;
	/** Set when a snapshot write was deferred because the publisher had snapshots frozen; arm() retries then. */
	snapshotRetryAt?: number | null;
	dirtySince?: number | null;
	snapshotError?: string | null;
}

export interface RunSummary {
	id: number;
	runAt: number;
	status: "ok" | "gone" | "error";
	added: number;
	changed: number;
	removed: number;
	unchanged: number;
	error: string | null;
}

export type StoredJob = Job & {
	contentHash: string;
	firstSeenAt: number;
	lastSeenAt: number;
	/** Last time the job's content changed (== firstSeenAt for never-changed jobs). */
	changedAt: number;
	removedAt: number | null;
	enrichStatus: EnrichStatus;
	enrichedAt: number | null;
	enrichment: unknown | null;
	enrichError: string | null;
	detailStatus: DetailStatus;
	detailFetchedAt: number | null;
	/** Provider detail payload (listing payload stays in `raw`). */
	detailRaw: unknown | null;
	embedStatus: "pending" | "done" | "error";
	embedModel: string | null;
	embedError: string | null;
	/** Only populated when requested (`embed: true`); large. */
	embedding?: number[];
};

export interface JobQuery {
	status?: "open" | "removed" | "all";
	enrich?: EnrichStatus;
	since?: number;
	slim?: boolean;
	/** Include embedding vectors (1536 floats per job). */
	embed?: boolean;
	/** Restrict to these job ids. */
	ids?: string[];
	/** Include the provider's detail payload (`detailRaw`); off by default — large and duplicates `content`. */
	detailRaw?: boolean;
	/** Page within a board (used by the export when vectors make responses large). */
	jobOffset?: number;
	jobLimit?: number;
}

export interface BoardState {
	meta: BoardMeta | null;
	jobs: StoredJob[];
}

export interface JobEnrichResult {
	status: "done" | "error" | "unknown";
	enrichment?: unknown;
	error?: string;
	cached?: boolean;
}
export interface EnrichJobsResult {
	company: CompanyEnrichment | null;
	companyError: string | null;
	companyCached: boolean;
	jobs: Record<string, JobEnrichResult>;
	/** Keys that would need a model call (dry run) / were called (real run). */
	todo: string[];
	/** Actual USD spent in this call (jobs + company), 0 on dry run. */
	costUsd: number;
}

export interface Diff {
	added: Job[];
	changed: Job[];
	removedIds: string[];
	unchanged: number;
}

type JobRow = {
	id: string;
	data: string;
	content_hash: string;
	first_seen_at: number;
	last_seen_at: number;
	changed_at: number;
	removed_at: number | null;
	enrich_status: string;
	enriched_at: number | null;
	enrichment: string | null;
	enrich_error: string | null;
	detail_status: string | null;
	detail: string | null;
	detail_error: string | null;
	detail_fetched_at: number | null;
	embedding: ArrayBuffer | null;
	embed_model: string | null;
	embed_status: string | null;
	embed_error: string | null;
	embed_tried_at: number | null;
	dup_of: string | null;
	[k: string]: SqlStorageValue;
};

/** Upper bounds for provider calls so a hung connection can never pin the alarm to its 15-min limit. */
const FETCH_JOBS_TIMEOUT_MS = 5 * MINUTE;
const FETCH_DETAIL_TIMEOUT_MS = 45_000;
/** Snapshot staleness escape: write even with a pending embed backlog after this long dirty. */
const SNAPSHOT_STALE_MS = 48 * 60 * 60 * 1000;
/** Boards with more open jobs than this skip snapshotting (isolate memory); they're aggregator-shaped anyway. */
const SNAPSHOT_MAX_JOBS = 400_000; // 80 parts of SNAPSHOT_PART_ROWS; beyond this something is wrong with the board

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
	let t: ReturnType<typeof setTimeout>;
	return Promise.race([
		p.finally(() => clearTimeout(t)),
		new Promise<T>((_, reject) => {
			t = setTimeout(() => reject(new Error(`${what}: timed out after ${ms / 1000}s`)), ms);
		}),
	]);
}

/** FNV-1a 32-bit; deterministic so a board keeps the same slot forever. */
function hash32(s: string, seed = 0x811c9dc5): number {
	let h = seed;
	for (let i = 0; i < s.length; i++) {
		h ^= s.charCodeAt(i);
		h = Math.imul(h, 0x01000193);
	}
	return h >>> 0;
}

/** 64-bit-ish content hash (two FNV-1a passes with different seeds), hex. */
/**
 * What the crawler considers the posting. The provider's raw payload is deliberately excluded: Workday and the
 * crawled career sites carry fields like "posted 3 days ago" that churn every night and rewrote most of their
 * rows daily (4 metered rows each). Prefixed "s" so a stored pre-2026-09-10 hash (whole-JSON, 16 hex) is
 * recognisable and upgraded in place instead of being reported as a change.
 */
function contentHash(job: Job): string {
	const s = JSON.stringify([job.title, job.location, job.url, job.departments, job.publishedAt, job.updatedAt, job.content]);
	return "s" + hash32(s).toString(16).padStart(8, "0") + hash32(s, 0x9747b28c).toString(16).padStart(8, "0");
}

/**
 * "Seen this walk", kept as 52-bit hashes of ids in a Set<number>: ~25 MB per million postings instead of the id
 * strings and a map of the whole board, so a listing walk has no ceiling a single object cannot hold. A collision
 * (~1e-4 per million) means one vanished posting waits a day to be swept.
 */
class SeenSet {
	private s = new Set<number>();
	private static h(id: string): number { return (hash32(id) % 0x100000) * 4294967296 + hash32(id, 0x9747b28c); }
	has(id: string): boolean { return this.s.has(SeenSet.h(id)); }
	add(id: string): void { this.s.add(SeenSet.h(id)); }
	get size(): number { return this.s.size; }
}

function isFresh(meta: BoardMeta, windowMs: number, now = Date.now()): boolean {
	return windowMs > 0 && meta.lastRunAt !== null && meta.lastStatus !== "error" && now - meta.lastRunAt < windowMs;
}

function nextSlotAfter(now: number, slotMs: number): number {
	const midnight = now - (now % DAY);
	const today = midnight + slotMs;
	return today > now ? today : today + DAY;
}

/**
 * `seenFloor` = the board's last successful fetch (meta.lastOkAt). An open job that survived that fetch was seen
 * then, but its row is not rewritten to say so (3M unchanged rows a day was the dominant DO cost), so the stored
 * `last_seen_at` only moves when the row changes; the true value for an open job is max(stored, floor).
 */
function rowToJob(r: JobRow, withEmbedding = false, seenFloor = 0): StoredJob {
	const job = JSON.parse(r.data) as Job;
	// Detail data (fetched once per job) overrides the listing where present.
	const detail = r.detail === null ? null : (JSON.parse(r.detail) as JobDetail);
	if (detail) {
		if (detail.title) job.title = detail.title;
		if (detail.org) (job as Job & { org?: string }).org = detail.org;
		if (detail.content) job.content = detail.content;
		if (detail.location) job.location = detail.location;
		if (detail.publishedAt) job.publishedAt = detail.publishedAt;
		if (detail.updatedAt) job.updatedAt = detail.updatedAt;
		if (detail.departments?.length) job.departments = detail.departments;
	}
	return {
		...job,
		detailStatus: (r.detail_status ?? "pending") as DetailStatus, // NULL = predates the detail stage
		embedStatus: (r.embed_status ?? "pending") as StoredJob["embedStatus"],
		embedModel: r.embed_model,
		embedError: r.embed_error,
		...(withEmbedding && r.embedding ? { embedding: Array.from(new Float32Array(r.embedding)) } : {}),
		detailFetchedAt: r.detail_fetched_at,
		detailRaw: detail?.raw ?? null,
		contentHash: r.content_hash,
		firstSeenAt: r.first_seen_at,
		lastSeenAt: r.removed_at == null ? Math.max(r.last_seen_at, seenFloor) : r.last_seen_at,
		changedAt: r.changed_at,
		removedAt: r.removed_at,
		enrichStatus: r.enrich_status as EnrichStatus,
		enrichedAt: r.enriched_at,
		enrichment: r.enrichment === null ? null : JSON.parse(r.enrichment),
		enrichError: r.enrich_error,
	};
}

/**
 * One instance per job board, named `${ats}/${slug}`.
 * Fetches its board once a day at a fixed, per-board pseudo-random time, diffs the result
 * against the stored snapshot, and enriches new/changed jobs via the pluggable Enricher.
 */
export class Board extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		ctx.blockConcurrencyWhile(async () => {
			ctx.storage.sql.exec(`
				CREATE TABLE IF NOT EXISTS jobs (
					id TEXT PRIMARY KEY,
					data TEXT NOT NULL,
					content_hash TEXT NOT NULL,
					first_seen_at INTEGER NOT NULL,
					last_seen_at INTEGER NOT NULL,
					changed_at INTEGER NOT NULL,
					removed_at INTEGER,
					enrich_status TEXT NOT NULL DEFAULT 'pending',
					enriched_at INTEGER,
					enrichment TEXT,
					enrich_error TEXT
				);
				CREATE INDEX IF NOT EXISTS jobs_enrich ON jobs (enrich_status, removed_at);
				CREATE TABLE IF NOT EXISTS runs (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					run_at INTEGER NOT NULL,
					status TEXT NOT NULL,
					added INTEGER NOT NULL DEFAULT 0,
					changed INTEGER NOT NULL DEFAULT 0,
					removed INTEGER NOT NULL DEFAULT 0,
					unchanged INTEGER NOT NULL DEFAULT 0,
					error TEXT
				);
			`);
			// Additive migrations for DOs created before these columns existed.
			const cols = new Set(ctx.storage.sql.exec<{ name: string }>(`PRAGMA table_info(jobs)`).toArray().map((c) => c.name));
			if (!cols.has("detail_status")) {
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN detail_status TEXT`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN detail TEXT`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN detail_error TEXT`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN detail_fetched_at INTEGER`);
				ctx.storage.sql.exec(`CREATE INDEX IF NOT EXISTS jobs_detail ON jobs (detail_status, removed_at)`);
			}
			if (!cols.has("embedding")) {
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN embedding BLOB`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN embed_model TEXT`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN embed_status TEXT`);
				ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN embed_error TEXT`);
				ctx.storage.sql.exec(`CREATE INDEX IF NOT EXISTS jobs_embed ON jobs (embed_status, removed_at)`);
			}
			if (!cols.has("embed_tried_at")) ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN embed_tried_at INTEGER`);
			if (!cols.has("dup_of")) ctx.storage.sql.exec(`ALTER TABLE jobs ADD COLUMN dup_of TEXT`);
		});
	}

	private async meta(): Promise<BoardMeta | undefined> {
		return this.ctx.storage.get<BoardMeta>("meta");
	}

	/**
	 * Idempotent: called by the Registry sweep. Initializes the board on first touch and
	 * makes sure an alarm is pending. First fetch happens within the next hour (jittered);
	 * afterwards the board runs at its fixed daily slot.
	 */
	async ensureScheduled(name: string): Promise<BoardMeta> {
		let meta = await this.meta();
		if (!meta) {
			const { ats, slug } = parseBoardName(name);
			meta = {
				name,
				ats,
				slug,
				slotMs: hash32(name) % DAY,
				lastRunAt: null,
				lastOkAt: null,
				lastStatus: null,
				lastError: null,
				consecutiveFailures: 0,
				jobCount: 0,
				nextFetchAt: null,
				nextAlarmAt: null,
			};
		}
		const now = Date.now();
		if (meta.nextFetchAt === null) {
			meta.nextFetchAt = meta.lastRunAt === null ? now + (hash32(name + ":boot") % HOUR) : nextSlotAfter(now, meta.slotMs);
		}
		if ((await this.ctx.storage.getAlarm()) === null) await this.arm(meta);
		await this.ctx.storage.put("meta", meta);
		return meta;
	}

	private autoEnrich(): boolean {
		return (this.env.JOB_ENRICH as string) === "on" && !!this.env.OPENAI_KEY;
	}
	private autoEmbed(): boolean {
		return (this.env.EMBED as string) === "on" && !!this.env.OPENAI_KEY;
	}

	/** Set the single alarm to whichever is sooner: the next fetch, or a near-term backlog tick. */
	private async arm(meta: BoardMeta): Promise<void> {
		let at = meta.nextFetchAt ?? Date.now();
		const backlog =
			(this.autoEnrich() && this.hasPending(`enrich_status = 'pending' AND removed_at IS NULL`)) ||
			(this.autoEmbed() && this.hasPending(`removed_at IS NULL AND ${EMBED_TODO} AND (detail_status IN ('done','na','error') OR detail_status IS NULL)`, EMBED_TAG, Date.now() - DAY)) ||
			(!!fetchers[meta.ats]?.fetchDetail && this.hasPending(`removed_at IS NULL AND (detail_status = 'pending' OR detail_status IS NULL)`));
		if (backlog) {
			let tick = Date.now() + MINUTE;
			if (meta.embedBackoffUntil && meta.embedBackoffUntil > tick) tick = meta.embedBackoffUntil;
			if (meta.detailBackoffUntil && meta.detailBackoffUntil > tick) tick = meta.detailBackoffUntil; // the site asked us to wait
			at = Math.min(at, tick);
		}
		if (meta.snapshotDirty && meta.snapshotRetryAt) at = Math.min(at, meta.snapshotRetryAt);
		await this.ctx.storage.setAlarm(at);
		meta.nextAlarmAt = at;
	}

	async alarm(): Promise<void> {
		const meta = await this.meta();
		if (!meta) return; // never initialized; nothing to do
		const now = Date.now();
		if (meta.ats === "dark" && DARK_BOARD_MAX_OPEN > 0) {
			const open = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL`).one().n;
			if (open > DARK_BOARD_MAX_OPEN) {
				meta.lastError = `paused: ${open.toLocaleString("en-US")} open rows exceeds the per-board cap ${DARK_BOARD_MAX_OPEN.toLocaleString("en-US")} (national job board)`;
				meta.nextFetchAt = nextSlotAfter(now, meta.slotMs); meta.nextAlarmAt = meta.nextFetchAt;
				await this.ctx.storage.setAlarm(meta.nextFetchAt); await this.ctx.storage.put("meta", meta);
				return;
			}
		}
		if (meta.nextFetchAt !== null && now >= meta.nextFetchAt - 1000) {
			if (meta.localOnly) {
				meta.nextFetchAt = nextSlotAfter(now, meta.slotMs); // snapshots come from the laptop; just keep ticking
			} else if (isFresh(meta, DEFAULT_FRESH_MS, now)) {
				meta.nextFetchAt = nextSlotAfter(now, meta.slotMs); // fetched recently (e.g. forced); roll to next slot
			} else {
				await this.runFetch(meta);
			}
		}
		await this.runDetails(meta);
		await this.runEmbed(meta);
		await this.runEnrich(meta);
		await this.maybeSnapshot(meta);
		await this.arm(meta);
		await this.ctx.storage.put("meta", meta);
	}

	/**
	 * On-demand fetch used by the fleet-wide force-fetch sweep: arms the board if needed, then
	 * fetches unless the board is fresh (completed a non-error fetch within `freshMs`; 0 = always
	 * fetch). Does not change the daily slot.
	 * @returns whether a fetch actually ran
	 */
	async forceFetch(name: string, freshMs = DEFAULT_FRESH_MS): Promise<boolean> {
		const meta = await this.ensureScheduled(name);
		if (meta.localOnly || isFresh(meta, freshMs)) return false;
		await this.runFetch(meta);
		await this.runDetails(meta);
		await this.runEmbed(meta);
		await this.runEnrich(meta);
		await this.maybeSnapshot(meta);
		await this.arm(meta);
		await this.ctx.storage.put("meta", meta);
		return true;
	}

	/** Force a fetch (+ one enrichment batch) now. Admin/testing. */
	async fetchNow(): Promise<BoardMeta> {
		const meta = await this.meta();
		if (!meta) throw new Error("board not initialized; call ensureScheduled first");
		await this.runFetch(meta);
		await this.runDetails(meta);
		await this.runEmbed(meta);
		await this.runEnrich(meta);
		await this.maybeSnapshot(meta);
		await this.arm(meta);
		await this.ctx.storage.put("meta", meta);
		return meta;
	}

	/**
	 * Ingest a snapshot fetched outside Cloudflare (scripts/fetch-local.mjs --ingest). Runs the
	 * identical pipeline as an online fetch: diff, runs row, company resolution, then the
	 * detail/embed/enrich backlog on this board's own alarm ticks.
	 */
	async ingest(name: string, result: FetchResult | { status: "error"; error: string }): Promise<BoardMeta> {
		const meta = await this.ensureScheduled(name);
		meta.localOnly = true;
		await this.runFetch(meta, result);
		await this.runDetails(meta);
		await this.runEmbed(meta);
		await this.runEnrich(meta);
		await this.maybeSnapshot(meta);
		await this.arm(meta);
		await this.ctx.storage.put("meta", meta);
		return meta;
	}

	private async runFetch(meta: BoardMeta, provided?: FetchResult | { status: "error"; error: string }): Promise<void> {
		const fetcher = fetchers[meta.ats];
		const now = Date.now();
		meta.lastRunAt = now;
		try {
			if (!fetcher) throw new Error(`no fetcher for ats ${meta.ats}`);
			if (provided?.status === "error") throw new Error(provided.error);
			if (!provided && fetcher.fetchJobsStream) {
				// streaming path: pages are applied to SQLite as they arrive and never accumulate in memory
				const diff: Diff = { added: [], changed: [], removedIds: [], unchanged: 0 };
				const seen = new SeenSet(); // hashes only: the board is never held in memory, each page is looked up in SQLite
				const hasDetail = !!fetcher.fetchDetail;
				const res = await withTimeout(
					fetcher.fetchJobsStream(meta.slug, async (page) => {
						this.ctx.storage.transactionSync(() => this.applyPage(page, now, hasDetail, seen, diff));
					}),
					fetcher.fetchTimeoutMs ?? FETCH_JOBS_TIMEOUT_MS,
					`fetchJobsStream ${meta.name}`,
				);
				if (res.status === "gone") {
					meta.lastStatus = "gone"; meta.lastError = null; meta.consecutiveFailures++;
					this.recordRun(now, "gone", null, null);
				} else {
					// a partial walk (budget spent) keeps what it found but says nothing about the rest: no removal sweep
					if (!res.partial) this.ctx.storage.transactionSync(() => this.sweepUnseen(seen, now, diff));
					meta.lastStatus = "ok"; meta.lastOkAt = now; meta.lastError = res.partial ? "partial listing: discovery budget spent; unseen rows kept" : null; meta.consecutiveFailures = 0;
					meta.jobCount = res.partial ? Math.max(seen.size, meta.jobCount ?? 0) : seen.size;
					this.recordRun(now, "ok", diff, res.partial ? "partial" : null);
					this.markSnapshotDirty(meta, diff, now);
					await this.dedupeSeed(meta, diff);
				}
				let next0 = nextSlotAfter(now, meta.slotMs);
				if (meta.consecutiveFailures >= BACKOFF_AFTER) next0 += 6 * DAY;
				meta.nextFetchAt = next0;
				return;
			}
			const result = provided ?? (await withTimeout(fetcher.fetchJobs(meta.slug, { env: this.env }), fetcher.fetchTimeoutMs ?? FETCH_JOBS_TIMEOUT_MS, `fetchJobs ${meta.name}`));
			if (result.status === "gone") {
				meta.lastStatus = "gone";
				meta.lastError = null;
				meta.consecutiveFailures++;
				this.recordRun(now, "gone", null, null);
			} else {
				const diff = this.applySnapshot(result.jobs, now, !!fetcher.fetchDetail);
				await this.dedupeSeed(meta, diff);
				meta.lastStatus = "ok";
				meta.lastOkAt = now;
				meta.lastError = null;
				meta.consecutiveFailures = 0;
				meta.jobCount = result.jobs.length;
				this.recordRun(now, "ok", diff, null);
				this.markSnapshotDirty(meta, diff, now);
				if ((this.env.BOARD_ENRICH as string) === "on") await this.enrichBoard(meta, result.jobs, false);
			}
		} catch (e) {
			meta.lastStatus = "error";
			meta.lastError = e instanceof Error ? e.message : String(e);
			meta.consecutiveFailures++;
			this.recordRun(now, "error", null, meta.lastError);
		}
		let next = nextSlotAfter(now, meta.slotMs);
		if (meta.consecutiveFailures >= BACKOFF_AFTER) next += 6 * DAY; // weekly
		meta.nextFetchAt = next;
	}

	/**
	 * Diff the fetched snapshot against storage and apply it atomically.
	 * New jobs are marked `pending` for enrichment (enrichment is one-shot: already-enriched jobs
	 * are never re-queued, even when their content changes); jobs missing from the snapshot get
	 * `removed_at` set (kept for history); reappearing jobs are revived.
	 */
	/** Which of these ids does the board already hold, and in what state? One query per page instead of a map of the board. */
	private lookupExisting(ids: string[]): Map<string, { hash: string; removed: boolean }> {
		const out = new Map<string, { hash: string; removed: boolean }>();
		for (let i = 0; i < ids.length; i += 100) { // the object's SQLite allows 100 bound parameters per statement
			const chunk = ids.slice(i, i + 100);
			for (const r of this.ctx.storage.sql.exec<{ id: string; content_hash: string; removed_at: number | null }>(
				`SELECT id, content_hash, removed_at FROM jobs WHERE id IN (${chunk.map(() => "?").join(",")})`, ...chunk,
			)) out.set(r.id, { hash: r.content_hash, removed: r.removed_at !== null });
		}
		return out;
	}

	/** After a complete walk: every open row the walk did not see is gone from the board. */
	private sweepUnseen(seen: SeenSet, now: number, diff: Diff): void {
		const gone: string[] = [];
		for (const r of this.ctx.storage.sql.exec<{ id: string }>(`SELECT id FROM jobs WHERE removed_at IS NULL`)) if (!seen.has(r.id)) gone.push(r.id);
		for (const id of gone) { this.ctx.storage.sql.exec(`UPDATE jobs SET removed_at = ? WHERE id = ?`, now, id); diff.removedIds.push(id); }
	}

	private applySnapshot(jobs: Job[], now: number, hasDetail: boolean): Diff {
		const diff: Diff = { added: [], changed: [], removedIds: [], unchanged: 0 };
		this.ctx.storage.transactionSync(() => {
			const seen = new SeenSet();
			for (let i = 0; i < jobs.length; i += 500) this.applyPage(jobs.slice(i, i + 500), now, hasDetail, seen, diff);
			this.sweepUnseen(seen, now, diff);
		});
		return diff;
	}

	/** Upsert one page of the snapshot; diff bookkeeping shared with the streaming path. */
	private applyPage(jobs: Job[], now: number, hasDetail: boolean, seen: SeenSet, diff: Diff): void {
		{
			const sql = this.ctx.storage.sql;
			const fresh = jobs.filter((j) => !seen.has(j.id)); // provider duplicated a job in its listing, or an earlier page had it
			const existing = this.lookupExisting(fresh.map((j) => j.id));
			for (const job of fresh) {
				if (seen.has(job.id)) continue;
				seen.add(job.id);
				const hash = contentHash(job);
				const prev = existing.get(job.id);
				if (!prev) {
					sql.exec(
						`INSERT INTO jobs (id, data, content_hash, first_seen_at, last_seen_at, changed_at, enrich_status, detail_status)
						 VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)`,
						job.id,
						JSON.stringify(job),
						hash,
						now,
						now,
						now,
						hasDetail && (job.content ?? "").length < DETAIL_MIN_CONTENT ? "pending" : "na",
					);
					diff.added.push(job);
				} else if (!this.sameContent(prev.hash, hash, job.id)) {
					// Content changed. Enrichment is one-shot: a job that is already 'done' is never re-enriched;
					// only never-enriched jobs go (back) to 'pending'.
					sql.exec(
						`UPDATE jobs SET data = ?, content_hash = ?, last_seen_at = ?, changed_at = ?, removed_at = NULL,
						 enrich_status = CASE WHEN enrich_status = 'done' THEN 'done' ELSE 'pending' END,
						 enrich_error = CASE WHEN enrich_status = 'done' THEN enrich_error ELSE NULL END
						 WHERE id = ?`,
						JSON.stringify(job),
						hash,
						now,
						now,
						job.id,
					);
					diff.changed.push(job);
				} else if (prev.removed) {
					sql.exec(`UPDATE jobs SET last_seen_at = ?, removed_at = NULL WHERE id = ?`, now, job.id);
					diff.added.push(job); // re-listed after removal
				} else {
					diff.unchanged++; // no write: an open row's last-seen is the board's lastOkAt (see rowToJob)
				}
			}
		}
	}

	/**
	 * Is the stored row the same posting? A legacy whole-JSON hash never equals a new one, so re-hash the stored
	 * row the new way before calling it a change; when it matches, upgrade the stored hash (one metered row,
	 * no changed_at bump) so the next fetch compares directly.
	 */
	private sameContent(prevHash: string, hash: string, id: string): boolean {
		if (prevHash === hash) return true;
		if (prevHash.startsWith("s")) return false;
		const row = this.ctx.storage.sql.exec<{ data: string }>(`SELECT data FROM jobs WHERE id = ?`, id).toArray()[0];
		if (!row || contentHash(JSON.parse(row.data) as Job) !== hash) return false;
		this.ctx.storage.sql.exec(`UPDATE jobs SET content_hash = ? WHERE id = ?`, hash, id);
		return true;
	}

	private recordRun(runAt: number, status: RunSummary["status"], diff: Diff | null, error: string | null): void {
		this.ctx.storage.sql.exec(
			`INSERT INTO runs (run_at, status, added, changed, removed, unchanged, error) VALUES (?, ?, ?, ?, ?, ?, ?)`,
			runAt,
			status,
			diff?.added.length ?? 0,
			diff?.changed.length ?? 0,
			diff?.removedIds.length ?? 0,
			diff?.unchanged ?? 0,
			error,
		);
	}

	/** After a fetch: claim the new postings' keys; the first time, every open posting's (one-time seed of the index). */
	private async dedupeSeed(meta: BoardMeta, diff: Diff): Promise<void> {
		if (meta.ats === "dark") return;
		if (meta.dedupeSeeded) { await this.dedupeClaimFirstParty(meta, diff.added); return; }
		const jobs = this.ctx.storage.sql.exec<{ data: string }>(`SELECT data FROM jobs WHERE removed_at IS NULL`).toArray().map((r) => JSON.parse(r.data) as Job);
		await this.dedupeClaimFirstParty(meta, jobs);
		meta.dedupeSeeded = true;
	}

	/** Claim dedup keys for this board's postings (batched per shard). Returns the existing owner for duplicates, else null. */
	private async dedupeClaim(entries: { key: string; owner: string }[], firstParty: boolean): Promise<(string | null)[]> {
		const out: (string | null)[] = new Array(entries.length).fill(null);
		const byShard = new Map<number, number[]>();
		entries.forEach((e, i) => { const sh = dedupeShard(e.key); (byShard.get(sh) ?? byShard.set(sh, []).get(sh)!).push(i); });
		await Promise.all([...byShard].map(async ([sh, idx]) => {
			try {
				const res = await this.env.DEDUPE.getByName(`dedupe:${sh}`).claim(idx.map((i) => entries[i]), firstParty);
				idx.forEach((i, j) => { out[i] = res[j]; });
			} catch { /* best effort: a missed claim just means consolidation dedups it later */ }
		}));
		return out;
	}

	/** First-party: claim the keys of these postings (the employer's own listing wins over any job-board copy). */
	private async dedupeClaimFirstParty(meta: BoardMeta, jobs: Job[]): Promise<void> {
		if (meta.ats === "dark" || !jobs.length) return;
		const company = meta.company?.name ?? deriveCandidates(meta.slug, []).candidate_name;
		const entries: { key: string; owner: string }[] = [];
		for (const j of jobs) { const k = dedupeKey(company, j.title, j.location); if (k) entries.push({ key: k, owner: `${meta.ats}/${meta.slug}#${j.id}` }); }
		for (let i = 0; i < entries.length; i += 500) await this.dedupeClaim(entries.slice(i, i + 500), true);
	}

	/** Is there at least one row matching `where`? One index probe instead of a COUNT over the whole backlog. */
	private hasPending(where: string, ...params: SqlStorageValue[]): boolean {
		return this.ctx.storage.sql.exec<{ x: number }>(`SELECT 1 AS x FROM jobs WHERE ${where} LIMIT 1`, ...params).toArray().length > 0;
	}

	private pendingDetailCount(): number {
		return this.ctx.storage.sql
			.exec<{ n: number }>(
				`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL AND (detail_status = 'pending' OR detail_status IS NULL)`,
			)
			.one().n;
	}

	/**
	 * Fetch full postings for jobs whose listing lacked a description (providers with fetchDetail). Once per job;
	 * `error` rows are retried after a day; NULL status predates this stage and counts as pending. Pacing is
	 * adaptive (see DETAIL_* above): as hard as the site allows, backing off on its signals.
	 */
	private async runDetails(meta: BoardMeta): Promise<void> {
		const fetcher = fetchers[meta.ats];
		if (!fetcher?.fetchDetail) return;
		if (meta.detailBackoffUntil && meta.detailBackoffUntil > Date.now()) return; // the site asked us to wait
		const start = Date.now(); const deadline = start + DETAIL_TICK_WALL_MS;
		let conc = Math.min(DETAIL_CONC_MAX, Math.max(DETAIL_CONC_MIN, meta.detailConc ?? DETAIL_CONC_START));
		let used = 0, okStreak = 0, walls = 0, timeouts = 0, stop: string | null = null, oks = 0, errs = 0, latSum = 0, latN = 0;
		const backoff = (ms: number, why: string) => { conc = Math.max(DETAIL_CONC_MIN, Math.floor(conc / 2)); meta.detailBackoffUntil = Date.now() + ms; stop = why; };
		while (!stop && Date.now() < deadline && used < DETAIL_TICK_SUBREQUESTS) {
			const rows = this.ctx.storage.sql
				.exec<JobRow>(
					`SELECT * FROM jobs WHERE removed_at IS NULL
					   AND (detail_status = 'pending' OR detail_status IS NULL OR (detail_status = 'error' AND detail_fetched_at < ?))
					 LIMIT ?`,  // no ORDER BY: sorting a 100k-row backlog for every small batch was billions of rows read a day
					start - DAY,
					Math.min(conc * 4, DETAIL_TICK_SUBREQUESTS - used),
				)
				.toArray();
			if (rows.length === 0) break;
			const queue = [...rows];
			const worker = async () => {
				for (;;) {
					if (stop || Date.now() >= deadline) return;
					const r = queue.shift();
					if (!r) return;
					const job = JSON.parse(r.data) as Job;
					if ((job.content ?? "").length >= DETAIL_MIN_CONTENT) {
						this.ctx.storage.sql.exec(`UPDATE jobs SET detail_status = 'na' WHERE id = ?`, r.id);
						continue;
					}
					used++;
					const t0 = Date.now();
					try {
						const d = await withTimeout(fetcher.fetchDetail!(meta.slug, job), FETCH_DETAIL_TIMEOUT_MS, `fetchDetail ${meta.name}/${r.id}`);
						latSum += Date.now() - t0; latN++; oks++;
						// a job-board posting asks the dedup index before its text and vector are kept: a copy of something
						// already claimed (an employer's own listing, or an earlier board) is stored slim and never embedded
						let dupOf: string | null = null;
						if (d && meta.ats === "dark") {
							const k = dedupeKey(d.org, d.title ?? job.title, d.location);
							if (k) dupOf = (await this.dedupeClaim([{ key: k, owner: `${meta.ats}/${meta.slug}#${r.id}` }], false))[0];
						}
						if (dupOf) {
							const slim = { title: d!.title, location: d!.location, publishedAt: d!.publishedAt, org: d!.org, content: null };
							this.ctx.storage.sql.exec(`UPDATE jobs SET detail_status = 'done', detail = ?, detail_error = NULL, detail_fetched_at = ?, embed_status = 'dup', dup_of = ? WHERE id = ?`, JSON.stringify(slim), Date.now(), dupOf, r.id);
							continue;
						}
						this.ctx.storage.sql.exec(
							`UPDATE jobs SET detail_status = ?, detail = ?, detail_error = NULL, detail_fetched_at = ? WHERE id = ?`,
							d ? "done" : "na",
							d ? JSON.stringify(d) : null,
							Date.now(),
							r.id,
						);
						walls = 0; timeouts = 0;
						// Slowing under our load is the site's way of saying "less". The reference is this board's own best
						// mean latency, so a site that is simply slow keeps its concurrency; one that gets 2.5x slower than
						// its best halves, and one answering near its best grows.
						if (++okStreak % DETAIL_GROW_EVERY === 0) {
							const mean = latSum / Math.max(1, latN);
							const base = Math.min(meta.detailBaseMs ?? mean, mean); meta.detailBaseMs = base;
							if (mean > 2.5 * base && mean > 1000) conc = Math.max(DETAIL_CONC_MIN, Math.floor(conc / 2));
							else if (mean < 1.3 * base && conc < DETAIL_CONC_MAX) conc = Math.min(DETAIL_CONC_MAX, Math.ceil(conc * 1.5));
							latSum = 0; latN = 0;
						}
					} catch (e) {
						const msg = e instanceof Error ? e.message : String(e);
						const status = e instanceof HttpError ? e.status : 0;
						okStreak = 0; errs++;
						if (status === 429 || status === 503 || status === 502 || status === 504) {
							// the site is pacing us: leave the row pending, halve, and sleep until Retry-After (or a minute, doubling per wall)
							const ra = Number(/retry-after=(\d+)/.exec(msg)?.[1]);
							backoff(Number.isFinite(ra) && ra > 0 ? Math.min(ra * 1000, 15 * MINUTE) : Math.min(15 * MINUTE, MINUTE * 2 ** Math.min(4, ++walls)), `HTTP ${status}`);
							return;
						}
						if (status === 403 || status === 401) {
							if (++walls >= 5) { backoff(HOUR, `bot wall (${walls} x HTTP ${status})`); return; } // a run of 403s is a wall, not a page
						} else if (/timed out|timeout/i.test(msg)) {
							if (++timeouts >= 5) { backoff(10 * MINUTE, `${timeouts} timeouts`); return; }
							conc = Math.max(DETAIL_CONC_MIN, Math.floor(conc / 2)); // load, not a wall: ease off, keep going
						}
						this.ctx.storage.sql.exec(
							`UPDATE jobs SET detail_status = 'error', detail_error = ?, detail_fetched_at = ? WHERE id = ?`,
							msg,
							Date.now(),
							r.id,
						);
					}
				}
			};
			await Promise.all(Array.from({ length: Math.min(conc, queue.length) }, worker));
		}
		meta.detailConc = conc;
		meta.detailTick = { at: start, fetched: used, ok: oks, errors: errs, wallMs: Date.now() - start, meanMs: Math.round(latSum / Math.max(1, latN)), conc };
		if (stop) meta.lastError = `details paused: ${stop}; concurrency now ${conc}`;
	}

	private pendingCount(): number {
		return this.ctx.storage.sql
			.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE enrich_status = 'pending' AND removed_at IS NULL`)
			.one().n;
	}

	/** Enrich one batch of pending (live) jobs. Automatic path; gated by JOB_ENRICH. */
	private async runEnrich(meta: BoardMeta): Promise<void> {
		if (!this.autoEnrich() || !enricher) return;
		// Jobs still waiting for their detail fetch are not enriched yet (the JD body is the main input).
		const rows = this.ctx.storage.sql
			.exec<JobRow>(
				`SELECT * FROM jobs WHERE enrich_status = 'pending' AND removed_at IS NULL
				   AND (detail_status IN ('done', 'na') OR (detail_status IS NULL AND ?))
				 ORDER BY changed_at LIMIT ?`,
				fetchers[meta.ats]?.fetchDetail ? 0 : 1,
				ENRICH_BATCH,
			)
			.toArray();
		if (rows.length === 0) return;
		const jobs = rows.map((r) => rowToJob(r) as Job);
		const now = Date.now();
		let results: (unknown | null)[];
		try {
			results = await this.enrichBatch(jobs, meta);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			for (const r of rows) {
				this.ctx.storage.sql.exec(`UPDATE jobs SET enrich_status = 'error', enrich_error = ? WHERE id = ?`, msg, r.id);
			}
			return;
		}
		for (let i = 0; i < rows.length; i++) {
			const payload = results[i];
			if (payload === null || payload === undefined) continue; // stays pending
			this.ctx.storage.sql.exec(
				`UPDATE jobs SET enrich_status = 'done', enriched_at = ?, enrichment = ?, enrich_error = NULL WHERE id = ?`,
				now,
				JSON.stringify(payload),
				rows[i].id,
			);
		}
		void meta;
	}

	/**
	 * Resolve the company behind this board (one-shot). Skipped when already resolved unless
	 * `force`; skipped when the board has no jobs or no OPENAI_KEY. Errors are recorded on meta
	 * and retried on the next successful fetch (daily), never in a tight loop.
	 */
	private async enrichBoard(meta: BoardMeta, jobs: Job[], force: boolean): Promise<void> {
		if (!this.env.OPENAI_KEY) return;
		if (meta.company && !force) return;
		if (jobs.length === 0) return;
		meta.companyAttemptedAt = Date.now();
		try {
			meta.company = await resolveCompany(this.env, { ats: meta.ats, slug: meta.slug, jobs });
			meta.companyError = null;
		} catch (e) {
			meta.companyError = e instanceof Error ? e.message : String(e);
		}
	}

	/** Admin: run (or re-run with force) the company resolution now. */
	async enrichBoardNow(force = false): Promise<BoardMeta> {
		const meta = await this.meta();
		if (!meta) throw new Error("board not initialized; call ensureScheduled first");
		const jobs = (await this.getJobs({ status: "open" })).map((j) => j as Job);
		await this.enrichBoard(meta, jobs, force);
		await this.ctx.storage.put("meta", meta);
		return meta;
	}

	/** Run the structured-output extraction for a set of jobs (ENRICH_CONCURRENCY in flight). */
	private async enrichBatch(jobs: Job[], meta: BoardMeta): Promise<(JobEnrichment | null)[]> {
		const out: (JobEnrichment | null)[] = new Array(jobs.length).fill(null);
		const ctx = { ats: meta.ats, company: meta.company?.name ?? null };
		let i = 0;
		await Promise.all(
			Array.from({ length: ENRICH_CONCURRENCY }, async () => {
				for (;;) {
					const k = i++;
					if (k >= jobs.length) return;
					out[k] = await enrichOne(this.env, jobs[k], ctx);
				}
			}),
		);
		return out;
	}

	/**
	 * Lazy enrichment: enrich the given job ids now and return their payloads. Idempotent — jobs
	 * already `done` are returned from storage unless `force`. Unknown ids are reported as such.
	 */
	async enrichJobs(ids: string[], force = false, dryRun = false): Promise<EnrichJobsResult> {
		const meta = await this.meta();
		if (!meta) throw new Error("board not initialized");
		let costUsd = 0;
		const companyCached = !!meta.company;
		// Resolve the company first (one-shot, cached on meta) so job extraction gets it as context.
		if (!meta.company && this.env.OPENAI_KEY && !dryRun) {
			const live = (await this.getJobs({ status: "open", jobLimit: 5 })).map((j) => j as Job);
			await this.enrichBoard(meta, live, false);
			const c = meta.company as CompanyEnrichment | null | undefined;
			if (c) costUsd += usd(c.usage, c.searches?.length ?? 0);
			await this.ctx.storage.put("meta", meta);
		}
		const result: Record<string, JobEnrichResult> = {};
		const todo: JobRow[] = [];
		for (const id of ids) {
			const r = this.ctx.storage.sql.exec<JobRow>(`SELECT * FROM jobs WHERE id = ?`, id).toArray()[0];
			if (!r) {
				result[id] = { status: "unknown" };
				continue;
			}
			if (r.enrich_status === "done" && r.enrichment && !force) {
				result[id] = { status: "done", enrichment: JSON.parse(r.enrichment), cached: true };
				continue;
			}
			todo.push(r);
		}
		const wrap = (): EnrichJobsResult => ({
			company: meta.company ?? null,
			companyError: meta.companyError ?? null,
			companyCached,
			jobs: result,
			todo: todo.map((r) => r.id),
			costUsd,
		});
		if (todo.length === 0 || dryRun) return wrap();
		if (!this.env.OPENAI_KEY) throw new Error("OPENAI_KEY secret not set");
		const jobs = todo.map((r) => rowToJob(r) as Job);
		const ctx = { ats: meta.ats, company: meta.company?.name ?? null };
		let i = 0;
		await Promise.all(
			Array.from({ length: ENRICH_CONCURRENCY }, async () => {
				for (;;) {
					const k = i++;
					if (k >= jobs.length) return;
					const id = todo[k].id;
					try {
						const e = await enrichOne(this.env, jobs[k], ctx);
						this.ctx.storage.sql.exec(
							`UPDATE jobs SET enrich_status = 'done', enriched_at = ?, enrichment = ?, enrich_error = NULL WHERE id = ?`,
							e.enriched_at,
							JSON.stringify(e),
							id,
						);
						result[id] = { status: "done", enrichment: e };
						costUsd += usd(e.usage);
					} catch (err) {
						const msg = err instanceof Error ? err.message : String(err);
						this.ctx.storage.sql.exec(`UPDATE jobs SET enrich_status = 'error', enrich_error = ? WHERE id = ?`, msg, id);
						result[id] = { status: "error", error: msg };
					}
				}
			}),
		);
		return wrap();
	}

	private pendingEmbedCount(): number {
		return this.ctx.storage.sql
			.exec<{ n: number }>(
				`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL
				   AND ${EMBED_TODO}
				   AND (detail_status IN ('done','na','error') OR detail_status IS NULL)`,
				EMBED_TAG, Date.now() - DAY,
			)
			.one().n;
	}

	/** Everything we know about the job, as labelled lines, then the full JD text (capped). */
	private embedText(job: Job, meta: BoardMeta): string {
		return clampTokens(this.embedTextRaw(job, meta), EMBED_MAX_TOKENS);
	}

	private embedTextRaw(job: Job, meta: BoardMeta): string {
		const company = meta.company?.name ?? deriveCandidates(meta.slug, []).candidate_name;
		const lines = [
			`Company: ${company}`,
			`Job title: ${job.title}`,
			job.location ? `Location: ${job.location}` : "",
			job.departments.length ? `Department: ${job.departments.join(", ")}` : "",
			job.publishedAt ? `Posted: ${job.publishedAt.slice(0, 10)}` : "",
			meta.company?.industry ? `Industry: ${meta.company.industry}` : "",
			meta.company?.is_staffing_agency ? "Posted by a staffing agency" : "",
			`Source: ${meta.ats}`,
			"",
			jdText(job).slice(0, EMBED_TEXT_CHARS),
		];
		return lines.filter((l) => l !== "").join("\n");
	}

	/**
	 * Embed one batch of live jobs that have no embedding yet (one-shot per job; gated by EMBED).
	 * Waits for a job's detail fetch so the JD body is included. Errors retry on later ticks.
	 */
	private async runEmbed(meta: BoardMeta): Promise<void> {
		if (!this.autoEmbed()) return;
		// Several batches per tick while there is backlog (giant boards would otherwise drain ~100/min),
		// bounded by wall time so the alarm stays well inside its limit. Detail-errored jobs embed from
		// their listing text rather than waiting a day for the detail retry.
		const deadline = Date.now() + 40_000;
		for (let i = 0; i < 12 && Date.now() < deadline; i++) {
			if (meta.embedBackoffUntil && meta.embedBackoffUntil > Date.now()) return;
			const rows = this.ctx.storage.sql
				.exec<JobRow>(
					`SELECT * FROM jobs WHERE removed_at IS NULL
					   AND ${EMBED_TODO}
					   AND (detail_status IN ('done','na','error') OR detail_status IS NULL)
					 LIMIT ?`,  // no ORDER BY (see runDetails); error rows are already pushed a day out by EMBED_TODO
					EMBED_TAG, Date.now() - DAY,
					EMBED_BATCH,
				)
				.toArray();
			if (rows.length === 0) return;
			await this.embedRows(rows, meta);
		}
	}

	private async embedRows(allRows: JobRow[], meta: BoardMeta, depth = 0): Promise<void> {
		// Trim the batch to the request char budget (always at least one row).
		const rows: JobRow[] = [];
		const texts: string[] = [];
		let chars = 0;
		for (const r of allRows) {
			const t = this.embedText(rowToJob(r) as Job, meta);
			if (rows.length > 0 && chars + t.length > EMBED_BATCH_CHARS) break;
			rows.push(r);
			texts.push(t);
			chars += t.length;
		}
		try {
			const { vectors, headroom, resetMs } = await embedTexts(this.env, texts);
			// Leave EMBED_HEADROOM of the org's per-minute budget to interactive /embed callers: when this call saw less
			// than that left, this board sleeps until the window resets (plus jitter so the fleet does not wake at once).
			meta.embedBackoffUntil = headroom < EMBED_HEADROOM ? Date.now() + Math.max(resetMs, 5_000) + Math.floor(Math.random() * 10_000) : null;
			this.ctx.storage.transactionSync(() => {
				for (let i = 0; i < rows.length; i++) {
					this.ctx.storage.sql.exec(
						`UPDATE jobs SET embedding = ?, embed_model = ?, embed_status = 'done', embed_error = NULL WHERE id = ?`,
						vectors[i].buffer,
						EMBED_TAG,
						rows[i].id,
					);
				}
			});
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			if (/\b429\b|rate limit/i.test(msg)) {
				// Org-wide TPM limit shared by every board: leave rows pending and back this board off with jitter.
				meta.embedBackoffUntil = Date.now() + MINUTE + Math.floor(Math.random() * 4 * MINUTE);
				return;
			}
			const now = Date.now();
			const bad = /input\[(\d+)\]/.exec(msg); // OpenAI names the offending input: park that row, embed the rest now
			const k = bad ? Number(bad[1]) : -1;
			if (k >= 0 && k < rows.length && rows.length > 1 && depth < 8) {
				this.ctx.storage.sql.exec(`UPDATE jobs SET embed_status = 'error', embed_error = ?, embed_tried_at = ? WHERE id = ?`, msg, now, rows[k].id);
				return this.embedRows(rows.filter((_, i) => i !== k), meta, depth + 1);
			}
			for (const r of rows) this.ctx.storage.sql.exec(`UPDATE jobs SET embed_status = 'error', embed_error = ?, embed_tried_at = ? WHERE id = ?`, msg, now, r.id);
		}
	}

	/**
	 * Backfill kick (fleet sweep): if this board has any detail/embed/enrich backlog, fire the alarm
	 * now so the minute-tick loop drains it. Returns the backlog sizes. Cheap and idempotent.
	 */
	async kick(name: string): Promise<{ details: number; embeds: number; enrich: number; kicked: boolean }> {
		const meta = await this.ensureScheduled(name);
		const details = fetchers[meta.ats]?.fetchDetail ? this.pendingDetailCount() : 0;
		const embeds = this.autoEmbed() ? this.pendingEmbedCount() : 0;
		const enrich = this.autoEnrich() ? this.pendingCount() : 0;
		const kicked = details + embeds + enrich > 0;
		if (kicked) {
			await this.ctx.storage.setAlarm(Date.now());
			meta.nextAlarmAt = Date.now();
			await this.ctx.storage.put("meta", meta);
		}
		return { details, embeds, enrich, kicked };
	}

	/** Diagnostic: meta + recent runs + backlog counts (temporary). */
	async debugState(): Promise<unknown> {
		const meta = await this.meta();
		const runs = this.ctx.storage.sql.exec(`SELECT * FROM runs ORDER BY id DESC LIMIT 12`).toArray();
		const nRuns24h = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM runs WHERE run_at > ?`, Date.now() - 86_400_000).one().n;
		const jobs = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL`).one().n;
		const embedErrors = this.ctx.storage.sql.exec(`SELECT embed_status, substr(embed_error, 1, 160) AS err, COUNT(*) AS n, MAX(length(data)) AS max_data FROM jobs WHERE removed_at IS NULL AND embed_status != 'done' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 6`).toArray();
		const detailErrors = this.ctx.storage.sql.exec(`SELECT detail_status, substr(detail_error, 1, 90) AS err, COUNT(*) AS n FROM jobs WHERE removed_at IS NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8`).toArray();
		const dups = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE embed_status = 'dup'`).one().n;
		return { dups, detailErrors, embedErrors, meta, nRuns24h, jobs, pendingDetail: this.pendingDetailCount(), pendingEmbed: this.pendingEmbedCount(), pendingEnrich: this.pendingCount(), alarm: await this.ctx.storage.getAlarm(), runs };
	}

	/** Diagnostic: metered rows written per statement shape, on a scratch row (temporary). */
	async rowMeter(): Promise<Record<string, number>> {
		const sql = this.ctx.storage.sql; const out: Record<string, number> = {}; const now = Date.now();
		const big = "x".repeat(25_000); const small = "x".repeat(500);
		const w = (k: string, q: string, ...a: SqlStorageValue[]) => { out[k] = sql.exec(q, ...a).rowsWritten; };
		w("insert_big", `INSERT INTO jobs (id, data, content_hash, first_seen_at, last_seen_at, changed_at, enrich_status, detail_status) VALUES ('t1', ?, 'h', ?, ?, ?, 'pending', 'pending')`, big, now, now, now);
		w("insert_small", `INSERT INTO jobs (id, data, content_hash, first_seen_at, last_seen_at, changed_at, enrich_status, detail_status) VALUES ('t2', ?, 'h', ?, ?, ?, 'pending', 'pending')`, small, now, now, now);
		w("seen_big", `UPDATE jobs SET last_seen_at = ?, removed_at = NULL WHERE id = 't1'`, now + 1);
		w("seen_small", `UPDATE jobs SET last_seen_at = ?, removed_at = NULL WHERE id = 't2'`, now + 1);
		w("removed_big", `UPDATE jobs SET removed_at = ? WHERE id = 't1'`, now);
		w("relist_big", `UPDATE jobs SET last_seen_at = ?, removed_at = NULL WHERE id = 't1'`, now + 2);
		w("detail_big", `UPDATE jobs SET detail_status = 'done', detail = ?, detail_error = NULL, detail_fetched_at = ? WHERE id = 't1'`, "y".repeat(20_000), now);
		w("embed_big", `UPDATE jobs SET embedding = ?, embed_model = 'm', embed_status = 'done', embed_error = NULL WHERE id = 't1'`, new Uint8Array(6144).buffer);
		w("enrich_big", `UPDATE jobs SET enrich_status = 'done', enriched_at = ?, enrichment = ?, enrich_error = NULL WHERE id = 't1'`, now, "z".repeat(2000));
		w("small_col_big", `UPDATE jobs SET enrich_error = 'e' WHERE id = 't1'`);
		w("data_rewrite_big", `UPDATE jobs SET data = ?, content_hash = 'h2', last_seen_at = ?, changed_at = ?, removed_at = NULL WHERE id = 't1'`, big + "1", now, now);
		w("run_insert", `INSERT INTO runs (run_at, status, added, changed, removed, unchanged, error) VALUES (?, 'ok', 0, 0, 0, 0, NULL)`, now);
		w("delete_two", `DELETE FROM jobs WHERE id IN ('t1','t2')`);
		w("delete_run", `DELETE FROM runs WHERE run_at = ?`, now);
		return out;
	}

	/** Admin: embed everything pending on this board now (all batches), regardless of the EMBED var. */
	async embedNow(): Promise<{ embedded: number }> {
		if (!this.env.OPENAI_KEY) throw new Error("OPENAI_KEY secret not set");
		const meta = await this.meta();
		if (!meta) throw new Error("board not initialized");
		let embedded = 0;
		for (;;) {
			const rows = this.ctx.storage.sql
				.exec<JobRow>(
					`SELECT * FROM jobs WHERE removed_at IS NULL
					   AND (embed_status IS NULL OR embed_status IN ('pending', 'error') OR embed_model != ?)
					 ORDER BY first_seen_at LIMIT ?`,
					EMBED_TAG,
					EMBED_BATCH,
				)
				.toArray();
			if (rows.length === 0) break;
			await this.embedRows(rows, meta);
			if (meta.embedBackoffUntil) break; // rate limited; the alarm loop will finish it
			embedded += rows.length;
		}
		await this.ctx.storage.put("meta", meta);
		return { embedded };
	}

	/** Reset errored enrichments to pending (admin). */
	async retryEnrichment(): Promise<number> {
		const n = this.ctx.storage.sql
			.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE enrich_status = 'error'`)
			.one().n;
		this.ctx.storage.sql.exec(`UPDATE jobs SET enrich_status = 'pending', enrich_error = NULL WHERE enrich_status = 'error'`);
		const meta = await this.meta();
		if (meta && n > 0) {
			await this.arm(meta);
			await this.ctx.storage.put("meta", meta);
		}
		return n;
	}

	async getMeta(): Promise<BoardMeta | null> {
		return (await this.meta()) ?? null;
	}

	/**
	 * @param opts.status  "open" (default: all) = still listed on the board; "removed" = no longer listed; "all"
	 * @param opts.enrich  filter by enrichment status
	 * @param opts.since   only jobs seen/changed/removed at or after this epoch-ms (incremental pulls)
	 * @param opts.slim    drop `raw` and `content` from each job (much smaller payloads)
	 */
	async getJobs(opts: JobQuery = {}): Promise<StoredJob[]> {
		const floor = (await this.meta())?.lastOkAt ?? 0;
		const where: string[] = [];
		const params: SqlStorageValue[] = [];
		if (opts.status === "open") where.push(`removed_at IS NULL`);
		else if (opts.status === "removed") where.push(`removed_at IS NOT NULL`);
		if (opts.enrich) {
			where.push(`enrich_status = ?`);
			params.push(opts.enrich);
		}
		if (opts.since !== undefined) {
			// open rows are not rewritten per fetch: if the board fetched OK since `since`, every open job was seen then
			where.push(floor >= opts.since ? `(removed_at IS NULL OR last_seen_at >= ? OR changed_at >= ? OR removed_at >= ?)` : `(last_seen_at >= ? OR changed_at >= ? OR removed_at >= ?)`);
			params.push(opts.since, opts.since, opts.since);
		}
		if (opts.ids?.length) {
			where.push(`id IN (${opts.ids.map(() => "?").join(",")})`);
			params.push(...opts.ids);
		}
		let sql = `SELECT * FROM jobs ${where.length ? "WHERE " + where.join(" AND ") : ""} ORDER BY id`;
		if (opts.jobLimit !== undefined) {
			sql += ` LIMIT ? OFFSET ?`;
			params.push(opts.jobLimit, opts.jobOffset ?? 0);
		}
		const jobs = this.ctx.storage.sql.exec<JobRow>(sql, ...params).toArray().map((r) => rowToJob(r, opts.embed, floor));
		if (opts.slim) for (const j of jobs) { j.raw = undefined; j.content = null; }
		if (!opts.detailRaw) for (const j of jobs) j.detailRaw = null;
		return jobs;
	}

	/** One-row lookup for /probe: by provider id, exact URL, or URL suffix (workday ids are URL paths). */
	async findJob(id: string | null, url: string | null): Promise<StoredJob | null> {
		const clean = (u: string) => u.toLowerCase().replace(/\/+$/, "").replace(/^https?:\/\/(www\.)?/, "");
		// exact id first; a substring match on the id only when it's long enough to be unambiguous (workday ids are URL paths)
		let r: JobRow | null = id ? this.ctx.storage.sql.exec<JobRow>(`SELECT * FROM jobs WHERE id = ?1 LIMIT 1`, id).toArray()[0] ?? null : null;
		if (!r && id && id.length >= 8) r = this.ctx.storage.sql.exec<JobRow>(`SELECT * FROM jobs WHERE instr(json_extract(data, '$.url'), ?1) > 0 LIMIT 1`, id).toArray()[0] ?? null;
		if (!r && url) {
			const want = clean(url);
			const tail = url.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/+$/, "");
			r = this.ctx.storage.sql.exec<JobRow>(`SELECT * FROM jobs WHERE instr(json_extract(data, '$.url'), ?1) > 0 LIMIT 5`, tail).toArray().find((x) => clean((JSON.parse(x.data) as Job).url) === want) ?? null;
		}
		if (!r) return null;
		const j = rowToJob(r, true, (await this.meta())?.lastOkAt ?? 0); j.raw = undefined; j.content = null; j.detailRaw = null; return j;
	}

	/** Drop every job row and reset counters; the next fetch repopulates from scratch. Recovery for
	 * boards whose stored rows grew pathological (e.g. fat detail raw written by an older fetcher). */
	/** Flag the open set as changed so the next eligible tick writes a fresh R2 snapshot. */
	private markSnapshotDirty(meta: BoardMeta, diff: Diff, now: number): void {
		if (diff.added.length || diff.changed.length || diff.removedIds.length) {
			meta.snapshotDirty = true;
			meta.dirtySince ??= now;
		}
	}

	/**
	 * Write the R2 parquet snapshot of current open jobs when (a) the open set changed since the last
	 * snapshot AND (b) the embed backlog has drained — so each change writes once, complete with
	 * vectors. Escape hatch: if the board has been dirty for >48h (stuck/erroring embeds, API outage),
	 * write anyway; the next successful drain rewrites it. Failures never break the pipeline.
	 */
	private async maybeSnapshot(meta: BoardMeta, force = false): Promise<void> {
		if (!force && !meta.snapshotDirty && meta.snapshotAt != null) return;
		const now = Date.now();
		const embedsDone = !this.autoEmbed() || this.pendingEmbedCount() === 0;
		const stale = meta.dirtySince != null && now - meta.dirtySince > SNAPSHOT_STALE_MS;
		if (!force && !embedsDone && !stale) return; // wait for vectors; a backlog tick will retry
		if (!force && (await this.env.LOCK.getByName("consolidate").snapshotsFrozen())) {
			meta.snapshotRetryAt = now + 10 * MINUTE; // the publisher is reading snapshots right now; rewrite ours after
			return;
		}
		meta.snapshotRetryAt = null;
		try {
			const open = this.ctx.storage.sql
				.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL`).one().n;
			if (open === 0) {
				// no current openings: remove the object(s) so consolidation doesn't see a stale board
				for (let p = 0; p < Math.max(1, meta.snapshotParts ?? 1); p++) await this.env.DATA.delete(snapshotKey(meta.ats, meta.slug, p));
				meta.snapshotParts = 0;
			} else if (open > SNAPSHOT_MAX_JOBS) {
				meta.snapshotError = `snapshot skipped: ${open} open jobs > ${SNAPSHOT_MAX_JOBS}`;
				meta.snapshotAt = now; meta.snapshotDirty = false; meta.dirtySince = null;
				return;
			} else {
				// parts of SNAPSHOT_PART_ROWS rows, each built and uploaded on its own so peak memory is one part
				const parts = Math.ceil(open / SNAPSHOT_PART_ROWS);
				for (let p = 0; p < parts; p++) {
					const cursor = this.ctx.storage.sql.exec(`SELECT * FROM jobs WHERE removed_at IS NULL ORDER BY id LIMIT ? OFFSET ?`, SNAPSHOT_PART_ROWS, p * SNAPSHOT_PART_ROWS);
					function* rows(): Iterable<StoredJob & { embeddingBuf?: ArrayBuffer | null }> {
						for (const r of cursor) {
							const j = rowToJob(r as unknown as JobRow, false, meta.lastOkAt ?? 0) as StoredJob & { embeddingBuf?: ArrayBuffer | null };
							j.embeddingBuf = (r as unknown as JobRow).embedding ?? null;
							yield j;
						}
					}
					const buf = buildSnapshotParquet({ ats: meta.ats, slug: meta.slug, jobs: rows(), meta });
					await this.env.DATA.put(snapshotKey(meta.ats, meta.slug, p), buf);
				}
				for (let p = parts; p < (meta.snapshotParts ?? 1); p++) await this.env.DATA.delete(snapshotKey(meta.ats, meta.slug, p)); // the board shrank
				meta.snapshotParts = parts;
			}
			meta.snapshotAt = now; meta.snapshotDirty = false; meta.dirtySince = null; meta.snapshotError = null;
		} catch (e) {
			meta.snapshotError = e instanceof Error ? e.message : String(e); // stays dirty; retried on later ticks
		}
	}

	/** Force a snapshot write now (admin/backfill). */
	async jobStatuses(ids: string[]): Promise<Record<string, { status: "open" | "removed"; first_seen_at: number | null; removed_at: number | null; last_seen_at: number | null; published_at: string | null }>> {
		const out: Record<string, { status: "open" | "removed"; first_seen_at: number | null; removed_at: number | null; last_seen_at: number | null; published_at: string | null }> = {};
		const floor = (await this.meta())?.lastOkAt ?? 0;
		for (let i = 0; i < ids.length; i += 100) {
			const batch = ids.slice(i, i + 100);
			const rows = this.ctx.storage.sql.exec<{ id: string; first_seen_at: number | null; removed_at: number | null; last_seen_at: number | null; published_at: string | null }>(
				`SELECT id, first_seen_at, removed_at, last_seen_at, json_extract(data, '$.publishedAt') AS published_at FROM jobs WHERE id IN (${batch.map(() => "?").join(",")})`, ...batch);
			for (const r of rows) out[r.id] = { status: r.removed_at == null ? "open" : "removed", first_seen_at: r.first_seen_at, removed_at: r.removed_at, last_seen_at: r.removed_at == null ? Math.max(r.last_seen_at ?? 0, floor) : r.last_seen_at, published_at: r.published_at };
		}
		return out;
	}

	async snapshotNow(): Promise<{ snapshotAt: number | null; error: string | null }> {
		const meta = await this.meta();
		if (!meta) throw new Error("board not initialized");
		await this.maybeSnapshot(meta, true);
		await this.ctx.storage.put("meta", meta);
		return { snapshotAt: meta.snapshotAt ?? null, error: meta.snapshotError ?? null };
	}

	async wipe(): Promise<{ wiped: number }> {
		const n = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs`).one().n;
		this.ctx.storage.sql.exec(`DELETE FROM jobs`);
		this.ctx.storage.sql.exec(`DELETE FROM runs`);
		const meta = await this.meta();
		if (meta) {
			meta.jobCount = 0; meta.snapshotAt = null; meta.snapshotDirty = false; meta.dirtySince = null;
			try { for (let p = 0; p < Math.max(1, meta.snapshotParts ?? 1); p++) await this.env.DATA.delete(snapshotKey(meta.ats, meta.slug, p)); } catch { /* best effort */ }
			await this.ctx.storage.put("meta", meta);
		}
		return { wiped: n };
	}

	async getRuns(limit = 30): Promise<RunSummary[]> {
		return this.ctx.storage.sql
			.exec<{ [k: string]: SqlStorageValue }>(`SELECT * FROM runs ORDER BY id DESC LIMIT ?`, limit)
			.toArray()
			.map((r) => ({
				id: r.id as number,
				runAt: r.run_at as number,
				status: r.status as RunSummary["status"],
				added: r.added as number,
				changed: r.changed as number,
				removed: r.removed as number,
				unchanged: r.unchanged as number,
				error: r.error as string | null,
			}));
	}

	async getState(opts: JobQuery = {}): Promise<BoardState> {
		return { meta: await this.getMeta(), jobs: await this.getJobs(opts) };
	}
}

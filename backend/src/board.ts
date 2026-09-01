import { DurableObject } from "cloudflare:workers";
import { fetchers, parseBoardName } from "./ats";
import type { FetchResult, Job, JobDetail } from "./ats/types";
import { ENRICH_BATCH, ENRICH_CONCURRENCY, enricher, enrichOne, jdText, type JobEnrichment } from "./enrich";
import { EMBED_TAG, embedTexts } from "./openai";
import { deriveCandidates } from "./company";
import { resolveCompany, type CompanyEnrichment } from "./company";
import { usd } from "./pricing";

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
/**
 * Chars of JD text that go into the embedding. text-embedding-3-small accepts 8,191 tokens per
 * input; ~28k chars of English stays under that. Everything about the job is embedded (company,
 * title, location, departments, terms, full JD) so classifiers can be trained on the vector alone.
 */
const EMBED_TEXT_CHARS = 28_000;
/** Detail requests per alarm tick (<= DETAIL_CONCURRENCY in flight). */
const DETAIL_BATCH = 150;
const DETAIL_CONCURRENCY = 6;

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
	/**
	 * Board is fetched from outside Cloudflare (provider blocks Worker IPs) and snapshots arrive via
	 * `ingest()`. The alarm never fetches; it only drains detail/embed/enrich backlogs.
	 */
	localOnly?: boolean;
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
	[k: string]: SqlStorageValue;
};

/** Upper bounds for provider calls so a hung connection can never pin the alarm to its 15-min limit. */
const FETCH_JOBS_TIMEOUT_MS = 5 * MINUTE;
const FETCH_DETAIL_TIMEOUT_MS = 45_000;

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
function contentHash(job: Job): string {
	const s = JSON.stringify(job);
	return hash32(s).toString(16).padStart(8, "0") + hash32(s, 0x9747b28c).toString(16).padStart(8, "0");
}

function isFresh(meta: BoardMeta, windowMs: number, now = Date.now()): boolean {
	return windowMs > 0 && meta.lastRunAt !== null && meta.lastStatus !== "error" && now - meta.lastRunAt < windowMs;
}

function nextSlotAfter(now: number, slotMs: number): number {
	const midnight = now - (now % DAY);
	const today = midnight + slotMs;
	return today > now ? today : today + DAY;
}

function rowToJob(r: JobRow, withEmbedding = false): StoredJob {
	const job = JSON.parse(r.data) as Job;
	// Detail data (fetched once per job) overrides the listing where present.
	const detail = r.detail === null ? null : (JSON.parse(r.detail) as JobDetail);
	if (detail) {
		if (detail.title) job.title = detail.title;
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
		lastSeenAt: r.last_seen_at,
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
			(this.autoEnrich() && this.pendingCount() > 0) ||
			(this.autoEmbed() && this.pendingEmbedCount() > 0) ||
			(fetchers[meta.ats]?.fetchDetail && this.pendingDetailCount() > 0);
		if (backlog) {
			let tick = Date.now() + MINUTE;
			if (meta.embedBackoffUntil && meta.embedBackoffUntil > tick) tick = meta.embedBackoffUntil;
			at = Math.min(at, tick);
		}
		await this.ctx.storage.setAlarm(at);
		meta.nextAlarmAt = at;
	}

	async alarm(): Promise<void> {
		const meta = await this.meta();
		if (!meta) return; // never initialized; nothing to do
		const now = Date.now();
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
				const existing = this.loadExisting();
				const seen = new Set<string>();
				const hasDetail = !!fetcher.fetchDetail;
				const res = await withTimeout(
					fetcher.fetchJobsStream(meta.slug, async (page) => {
						this.ctx.storage.transactionSync(() => this.applyPage(page, now, hasDetail, existing, seen, diff));
					}),
					FETCH_JOBS_TIMEOUT_MS,
					`fetchJobsStream ${meta.name}`,
				);
				if (res.status === "gone") {
					meta.lastStatus = "gone"; meta.lastError = null; meta.consecutiveFailures++;
					this.recordRun(now, "gone", null, null);
				} else {
					this.ctx.storage.transactionSync(() => this.sweepUnseen(existing, seen, now, diff));
					meta.lastStatus = "ok"; meta.lastOkAt = now; meta.lastError = null; meta.consecutiveFailures = 0;
					meta.jobCount = seen.size;
					this.recordRun(now, "ok", diff, null);
				}
				let next0 = nextSlotAfter(now, meta.slotMs);
				if (meta.consecutiveFailures >= BACKOFF_AFTER) next0 += 6 * DAY;
				meta.nextFetchAt = next0;
				return;
			}
			const result = provided ?? (await withTimeout(fetcher.fetchJobs(meta.slug), FETCH_JOBS_TIMEOUT_MS, `fetchJobs ${meta.name}`));
			if (result.status === "gone") {
				meta.lastStatus = "gone";
				meta.lastError = null;
				meta.consecutiveFailures++;
				this.recordRun(now, "gone", null, null);
			} else {
				const diff = this.applySnapshot(result.jobs, now, !!fetcher.fetchDetail);
				meta.lastStatus = "ok";
				meta.lastOkAt = now;
				meta.lastError = null;
				meta.consecutiveFailures = 0;
				meta.jobCount = result.jobs.length;
				this.recordRun(now, "ok", diff, null);
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
	private loadExisting(): Map<string, { hash: string; removed: boolean }> {
		const existing = new Map<string, { hash: string; removed: boolean }>();
		for (const r of this.ctx.storage.sql.exec<{ id: string; content_hash: string; removed_at: number | null }>(
			`SELECT id, content_hash, removed_at FROM jobs`,
		)) {
			existing.set(r.id, { hash: r.content_hash, removed: r.removed_at !== null });
		}
		return existing;
	}

	private sweepUnseen(existing: Map<string, { hash: string; removed: boolean }>, seen: Set<string>, now: number, diff: Diff): void {
		for (const [id, prev] of existing) {
			if (!seen.has(id) && !prev.removed) {
				this.ctx.storage.sql.exec(`UPDATE jobs SET removed_at = ? WHERE id = ?`, now, id);
				diff.removedIds.push(id);
			}
		}
	}

	private applySnapshot(jobs: Job[], now: number, hasDetail: boolean): Diff {
		const diff: Diff = { added: [], changed: [], removedIds: [], unchanged: 0 };
		this.ctx.storage.transactionSync(() => {
			const existing = this.loadExisting();
			const seen = new Set<string>();
			this.applyPage(jobs, now, hasDetail, existing, seen, diff);
			this.sweepUnseen(existing, seen, now, diff);
		});
		return diff;
	}

	/** Upsert one page of the snapshot; diff bookkeeping shared with the streaming path. */
	private applyPage(jobs: Job[], now: number, hasDetail: boolean, existing: Map<string, { hash: string; removed: boolean }>, seen: Set<string>, diff: Diff): void {
		{
			const sql = this.ctx.storage.sql;
			for (const job of jobs) {
				if (seen.has(job.id)) continue; // provider duplicated a job in its listing
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
				} else if (prev.hash !== hash) {
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
				} else {
					sql.exec(`UPDATE jobs SET last_seen_at = ?, removed_at = NULL WHERE id = ?`, now, job.id);
					if (prev.removed) diff.added.push(job);
					else diff.unchanged++;
				}
			}
		}
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

	private pendingDetailCount(): number {
		return this.ctx.storage.sql
			.exec<{ n: number }>(
				`SELECT COUNT(*) AS n FROM jobs WHERE removed_at IS NULL AND (detail_status = 'pending' OR detail_status IS NULL)`,
			)
			.one().n;
	}

	/**
	 * Fetch full postings for jobs whose listing lacked a description (providers with fetchDetail).
	 * One batch per tick, once per job; `error` rows are retried after a day. Rows with NULL
	 * status predate this stage and are treated as pending.
	 */
	private async runDetails(meta: BoardMeta): Promise<void> {
		const fetcher = fetchers[meta.ats];
		if (!fetcher?.fetchDetail) return;
		const now = Date.now();
		const rows = this.ctx.storage.sql
			.exec<JobRow>(
				`SELECT * FROM jobs WHERE removed_at IS NULL
				   AND (detail_status = 'pending' OR detail_status IS NULL OR (detail_status = 'error' AND detail_fetched_at < ?))
				 ORDER BY first_seen_at LIMIT ?`,
				now - DAY,
				DETAIL_BATCH,
			)
			.toArray();
		if (rows.length === 0) return;
		const queue = [...rows];
		const worker = async () => {
			for (;;) {
				const r = queue.shift();
				if (!r) return;
				const job = JSON.parse(r.data) as Job;
				if ((job.content ?? "").length >= DETAIL_MIN_CONTENT) {
					this.ctx.storage.sql.exec(`UPDATE jobs SET detail_status = 'na' WHERE id = ?`, r.id);
					continue;
				}
				try {
					const d = await withTimeout(fetcher.fetchDetail!(meta.slug, job), FETCH_DETAIL_TIMEOUT_MS, `fetchDetail ${meta.name}/${r.id}`);
					this.ctx.storage.sql.exec(
						`UPDATE jobs SET detail_status = ?, detail = ?, detail_error = NULL, detail_fetched_at = ? WHERE id = ?`,
						d ? "done" : "na",
						d ? JSON.stringify(d) : null,
						Date.now(),
						r.id,
					);
				} catch (e) {
					this.ctx.storage.sql.exec(
						`UPDATE jobs SET detail_status = 'error', detail_error = ?, detail_fetched_at = ? WHERE id = ?`,
						e instanceof Error ? e.message : String(e),
						Date.now(),
						r.id,
					);
				}
			}
		};
		await Promise.all(Array.from({ length: DETAIL_CONCURRENCY }, worker));
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
				   AND (embed_status IS NULL OR embed_status IN ('pending', 'error') OR embed_model != ?)
				   AND (detail_status IN ('done','na','error') OR detail_status IS NULL)`,
				EMBED_TAG,
			)
			.one().n;
	}

	/** Everything we know about the job, as labelled lines, then the full JD text (capped). */
	private embedText(job: Job, meta: BoardMeta): string {
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
					   AND (embed_status IS NULL OR embed_status IN ('pending', 'error') OR embed_model != ?)
					   AND (detail_status IN ('done','na','error') OR detail_status IS NULL)
					 ORDER BY CASE WHEN embed_status = 'error' THEN 1 ELSE 0 END, first_seen_at LIMIT ?`,
					EMBED_TAG,
					EMBED_BATCH,
				)
				.toArray();
			if (rows.length === 0) return;
			await this.embedRows(rows, meta);
		}
	}

	private async embedRows(allRows: JobRow[], meta: BoardMeta): Promise<void> {
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
			const { vectors } = await embedTexts(this.env, texts);
			meta.embedBackoffUntil = null;
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
			for (const r of rows) this.ctx.storage.sql.exec(`UPDATE jobs SET embed_status = 'error', embed_error = ? WHERE id = ?`, msg, r.id);
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
		const where: string[] = [];
		const params: SqlStorageValue[] = [];
		if (opts.status === "open") where.push(`removed_at IS NULL`);
		else if (opts.status === "removed") where.push(`removed_at IS NOT NULL`);
		if (opts.enrich) {
			where.push(`enrich_status = ?`);
			params.push(opts.enrich);
		}
		if (opts.since !== undefined) {
			where.push(`(last_seen_at >= ? OR changed_at >= ? OR removed_at >= ?)`);
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
		const jobs = this.ctx.storage.sql.exec<JobRow>(sql, ...params).toArray().map((r) => rowToJob(r, opts.embed));
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
		const j = rowToJob(r, true); j.raw = undefined; j.content = null; j.detailRaw = null; return j;
	}

	/** Drop every job row and reset counters; the next fetch repopulates from scratch. Recovery for
	 * boards whose stored rows grew pathological (e.g. fat detail raw written by an older fetcher). */
	async wipe(): Promise<{ wiped: number }> {
		const n = this.ctx.storage.sql.exec<{ n: number }>(`SELECT COUNT(*) AS n FROM jobs`).one().n;
		this.ctx.storage.sql.exec(`DELETE FROM jobs`);
		this.ctx.storage.sql.exec(`DELETE FROM runs`);
		const meta = await this.meta();
		if (meta) { meta.jobCount = 0; await this.ctx.storage.put("meta", meta); }
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

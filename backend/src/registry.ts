import { DurableObject } from "cloudflare:workers";
import { boardName, slugsFor } from "./ats";
import { DEFAULT_FRESH_MS } from "./board";

/** Boards touched per alarm tick; keeps each tick well under subrequest/time limits. */
const CHUNK = 250;
const CONCURRENCY = 25;
/**
 * Fetch mode does real work per board (seconds each) and hits one provider repeatedly, so use
 * smaller ticks and low concurrency (workable 429s at 25 concurrent).
 */
const FETCH_CHUNK = 30; // with retry/backoff a fetch can take ~40 s; keep a tick far below the alarm limit
const FETCH_CONCURRENCY = 6;

export type SyncMode = "arm" | "fetch" | "kick";
/** A sweep with no progress for this long is restarted by the next sync() (its alarm was dropped). */
const STALE_MS = 20 * 60_000;

export interface SyncState {
	ats: string;
	/** "arm" = ensure every board has an alarm; "fetch" = also fetch each board now; "kick" = fire the alarm now on boards with a detail/embed/enrich backlog. */
	mode: SyncMode;
	/** fetch mode: skip boards that completed a non-error fetch within this many ms (0 = never skip). */
	skipRecentMs: number;
	cursor: number;
	total: number;
	startedAt: number;
	/** Last tick that made progress; a running sweep older than STALE_MS is considered dead. */
	updatedAt: number;
	finishedAt: number | null;
	touched: number;
	/** fetch mode: boards actually fetched / skipped as recent. kick mode: boards kicked / idle. */
	fetched: number;
	skipped: number;
	/** kick mode: fleet backlog tallied during the sweep (jobs awaiting detail fetch / embedding). */
	pendingDetails: number;
	pendingEmbeds: number;
	errors: number;
	lastError: string | null;
}

/**
 * One instance per ATS. `sync()` walks every slug for that ATS in alarm-driven chunks and
 * calls `ensureScheduled` on each Board, so the whole fleet gets (re)armed without a single
 * invocation ever needing thousands of subrequests.
 */
export class Registry extends DurableObject<Env> {
	async sync(ats: string, opts: { mode?: SyncMode; skipRecentMs?: number } = {}): Promise<SyncState> {
		const existing = await this.ctx.storage.get<SyncState>("sync");
		if (existing && existing.finishedAt === null && Date.now() - (existing.updatedAt ?? existing.startedAt) < STALE_MS) {
			return existing; // already running
		}
		const state: SyncState = {
			ats,
			mode: opts.mode ?? "arm",
			skipRecentMs: opts.skipRecentMs ?? DEFAULT_FRESH_MS,
			cursor: 0,
			total: slugsFor(ats).length,
			startedAt: Date.now(),
			updatedAt: Date.now(),
			finishedAt: null,
			touched: 0,
			fetched: 0,
			skipped: 0,
			pendingDetails: 0,
			pendingEmbeds: 0,
			errors: 0,
			lastError: null,
		};
		await this.ctx.storage.put("sync", state);
		await this.ctx.storage.setAlarm(Date.now());
		return state;
	}

	async status(): Promise<SyncState | null> {
		return (await this.ctx.storage.get<SyncState>("sync")) ?? null;
	}

	async alarm(): Promise<void> {
		const state = await this.ctx.storage.get<SyncState>("sync");
		if (!state || state.finishedAt !== null) return;
		try {
			await this.tick(state);
		} finally {
			state.updatedAt = Date.now();
			await this.ctx.storage.put("sync", state);
			if (state.finishedAt === null) await this.ctx.storage.setAlarm(Date.now());
		}
	}

	private async tick(state: SyncState): Promise<void> {
		// Fetch mode does provider work per board; arm/kick are cheap RPCs (a kick may wait on a busy DO,
		// so keep many in flight).
		const chunk = state.mode === "fetch" ? FETCH_CHUNK : CHUNK;
		const slugs = slugsFor(state.ats).slice(state.cursor, state.cursor + chunk);

		const touch = async (slug: string): Promise<boolean | undefined> => {
			const name = boardName(state.ats, slug);
			const stub = this.env.BOARD.getByName(name);
			if (state.mode === "fetch") return stub.forceFetch(name, state.skipRecentMs);
			if (state.mode === "kick") {
				const k = await stub.kick(name);
				state.pendingDetails = (state.pendingDetails ?? 0) + k.details; // sweeps started before this field existed
				state.pendingEmbeds = (state.pendingEmbeds ?? 0) + k.embeds;
				return k.kicked;
			}
			await stub.ensureScheduled(name);
			return undefined;
		};

		const concurrency = state.mode === "fetch" ? FETCH_CONCURRENCY : CONCURRENCY;
		for (let i = 0; i < slugs.length; i += concurrency) {
			const batch = slugs.slice(i, i + concurrency);
			const results = await Promise.allSettled(
				batch.map(async (slug) => {
					try {
						return await touch(slug);
					} catch {
						// Transient platform errors (connection lost, storage reset) are common during mass
						// creation; one retry after a short pause recovers almost all of them.
						await new Promise((r) => setTimeout(r, 500));
						return await touch(slug);
					}
				}),
			);
			for (const r of results) {
				if (r.status === "fulfilled") {
					state.touched++;
					if (r.value === true) state.fetched++;
					else if (r.value === false) state.skipped++;
				} else {
					state.errors++;
					state.lastError = String(r.reason);
				}
			}
		}

		state.cursor += slugs.length;
		if (state.cursor >= state.total) state.finishedAt = Date.now();
	}
}

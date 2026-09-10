import { DurableObject } from "cloudflare:workers";

/**
 * The publisher mutex. Exactly one consolidation run (laptop or container) may write the bucket at a time;
 * every publishing stage acquires this lock under a per-run holder id, renews it while it works, and the
 * last stage releases it. A lock that is not renewed expires at `until`, so a crashed run cannot block the
 * next one forever. One object, named "consolidate"; single-threaded, so acquire is atomic.
 */
export interface LockState { holder: string; since: number; until: number; renewed: number; note?: string }
export class Lock extends DurableObject<Env> {
	private async current(): Promise<LockState | null> {
		const s = await this.ctx.storage.get<LockState>("lock");
		return s && s.until > Date.now() ? s : null;
	}
	async status(): Promise<LockState | null> { return this.current(); }
	/** Take the lock unless another live holder has it (or `force`). Re-acquiring under the same holder renews. */
	async acquire(holder: string, ttlMs: number, note?: string, force = false): Promise<{ ok: boolean; lock: LockState | null }> {
		const now = Date.now(); const cur = await this.current();
		if (cur && cur.holder !== holder && !force) return { ok: false, lock: cur };
		const lock: LockState = { holder, since: cur?.holder === holder ? cur.since : now, until: now + ttlMs, renewed: now, note: note ?? cur?.note };
		await this.ctx.storage.put("lock", lock);
		return { ok: true, lock };
	}
	async renew(holder: string, ttlMs: number): Promise<{ ok: boolean; lock: LockState | null }> {
		const cur = await this.current();
		if (!cur || cur.holder !== holder) return { ok: false, lock: cur };
		const lock = { ...cur, until: Date.now() + ttlMs, renewed: Date.now() };
		await this.ctx.storage.put("lock", lock);
		return { ok: true, lock };
	}
	/**
	 * Snapshot freeze: while set, Board objects defer rewriting their R2 snapshot (they retry in ten minutes). The
	 * parquet stage reads thousands of snapshot files by byte range over minutes; a board replacing its file
	 * mid-read hands the reader pages of the new file under the old footer (2026-09-09: embedding bytes decoded
	 * as text, twice). Expires on its own like the lock.
	 */
	async freeze(holder: string, ttlMs: number): Promise<{ ok: boolean; until: number }> {
		const until = Date.now() + ttlMs;
		await this.ctx.storage.put("freeze", { holder, until });
		return { ok: true, until };
	}
	async thaw(): Promise<{ ok: boolean }> { await this.ctx.storage.delete("freeze"); return { ok: true }; }
	async snapshotsFrozen(): Promise<boolean> {
		const f = await this.ctx.storage.get<{ holder: string; until: number }>("freeze");
		return !!f && f.until > Date.now();
	}
	async release(holder: string, force = false): Promise<{ ok: boolean; lock: LockState | null }> {
		const cur = await this.current();
		if (cur && cur.holder !== holder && !force) return { ok: false, lock: cur };
		await this.ctx.storage.delete("lock");
		return { ok: true, lock: null };
	}
}

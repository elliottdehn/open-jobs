import { DurableObject } from "cloudflare:workers";

/**
 * Per-IP spend meter for user-triggered enrichment: two fixed windows (hour, day) in USD.
 * `reserve(estimate)` checks both limits and holds the estimate; `settle(reserved, actual)` replaces
 * the hold with what was really spent. Idle most of the time; hibernates.
 */
interface Win { start: number; usd: number }
export class Budget extends DurableObject<Env> {
	private async win(key: string, len: number): Promise<Win> {
		const w = (await this.ctx.storage.get<Win>(key)) ?? { start: Date.now(), usd: 0 };
		if (Date.now() - w.start >= len) return { start: Date.now(), usd: 0 };
		return w;
	}
	async status(hourLimit: number, dayLimit: number): Promise<{ hourUsd: number; dayUsd: number; hourLimit: number; dayLimit: number; hourResetMs: number; dayResetMs: number }> {
		const h = await this.win("h", 3_600_000), d = await this.win("d", 86_400_000);
		return { hourUsd: h.usd, dayUsd: d.usd, hourLimit, dayLimit, hourResetMs: h.start + 3_600_000 - Date.now(), dayResetMs: d.start + 86_400_000 - Date.now() };
	}
	/** Hold `estimate` USD if it fits both windows. Returns what could be admitted (may be 0). */
	async reserve(estimate: number, hourLimit: number, dayLimit: number): Promise<{ ok: boolean; retryAfterMs: number; hourUsd: number; dayUsd: number }> {
		const h = await this.win("h", 3_600_000), d = await this.win("d", 86_400_000);
		const ok = h.usd + estimate <= hourLimit && d.usd + estimate <= dayLimit;
		if (ok) {
			h.usd += estimate; d.usd += estimate;
			await this.ctx.storage.put({ h, d });
		}
		const retryAfterMs = h.usd + estimate > hourLimit ? h.start + 3_600_000 - Date.now() : d.start + 86_400_000 - Date.now();
		return { ok, retryAfterMs: ok ? 0 : Math.max(1000, retryAfterMs), hourUsd: h.usd, dayUsd: d.usd };
	}
	/** Replace a reservation with the actual spend. */
	async settle(reserved: number, actual: number): Promise<void> {
		const h = await this.win("h", 3_600_000), d = await this.win("d", 86_400_000);
		h.usd = Math.max(0, h.usd - reserved + actual); d.usd = Math.max(0, d.usd - reserved + actual);
		await this.ctx.storage.put({ h, d });
	}
}

import { DurableObject } from "cloudflare:workers";

/**
 * Fixed-window rate limiter, one DO per key (client IP). `hit()` returns whether the request is
 * allowed and how long until the window resets. Tiny and idle most of the time; hibernates.
 */
export class RateLimit extends DurableObject<Env> {
	async hit(limit: number, windowMs: number): Promise<{ ok: boolean; remaining: number; resetMs: number }> {
		const now = Date.now();
		const win = (await this.ctx.storage.get<{ start: number; n: number }>("w")) ?? { start: now, n: 0 };
		if (now - win.start >= windowMs) {
			win.start = now;
			win.n = 0;
		}
		const ok = win.n < limit;
		if (ok) win.n++;
		await this.ctx.storage.put("w", win);
		return { ok, remaining: Math.max(0, limit - win.n), resetMs: win.start + windowMs - now };
	}
}

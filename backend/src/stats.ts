import { DurableObject } from "cloudflare:workers";

/**
 * Daily use counters for the public endpoints: searches embedded (/embed), JDs generated (/jd), group files
 * fetched (/data/groups/...). One object named "daily"; bumps accumulate in memory and flush to storage on a
 * five-second alarm, so a burst of group reads costs a handful of row writes, not one per request. Keys are
 * `<UTC date>:<kind>`. The cron posts yesterday's line to Slack (see scheduled() in index.ts); GET /stats (admin)
 * returns the last days.
 */
export type StatKind = "embed" | "jd" | "group" | (string & {});  // "page:/", "page:/data/", "ref:<referrer host>" are counted too
const KINDS: StatKind[] = ["embed", "jd", "group"];
const utcDay = (t = Date.now()) => new Date(t).toISOString().slice(0, 10);

export class Stats extends DurableObject<Env> {
	private pending = new Map<string, number>();

	async bump(kind: StatKind, n = 1): Promise<void> {
		const key = `${utcDay()}:${kind}`;
		this.pending.set(key, (this.pending.get(key) ?? 0) + n);
		if ((await this.ctx.storage.getAlarm()) === null) await this.ctx.storage.setAlarm(Date.now() + 5000);
	}
	async alarm(): Promise<void> {
		const batch = this.pending; this.pending = new Map();
		if (!batch.size) return;
		const cur = await this.ctx.storage.get<number>([...batch.keys()]);
		const out: Record<string, number> = {};
		for (const [k, n] of batch) out[k] = (cur.get(k) ?? 0) + n;
		await this.ctx.storage.put(out);
	}
	async day(date: string): Promise<Record<StatKind, number>> {
		// every counter the day has (the three fixed kinds always present, then page loads and referrers as they occur)
		const m = await this.ctx.storage.list<number>({ prefix: `${date}:` });
		const out = {} as Record<StatKind, number>;
		for (const k of KINDS) out[k] = 0;
		for (const [key, n] of m) out[key.slice(date.length + 1)] = n;
		for (const [key, n] of this.pending) if (key.startsWith(`${date}:`)) out[key.slice(date.length + 1)] = (out[key.slice(date.length + 1)] ?? 0) + n;
		return out;
	}
	async recent(days = 7): Promise<Record<string, Record<StatKind, number>>> {
		const out: Record<string, Record<StatKind, number>> = {};
		for (let i = 0; i < days; i++) { const d = utcDay(Date.now() - i * 86_400_000); out[d] = await this.day(d); }
		return out;
	}
}

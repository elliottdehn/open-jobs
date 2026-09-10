import { DurableObject } from "cloudflare:workers";

/**
 * Best-effort global dedup of postings across boards. A key is (employer, title, location), normalized the way
 * the export dedups (scripts/build-parquet.py dedup_aggregators); the owner is "ats/slug#id". First-party boards
 * claim their postings; a crawled job board asks before it embeds and stores a posting's text, and a copy of
 * something already claimed is kept as a slim `dup_of` row instead. Misses are fine: consolidation is the real
 * dedup. 64 shards by key hash (see dedupeShard); claims are batched per page/tick.
 */
export const DEDUPE_SHARDS = 64;
export function dedupeShard(key: string): number {
	let h = 2166136261;
	for (let i = 0; i < key.length; i++) { h ^= key.charCodeAt(i); h = Math.imul(h, 16777619); }
	return (h >>> 0) % DEDUPE_SHARDS;
}
const SUFFIX = /\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|sas|sarl|srl|bv|nv|oy|ab|as|plc|co|corp|corporation|company|group|holding|holdings|kg|mbh|e\.?v\.?|se|s\.?p\.?a\.?|kk|k\.k\.)\b/g;
const normOrg = (s: string) => s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(SUFFIX, " ").replace(/ +/g, " ").trim();
const normTitle = (s: string) => s.toLowerCase().replace(/\(.*?\)|\[.*?\]|[^a-z0-9 ]+/g, " ").replace(/ +/g, " ").trim();
const normLoc = (s: string) => s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/ +/g, " ").trim();
/** The dedup key, or null when a part that makes it meaningful is missing (an aggregator posting without an employer or a location never dedups). */
export function dedupeKey(org: string | null | undefined, title: string | null | undefined, location: string | null | undefined): string | null {
	const o = normOrg(org ?? ""), t = normTitle(title ?? ""), l = normLoc(location ?? "");
	return o && t && l ? `${o}|${t}|${l}` : null;
}

export class Dedupe extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		ctx.blockConcurrencyWhile(async () => {
			ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS keys (k TEXT PRIMARY KEY, owner TEXT NOT NULL, first_party INTEGER NOT NULL, at INTEGER NOT NULL)`);
		});
	}
	/**
	 * Claim keys for `owner`. Returns, per key, the existing owner when someone else holds it (a duplicate), else null.
	 * A first-party claim takes over a key held by a job-board posting (the employer's own listing wins).
	 */
	async claim(entries: { key: string; owner: string }[], firstParty: boolean): Promise<(string | null)[]> {
		const now = Date.now(); const out: (string | null)[] = [];
		for (const { key, owner } of entries) {
			const cur = this.ctx.storage.sql.exec<{ owner: string; first_party: number }>(`SELECT owner, first_party FROM keys WHERE k = ?`, key).toArray()[0];
			if (!cur) { this.ctx.storage.sql.exec(`INSERT INTO keys (k, owner, first_party, at) VALUES (?, ?, ?, ?)`, key, owner, firstParty ? 1 : 0, now); out.push(null); continue; }
			if (cur.owner === owner) { out.push(null); continue; }
			if (firstParty && !cur.first_party) { this.ctx.storage.sql.exec(`UPDATE keys SET owner = ?, first_party = 1, at = ? WHERE k = ?`, owner, now, key); out.push(null); continue; }
			out.push(cur.owner);
		}
		return out;
	}
	async release(keys: string[], owner: string): Promise<number> {
		let n = 0;
		for (const k of keys) n += this.ctx.storage.sql.exec(`DELETE FROM keys WHERE k = ? AND owner = ?`, k, owner).rowsWritten > 0 ? 1 : 0;
		return n;
	}
	async stats(): Promise<{ keys: number; firstParty: number }> {
		const r = this.ctx.storage.sql.exec<{ n: number; f: number }>(`SELECT COUNT(*) AS n, COALESCE(SUM(first_party), 0) AS f FROM keys`).one();
		return { keys: r.n, firstParty: r.f };
	}
}

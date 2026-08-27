import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

const UA = "open-jobs/0.1";
const CONCURRENCY = 6;
const PAGE = 10; // both APIs cap page size at 10 regardless of `num`

async function mapLimit<T, R>(items: T[], limit: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let i = 0;
	await Promise.all(
		Array.from({ length: Math.min(limit, items.length) }, async () => {
			while (i < items.length) {
				const idx = i++;
				out[idx] = await fn(items[idx]);
			}
		}),
	);
	return out;
}

interface V2Position {
	id: number;
	name: string;
	location?: string | null;
	locations?: string[];
	department?: string | null;
	business_unit?: string | null;
	t_update?: number;
	t_create?: number;
	job_description?: string | null;
	canonicalPositionUrl?: string;
}
interface PcsxPosition {
	id: number;
	name: string;
	locations?: string[];
	department?: string | null;
	postedTs?: number;
	creationTs?: number;
	positionUrl?: string;
}

const ts = (n: number | undefined | null): string | null => (n ? new Date(n * 1000).toISOString() : null);

async function getJson(url: string, attempt = 0): Promise<{ status: number; body: unknown }> {
	const res = await fetch(url, { headers: { "user-agent": UA, accept: "application/json" } });
	if ((res.status === 429 || res.status >= 500) && attempt < 5) {
		await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
		return getJson(url, attempt + 1);
	}
	const text = await res.text();
	let body: unknown = null;
	try {
		body = JSON.parse(text);
	} catch {
		body = text;
	}
	return { status: res.status, body };
}

async function discoverDomain(slug: string): Promise<string | null | "gone"> {
	let res: Response;
	try {
		res = await fetch(`https://${slug}.eightfold.ai/careers`, { headers: { "user-agent": UA } });
	} catch (e) {
		if (e instanceof TypeError) return "gone"; // NXDOMAIN
		throw e;
	}
	if (res.status === 404) return "gone";
	if (!res.ok) throw new Error(`eightfold ${slug}: HTTP ${res.status} on /careers`);
	const html = await res.text();
	const m =
		html.match(/[?&]domain=([a-z0-9.-]+)/i) ??
		html.match(/&#34;domain&#34;:\s*&#34;([^&]+)&#34;/) ??
		html.match(/"domain":\s*"([a-z0-9.-]+)"/i);
	return m ? m[1] : null;
}

/** Domain per slug, remembered from fetchJobs so detail calls need not re-scrape /careers. */
const domains = new Map<string, string>();

async function domainFor(slug: string): Promise<string | "gone"> {
	const hit = domains.get(slug);
	if (hit) return hit;
	const d = await discoverDomain(slug);
	if (d === "gone") return "gone";
	if (!d) throw new Error(`eightfold ${slug}: could not discover domain from /careers page`);
	domains.set(slug, d);
	return d;
}

/**
 * Detail: PCS sites → GET /api/apply/v2/jobs/{id}?domain=… ({ ...position, job_description });
 * PCSX sites → GET /api/pcsx/jobs/{id}?domain=… ({ data: { job_description, ... } }). Both 404 for unknown ids.
 */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const base = `https://${slug}.eightfold.ai`;
	const domain = await domainFor(slug);
	if (domain === "gone") return null;
	const raw = job.raw as Partial<V2Position & PcsxPosition> | undefined;
	const pcsx = !!raw && "positionUrl" in raw && !("canonicalPositionUrl" in raw) && !("job_description" in raw);
	const q = `domain=${encodeURIComponent(domain)}`;
	const url = pcsx ? `${base}/api/pcsx/jobs/${encodeURIComponent(job.id)}?${q}` : `${base}/api/apply/v2/jobs/${encodeURIComponent(job.id)}?${q}`;
	const res = await fetchRetry(url, { headers: { "user-agent": UA, accept: "application/json" } });
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`eightfold ${slug}: HTTP ${res.status} on detail ${job.id}`);
	const body = (await res.json()) as Record<string, unknown>;
	const p = ((body.data as Record<string, unknown> | undefined) ?? body) as Partial<V2Position & PcsxPosition> & {
		job_description?: string | null;
		description?: string | null;
	};
	const content = p.job_description || p.description || null;
	if (!content && !p.id) return null;
	const locs = p.location ? [p.location] : (p.locations ?? []);
	return {
		content,
		raw: p,
		location: locs.length ? locs.join("; ") : undefined,
		departments: p.department ? [p.department] : undefined,
		publishedAt: ts(p.t_create ?? p.postedTs ?? p.creationTs) ?? undefined,
		updatedAt: ts(p.t_update) ?? undefined,
	};
}

export const eightfold: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const base = `https://${slug}.eightfold.ai`;
		const domain = await discoverDomain(slug);
		if (domain === "gone") return { status: "gone" };
		if (!domain) throw new Error(`eightfold ${slug}: could not discover domain from /careers page`);
		domains.set(slug, domain);

		// Older "PCS" API first.
		const v2Url = (start: number) =>
			`${base}/api/apply/v2/jobs?domain=${encodeURIComponent(domain)}&start=${start}&num=${PAGE}`;
		const first = await getJson(v2Url(0));
		if (first.status === 404) return { status: "gone" };
		const fb = first.body as { positions?: V2Position[]; count?: number; message?: string } | string;
		if (typeof fb === "object" && fb && Array.isArray(fb.positions)) {
			const count = fb.count ?? fb.positions.length;
			const starts: number[] = [];
			for (let s = PAGE; s < count; s += PAGE) starts.push(s);
			const rest = await mapLimit(starts, CONCURRENCY, async (s) => {
				const r = await getJson(v2Url(s));
				const b = r.body as { positions?: V2Position[] };
				if (r.status !== 200 || !b || !Array.isArray(b.positions))
					throw new Error(`eightfold ${slug}: HTTP ${r.status} at start=${s}`);
				return b.positions;
			});
			const all = [fb.positions, ...rest].flat();
			const seen = new Set<string>();
			const jobs: Job[] = [];
			for (const p of all) {
				const id = String(p.id);
				if (seen.has(id)) continue;
				seen.add(id);
				jobs.push({
					id,
					title: p.name,
					location: p.location || (p.locations?.length ? p.locations.join("; ") : null),
					url: p.canonicalPositionUrl || `${base}/careers/job/${p.id}`,
					departments: [p.department].filter((d): d is string => !!d),
					publishedAt: ts(p.t_create),
					updatedAt: ts(p.t_update),
					content: p.job_description || null,
					raw: p,
				});
			}
			return { status: "ok", jobs };
		}
		const msg = typeof fb === "object" && fb ? fb.message : String(fb);
		if (!/PCSX/i.test(msg ?? "")) throw new Error(`eightfold ${slug}: unexpected v2 response (${first.status}): ${String(msg).slice(0, 100)}`);

		// Newer "PCSX" sites.
		const pxUrl = (start: number) =>
			`${base}/api/pcsx/search?domain=${encodeURIComponent(domain)}&query=&location=&start=${start}`;
		const getPx = async (start: number): Promise<{ count: number; positions: PcsxPosition[] }> => {
			const r = await getJson(pxUrl(start));
			if (r.status === 404) throw new Error(`eightfold ${slug}: pcsx search 404 for domain ${domain}`);
			const b = r.body as { data?: { count: number; positions: PcsxPosition[] } };
			if (r.status !== 200 || !b?.data || !Array.isArray(b.data.positions))
				throw new Error(`eightfold ${slug}: HTTP ${r.status} at pcsx start=${start}`);
			return b.data;
		};
		const p0 = await getPx(0);
		const starts: number[] = [];
		for (let s = PAGE; s < p0.count; s += PAGE) starts.push(s);
		// PCSX rate-limits aggressively; keep concurrency low.
		const rest = await mapLimit(starts, 3, async (s) => (await getPx(s)).positions);
		const all = [p0.positions, ...rest].flat();
		const seen = new Set<string>();
		const jobs: Job[] = [];
		for (const p of all) {
			const id = String(p.id);
			if (seen.has(id)) continue;
			seen.add(id);
			jobs.push({
				id,
				title: p.name,
				location: p.locations?.length ? p.locations.join("; ") : null,
				url: p.positionUrl ? new URL(p.positionUrl, base).toString() : `${base}/careers/job/${p.id}`,
				departments: [p.department].filter((d): d is string => !!d),
				publishedAt: ts(p.postedTs ?? p.creationTs),
				updatedAt: null,
				content: null,
				raw: p,
			});
		}
		return { status: "ok", jobs };
	},
};

import type { AtsFetcher, FetchResult, Job } from "./types";

/**
 * Recruiterbox (now Trakstar Hire). Public widget API:
 * GET https://jsapi.recruiterbox.com/v1/openings?client_name={slug}&limit=250&offset=N
 * -> { meta: { offset, limit, total }, objects: [...] } (offset paging, max limit 250).
 * Includes full HTML description. Unknown client -> HTTP 400 {"client_name": "Invalid client name"}.
 * No dates are exposed.
 */

const UA = "open-jobs/0.1";
const LIMIT = 250;

interface RbOpening {
	id: string;
	title: string;
	client_name?: string;
	description?: string | null;
	location?: { city?: string | null; state?: string | null; country?: string | null; zipcode?: string | null } | null;
	tags?: string[];
	hosted_url?: string;
	allows_remote?: boolean | null;
	position_type?: string | null;
	team?: string | null;
	close_date?: string | null;
}

interface RbPage {
	meta: { offset: number; limit: number; total: number };
	objects: RbOpening[];
}

async function getPage(slug: string, offset: number): Promise<RbPage | "gone"> {
	const url = `https://jsapi.recruiterbox.com/v1/openings?client_name=${encodeURIComponent(slug)}&limit=${LIMIT}&offset=${offset}`;
	const res = await fetch(url, { headers: { accept: "application/json", "user-agent": UA } });
	if (res.status === 404) return "gone";
	if (res.status === 400) {
		const text = await res.text();
		if (/invalid client name/i.test(text)) return "gone";
		throw new Error(`recruiterbox ${slug}: HTTP 400 ${text.slice(0, 200)}`);
	}
	if (!res.ok) throw new Error(`recruiterbox ${slug}: HTTP ${res.status}`);
	const body = (await res.json()) as RbPage;
	if (!body || !Array.isArray(body.objects) || !body.meta) throw new Error(`recruiterbox ${slug}: unexpected response shape`);
	return body;
}

export const recruiterbox: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const first = await getPage(slug, 0);
		if (first === "gone") return { status: "gone" };
		const all: RbOpening[] = [...first.objects];
		const total = first.meta.total;
		if (total > all.length) {
			const offsets: number[] = [];
			for (let o = first.objects.length; o < total; o += LIMIT) offsets.push(o);
			// at most 6 in flight
			for (let i = 0; i < offsets.length; i += 6) {
				const pages = await Promise.all(offsets.slice(i, i + 6).map((o) => getPage(slug, o)));
				for (const p of pages) {
					if (p === "gone") return { status: "gone" };
					all.push(...p.objects);
				}
			}
		}
		const seen = new Set<string>();
		const jobs: Job[] = [];
		for (const o of all) {
			if (seen.has(o.id)) continue;
			seen.add(o.id);
			const loc = [o.location?.city, o.location?.state, o.location?.country]
				.map((s) => (s ?? "").trim())
				.filter(Boolean)
				.join(", ");
			jobs.push({
				id: o.id,
				title: o.title,
				location: loc || (o.allows_remote ? "Remote" : null),
				url: o.hosted_url ?? `https://${slug}.recruiterbox.com/jobs/${o.id}/`,
				departments: o.team ? [o.team] : [],
				publishedAt: null,
				updatedAt: null,
				content: o.description ?? null,
				raw: o,
			});
		}
		return { status: "ok", jobs };
	},
};

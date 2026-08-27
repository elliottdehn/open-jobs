import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

const UA = "open-jobs/0.1";
const PAGE = 100;
const CONCURRENCY = 6;

interface SrPosting {
	id: string;
	name: string;
	uuid?: string;
	refNumber?: string;
	company?: { identifier?: string; name?: string };
	releasedDate?: string | null;
	location?: { fullLocation?: string; city?: string; region?: string; country?: string; remote?: boolean } | null;
	department?: { id?: string; label?: string } | null;
	function?: { id?: string; label?: string } | null;
	ref?: string;
}

interface SrPage {
	offset: number;
	limit: number;
	totalFound: number;
	content: SrPosting[];
}

interface SrDetail {
	postingUrl?: string;
	releasedDate?: string | null;
	location?: SrPosting["location"];
	department?: SrPosting["department"];
	jobAd?: { sections?: Record<string, { title?: string; text?: string }> };
}

async function getJson<T>(url: string): Promise<T> {
	const res = await fetch(url, { headers: { accept: "application/json", "user-agent": UA } });
	if (!res.ok) throw new Error(`smartrecruiters: HTTP ${res.status} for ${url}`);
	return (await res.json()) as T;
}

/** The postings API returns an empty 200 for unknown companies; the careers site 302s to the root. */
async function companyExists(slug: string): Promise<boolean> {
	const res = await fetch(`https://careers.smartrecruiters.com/${encodeURIComponent(slug)}`, {
		redirect: "manual",
		headers: { "user-agent": UA },
	});
	if (res.status === 404) return false;
	if (res.status >= 300 && res.status < 400) {
		const loc = res.headers.get("location") ?? "";
		// Redirect to the generic landing page => no such company.
		if (/^https?:\/\/(jobs|careers)\.smartrecruiters\.com\/?$/.test(loc)) return false;
	}
	return true;
}

async function mapLimit<T, R>(items: T[], limit: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let next = 0;
	const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
		while (next < items.length) {
			const i = next++;
			out[i] = await fn(items[i]);
		}
	});
	await Promise.all(workers);
	return out;
}

function locationOf(p: SrPosting): string | null {
	const l = p.location;
	if (!l) return null;
	if (l.fullLocation) return l.fullLocation;
	const parts = [l.city, l.region, l.country?.toUpperCase()].filter(Boolean);
	if (parts.length === 0) return l.remote ? "Remote" : null;
	return parts.join(", ");
}

/** Detail: GET /v1/companies/{slug}/postings/{id} → jobAd.sections (HTML per section). */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const raw = job.raw as SrPosting | undefined;
	const company = raw?.company?.identifier ?? slug;
	const id = raw?.id ?? job.id;
	const res = await fetchRetry(
		`https://api.smartrecruiters.com/v1/companies/${encodeURIComponent(company)}/postings/${encodeURIComponent(id)}`,
		{ headers: { accept: "application/json", "user-agent": UA } },
	);
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`smartrecruiters detail ${company}/${id}: HTTP ${res.status}`);
	const d = (await res.json()) as SrDetail;
	const sections = d.jobAd?.sections ?? {};
	const html = Object.values(sections)
		.filter((s) => s && s.text)
		.map((s) => (s.title ? `<h3>${s.title}</h3>\n${s.text}` : s.text))
		.join("\n");
	const location = d.location ? locationOf({ location: d.location } as SrPosting) : undefined;
	return {
		content: html || null,
		raw: d,
		location: location ?? undefined,
		publishedAt: d.releasedDate ?? undefined,
		departments: d.department?.label ? [d.department.label] : undefined,
	};
}

export const smartrecruiters: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const base = `https://api.smartrecruiters.com/v1/companies/${encodeURIComponent(slug)}/postings`;
		const first = await getJson<SrPage>(`${base}?limit=${PAGE}&offset=0`);
		if (!Array.isArray(first.content)) throw new Error(`smartrecruiters ${slug}: unexpected response shape`);
		if (first.totalFound === 0) {
			if (!(await companyExists(slug))) return { status: "gone" };
			return { status: "ok", jobs: [] };
		}

		const postings: SrPosting[] = [...first.content];
		const total = first.totalFound;
		const offsets: number[] = [];
		for (let off = PAGE; off < total; off += PAGE) offsets.push(off);
		const pages = await mapLimit(offsets, CONCURRENCY, (off) => getJson<SrPage>(`${base}?limit=${PAGE}&offset=${off}`));
		for (const p of pages) postings.push(...p.content);

		// De-dupe (pagination without a stable sort can repeat items).
		const seen = new Set<string>();
		const unique = postings.filter((p) => (seen.has(p.id) ? false : (seen.add(p.id), true)));


		const jobs: Job[] = unique.map((p) => ({
			id: String(p.id),
			title: p.name,
			location: locationOf(p),
			url: `https://jobs.smartrecruiters.com/${encodeURIComponent(p.company?.identifier ?? slug)}/${p.id}`,
			departments: [p.department?.label].filter((x): x is string => !!x),
			publishedAt: p.releasedDate ?? null,
			updatedAt: null,
			content: null,
			raw: p,
		}));
		return { status: "ok", jobs };
	},
};

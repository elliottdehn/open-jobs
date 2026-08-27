import type { AtsFetcher, FetchResult, Job } from "./types";
import { fetchRetry } from "./http.ts";

interface WorkableJob {
	title: string;
	shortcode: string;
	code?: string;
	employment_type?: string;
	telecommuting?: boolean;
	department?: string;
	url: string;
	shortlink?: string;
	application_url?: string;
	published_on?: string;
	created_at?: string;
	country?: string;
	city?: string;
	state?: string;
	locations?: { country?: string; city?: string; region?: string; hidden?: boolean }[];
	description?: string;
}

function fmtLoc(l: { country?: string; city?: string; region?: string }): string {
	return [l.city, l.region, l.country].filter(Boolean).join(", ");
}

/** "YYYY-MM-DD" -> ISO midnight UTC. */
function dateToIso(d: string | undefined): string | null {
	if (!d) return null;
	const t = Date.parse(d.length === 10 ? `${d}T00:00:00Z` : d);
	return Number.isNaN(t) ? null : new Date(t).toISOString();
}

/**
 * Workable public widget API: https://apply.workable.com/api/v1/widget/accounts/<account>?details=true
 * (www.workable.com/api/accounts/<account> redirects here). Slugs are the account subdomain
 * (<account>.workable.com / apply.workable.com/<account>). Single response, includes descriptions.
 */
export const workable: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const url = `https://apply.workable.com/api/v1/widget/accounts/${encodeURIComponent(slug)}?details=true`;
		const res = await fetchRetry(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`workable ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { jobs?: WorkableJob[] };
		if (!Array.isArray(body.jobs)) throw new Error(`workable ${slug}: unexpected response shape`);
		const jobs: Job[] = body.jobs.map((j) => {
			const locs = (j.locations ?? []).filter((l) => !l.hidden).map(fmtLoc).filter(Boolean);
			const primary = fmtLoc(j);
			const location = locs.length ? locs.join("; ") : primary || null;
			return {
				id: j.shortcode,
				title: j.title,
				location: j.telecommuting && !location ? "Remote" : location,
				url: j.url || `https://apply.workable.com/j/${j.shortcode}`,
				departments: j.department ? [j.department] : [],
				publishedAt: dateToIso(j.published_on),
				updatedAt: null,
				content: j.description || null,
				raw: j,
			};
		});
		return { status: "ok", jobs };
	},
};

import type { AtsFetcher, FetchResult, Job } from "./types";

/**
 * JobScore: GET https://careers.jobscore.com/careers/{slug}/feed.json
 * -> { jobs: [...] } with full HTML description. Single response, no paging.
 * 404 = unknown company, 410 = company/board removed.
 */

interface JsJob {
	id: string;
	title: string;
	detail_url?: string;
	apply_url?: string;
	url_slug?: string;
	department?: string | null;
	location?: string | null;
	city?: string | null;
	state?: string | null;
	country?: string | null;
	last_updated_date?: string | null;
	opened_date?: string | null;
	created_on?: string | null;
	description?: string | null;
}

function stripRef(u: string): string {
	try {
		const url = new URL(u);
		url.searchParams.delete("ref");
		url.searchParams.delete("sid");
		return url.toString();
	} catch {
		return u;
	}
}

export const jobscore: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const url = `https://careers.jobscore.com/careers/${encodeURIComponent(slug)}/feed.json`;
		const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`jobscore ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { jobs?: JsJob[] };
		if (!Array.isArray(body.jobs)) throw new Error(`jobscore ${slug}: unexpected response shape`);
		const jobs: Job[] = body.jobs.map((j) => ({
			id: j.id,
			title: j.title,
			location: j.location || [j.city, j.state, j.country].filter(Boolean).join(", ") || null,
			url: j.detail_url
				? stripRef(j.detail_url)
				: `https://careers.jobscore.com/careers/${encodeURIComponent(slug)}/jobs/${j.url_slug ?? j.id}`,
			departments: j.department ? [j.department] : [],
			publishedAt: j.opened_date ?? j.created_on ?? null,
			updatedAt: j.last_updated_date ?? null,
			content: j.description ?? null,
			raw: j,
		}));
		return { status: "ok", jobs };
	},
};

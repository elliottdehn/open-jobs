import type { AtsFetcher, FetchResult, Job } from "./types";

interface AshbyJob {
	id: string;
	title: string;
	department?: string | null;
	team?: string | null;
	employmentType?: string;
	location?: string | null;
	secondaryLocations?: { location?: string; address?: unknown }[];
	publishedAt?: string | null;
	isListed?: boolean;
	isRemote?: boolean;
	workplaceType?: string;
	jobUrl: string;
	applyUrl?: string;
	descriptionHtml?: string | null;
	descriptionPlain?: string | null;
}

/**
 * Ashby public posting API: https://api.ashbyhq.com/posting-api/job-board/<board>
 * Slugs are the board name (jobs.ashbyhq.com/<board>). Single response, no pagination.
 */
export const ashby: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const url = `https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(slug)}`;
		const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`ashby ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { jobs?: AshbyJob[] };
		if (!Array.isArray(body.jobs)) throw new Error(`ashby ${slug}: unexpected response shape`);
		const jobs: Job[] = body.jobs
			.filter((j) => j.isListed !== false)
			.map((j) => {
				const locs = [j.location, ...(j.secondaryLocations ?? []).map((s) => s.location)].filter(
					(x): x is string => !!x,
				);
				return {
					id: j.id,
					title: j.title,
					location: locs.length ? locs.join("; ") : null,
					url: j.jobUrl,
					departments: [...new Set([j.department, j.team].filter((x): x is string => !!x))],
					publishedAt: j.publishedAt ?? null,
					updatedAt: null,
					content: j.descriptionHtml || j.descriptionPlain || null,
					raw: j,
				};
			});
		return { status: "ok", jobs };
	},
};

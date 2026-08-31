import type { AtsFetcher, FetchResult, Job } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Jibe / iCIMS Attract career sites (careers.costco.com, …). Slug = the site hostname.
 * Listing: GET https://<host>/api/jobs?page=N&limit=100 — full description included, so no detail stage.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";
const PAGE = 100;
const MAX_PAGES = 400;

interface JibeJob {
	data?: {
		req_id?: string; slug?: string; title?: string; description?: string;
		full_location?: string; location_name?: string; city?: string; country_code?: string;
		category?: string[] | string; categories?: { name?: string }[];
		create_date?: string; posted_date?: string; apply_url?: string; language?: string;
		meta_data?: { canonical_url?: string };
	};
}

export const jibe: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const jobs: Job[] = []; const seen = new Set<string>();
		let total = Infinity;
		for (let page = 1; page <= MAX_PAGES && jobs.length < total; page++) {
			const res = await fetchRetry(`https://${slug}/api/jobs?page=${page}&limit=${PAGE}`, {
				headers: { "user-agent": UA, accept: "application/json" },
			});
			if (res.status === 404) return { status: "gone" };
			if (!res.ok) throw new Error(`jibe api HTTP ${res.status}`);
			const data = (await res.json()) as { totalCount?: number; jobs?: JibeJob[] };
			if (!Array.isArray(data.jobs)) return { status: "gone" }; // not a Jibe site
			total = data.totalCount ?? 0;
			for (const row of data.jobs) {
				const d = row.data ?? {};
				const id = d.req_id || d.slug;
				if (!id || !d.title || seen.has(id)) continue;
				seen.add(id);
				const cats = Array.isArray(d.categories) ? d.categories.map((c) => c?.name).filter((x): x is string => !!x)
					: Array.isArray(d.category) ? (d.category as string[]) : d.category ? [d.category as string] : [];
				jobs.push({
					id,
					title: d.title,
					location: d.full_location || d.location_name || d.city || null,
					url: d.meta_data?.canonical_url || d.apply_url || `https://${slug}/jobs/${encodeURIComponent(d.slug ?? id)}`,
					departments: cats,
					publishedAt: d.posted_date || d.create_date || null,
					updatedAt: null,
					content: d.description || null,
					raw: d,
				});
			}
			if ((data.jobs.length ?? 0) < PAGE) break;
		}
		return { status: "ok", jobs };
	},
};

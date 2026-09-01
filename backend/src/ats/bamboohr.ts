import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * BambooHR hosted careers boards. Slug = the company subdomain (e.g. "acme" for
 * acme.bamboohr.com), mined directly from the Common Crawl index (*.bamboohr.com) rather than by
 * scanning careers pages. Two clean JSON stages:
 *   fetchJobs   — GET /careers/list  -> {result:[{id, jobOpeningName, location, departmentLabel, ...}]}
 *   fetchDetail — GET /careers/{id}/detail -> {result:{jobOpening:{description, datePosted, ...}}}
 * An invalid/closed board 302-redirects to the marketing site; treat that as gone.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";

interface BhLoc { city?: string | null; state?: string | null; province?: string | null; country?: string | null }
interface BhJob {
	id: number | string; jobOpeningName?: string; departmentLabel?: string; employmentType?: string | null;
	location?: BhLoc | null; atsLocation?: BhLoc | null; isRemote?: boolean | null; locationType?: string | null;
}
interface BhDetail {
	jobOpeningName?: string; description?: string; datePosted?: string | null; employmentType?: string | null;
	location?: BhLoc | null; atsLocation?: BhLoc | null; isRemote?: boolean | null; locationType?: string | null;
	departmentLabel?: string; jobOpeningShareUrl?: string;
}

function locStr(loc?: BhLoc | null, isRemote?: boolean | null, locationType?: string | null): string | null {
	const parts = [loc?.city, loc?.state || loc?.province, loc?.country].filter(Boolean);
	const base = parts.join(", ");
	if (isRemote || locationType === "remote") return base ? `Remote - ${base}` : "Remote";
	return base || null;
}

export const bamboohr: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const base = `https://${slug}.bamboohr.com`;
		const res = await fetchRetry(`${base}/careers/list`, { headers: { "user-agent": UA, accept: "application/json" }, redirect: "manual" });
		if (res.status >= 300 && res.status < 400) return { status: "gone" }; // redirect to marketing site: no board
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`bamboohr list HTTP ${res.status}`);
		let data: { result?: BhJob[] };
		try { data = (await res.json()) as { result?: BhJob[] }; } catch { return { status: "gone" }; }
		if (!Array.isArray(data.result)) return { status: "gone" };
		const jobs: Job[] = [];
		for (const j of data.result) {
			if (j.id == null || !j.jobOpeningName) continue;
			const id = String(j.id);
			jobs.push({
				id,
				title: j.jobOpeningName.trim(),
				location: locStr(j.location ?? j.atsLocation, j.isRemote, j.locationType),
				url: `${base}/careers/${id}`,
				departments: j.departmentLabel ? [j.departmentLabel] : [],
				publishedAt: null,
				updatedAt: null,
				content: null, // description arrives via fetchDetail
				raw: null,
			});
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(`https://${slug}.bamboohr.com/careers/${job.id}/detail`, { headers: { "user-agent": UA, accept: "application/json" }, redirect: "manual" });
		if (res.status === 404 || res.status === 410 || (res.status >= 300 && res.status < 400)) return null;
		if (!res.ok) throw new Error(`bamboohr detail HTTP ${res.status}`);
		let data: { result?: { jobOpening?: BhDetail } };
		try { data = (await res.json()) as { result?: { jobOpening?: BhDetail } }; } catch { return null; }
		const d = data.result?.jobOpening;
		if (!d || !d.description) return null;
		return {
			content: d.description,
			title: d.jobOpeningName ? d.jobOpeningName.trim() : null,
			location: locStr(d.location ?? d.atsLocation, d.isRemote, d.locationType),
			publishedAt: d.datePosted ?? null,
			departments: d.departmentLabel ? [d.departmentLabel] : undefined,
		};
	},
};

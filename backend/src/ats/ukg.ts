import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * UKG Pro (UltiPro) job boards. Slug = "<host>:<companyCode>:<boardGuid>", e.g.
 * "recruiting.ultipro.com:AAM1000AAM:c5a88c41-a6d1-4e5d-bf94-4d0432a0df30" (boards are mined from the
 * Common Crawl index; the code+guid pair cannot be derived from a company name).
 * Listing: POST JobBoardView/LoadSearchResults (Top/Skip paging, JSON). Detail: the OpportunityDetail
 * page embeds the opportunity JSON in its HTML; the full Description is extracted from it.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";
const PAGE = 50;
const MAX = 4_000;

interface UkgOpportunity {
	Id?: string; Title?: string; BriefDescription?: string; PostedDate?: string; RequisitionNumber?: string;
	JobCategoryName?: string; FullTime?: boolean;
	Locations?: { LocalizedName?: string; Address?: { City?: string; State?: { Code?: string; Name?: string }; Country?: { Code?: string } } }[];
}

function parts(slug: string): { host: string; code: string; guid: string } | null {
	const [host, code, guid] = slug.split(":");
	return host && code && guid ? { host, code, guid } : null;
}

function locOf(o: UkgOpportunity): string | null {
	const ls = (o.Locations ?? []).map((l) => {
		const a = l.Address;
		return [a?.City, a?.State?.Code || a?.State?.Name, a?.Country?.Code].filter(Boolean).join(", ") || l.LocalizedName || "";
	}).filter(Boolean);
	return ls.slice(0, 4).join("; ") || null;
}

export const ukg: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const p = parts(slug);
		if (!p) return { status: "gone" }; // legacy datacenter-hostname slugs: nothing to fetch
		const base = `https://${p.host}/${p.code}/JobBoard/${p.guid}`;
		const jobs: Job[] = []; const seen = new Set<string>();
		let total = Infinity;
		for (let skip = 0; skip < Math.min(total, MAX); skip += PAGE) {
			const res = await fetchRetry(`${base}/JobBoardView/LoadSearchResults`, {
				method: "POST",
				headers: { "content-type": "application/json", accept: "application/json", "user-agent": UA },
				body: JSON.stringify({
					opportunitySearch: { Top: PAGE, Skip: skip, QueryString: "", OrderBy: [{ Value: "postedDateOpportunity", PropertyName: "PostedDate", Ascending: false }], Filters: [] },
					matchCriteria: { PreferredJobs: [], Educations: [], LicenseAndCertifications: [], Skills: [], hasNoLicenses: false, SkippedSkills: [] },
				}),
			});
			if (res.status === 404 || res.status === 410) return { status: "gone" };
			if (!res.ok) throw new Error(`ukg LoadSearchResults HTTP ${res.status}`);
			const data = (await res.json()) as { totalCount?: number; opportunities?: UkgOpportunity[] };
			if (!Array.isArray(data.opportunities)) return { status: "gone" };
			total = data.totalCount ?? 0;
			for (const o of data.opportunities) {
				if (!o.Id || !o.Title || seen.has(o.Id)) continue;
				seen.add(o.Id);
				jobs.push({
					id: o.Id,
					title: o.Title,
					location: locOf(o),
					url: `${base}/OpportunityDetail?opportunityId=${o.Id}`,
					departments: o.JobCategoryName ? [o.JobCategoryName] : [],
					publishedAt: o.PostedDate ?? null,
					updatedAt: null,
					content: o.BriefDescription ?? null, // brief; the full description arrives via fetchDetail
					raw: null,
				});
			}
			if ((data.opportunities.length ?? 0) < PAGE) break;
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`ukg OpportunityDetail HTTP ${res.status}`);
		const html = await res.text();
		// the page embeds the opportunity JSON; pull the escaped Description string and JSON-decode it
		const m = html.match(/"Description":"((?:[^"\\]|\\.)*)"/);
		if (!m) return null;
		try {
			const content = JSON.parse(`"${m[1]}"`) as string;
			return content ? { content } : null;
		} catch { return null; }
	},
};

import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Phenom People career sites (jobs.cvshealth.com, careers.cencora.com, …). Slug = the site hostname.
 * Listing: POST https://<host>/widgets with ddoKey "refineSearch", paged by from/size (size 100 works);
 * jobs carry only a teaser, so the full description comes from the job page's embedded `phApp.ddo` JSON
 * (fetchDetail, once per job). The search window is capped defensively at 10k rows.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";
const PAGE = 100;
const MAX = 10_000;

interface PhenomJob {
	jobId?: string;
	reqId?: string;
	jobSeqNo?: string;
	title?: string;
	location?: string;
	cityStateCountry?: string;
	category?: string;
	postedDate?: string;
	dateCreated?: string;
	descriptionTeaser?: string;
	applyUrl?: string;
}

function widgetBody(from: number, size: number): string {
	return JSON.stringify({
		lang: "en_us", deviceType: "desktop", country: "us", pageName: "search-results",
		ddoKey: "refineSearch", sortBy: "", subsearch: "", from, jobs: true, counts: true,
		all_fields: [], size, clearAll: false, jdsource: "facets", isSliderEnable: false,
		pageId: "page1", siteType: "external", keywords: "", global: true,
		selected_fields: {}, locationData: {},
	});
}

function toJob(host: string, p: PhenomJob): Job | null {
	const id = p.jobSeqNo || p.reqId || p.jobId;
	if (!id || !p.title) return null;
	return {
		id,
		title: p.title,
		location: p.cityStateCountry || p.location || null,
		url: `https://${host}/us/en/job/${encodeURIComponent(id)}`,
		departments: p.category ? [p.category] : [],
		publishedAt: p.postedDate || p.dateCreated || null,
		updatedAt: null,
		content: p.descriptionTeaser || null, // teaser; the real description arrives via fetchDetail
		raw: { ...p, ml_skills: undefined, ml_job_parser: undefined } as unknown, // ml_* blobs are the bulk of the payload
	};
}

/** Decode the HTML-entity-escaped HTML Phenom embeds ("&lt;p&gt;…"), so content looks like other providers'. */
function unescapeHtml(s: string): string {
	return s.replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (m, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", nbsp: " " })[e as string] ?? m);
}

/** Extract the `phApp.ddo = {...};` JSON blob from a job page with a brace counter. */
function ddoJson(html: string): unknown | null {
	const i = html.indexOf("phApp.ddo");
	if (i < 0) return null;
	const start = html.indexOf("{", i);
	if (start < 0) return null;
	let depth = 0, inStr = false, esc = false;
	for (let j = start; j < html.length && j < start + 2_000_000; j++) {
		const c = html[j];
		if (inStr) {
			if (esc) esc = false;
			else if (c === "\\") esc = true;
			else if (c === '"') inStr = false;
			continue;
		}
		if (c === '"') inStr = true;
		else if (c === "{") depth++;
		else if (c === "}") { depth--; if (depth === 0) { try { return JSON.parse(html.slice(start, j + 1)); } catch { return null; } } }
	}
	return null;
}

export const phenom: AtsFetcher = {
	async fetchJobsStream(slug, sink): Promise<{ status: "ok" } | { status: "gone" }> {
		const seen = new Set<string>();
		let total = Infinity;
		for (let from = 0; from < Math.min(total, MAX); from += PAGE) {
			const res = await fetchRetry(`https://${slug}/widgets`, {
				method: "POST",
				headers: { "content-type": "application/json", "user-agent": UA, accept: "application/json" },
				body: widgetBody(from, PAGE),
			});
			if (res.status === 404) return { status: "gone" };
			if (!res.ok) throw new Error(`phenom widgets HTTP ${res.status}`);
			const data = (await res.json()) as { refineSearch?: { totalHits?: number; data?: { jobs?: PhenomJob[] } } };
			const rs = data.refineSearch;
			if (!rs || !rs.data) return { status: "gone" }; // not a phenom site (or widget disabled)
			total = rs.totalHits ?? 0;
			const page = rs.data.jobs ?? [];
			const out: Job[] = [];
			for (const p of page) {
				const j = toJob(slug, p);
				if (j && !seen.has(j.id)) { seen.add(j.id); out.push(j); }
			}
			if (out.length) await sink(out);
			if (page.length < PAGE) break;
		}
		return { status: "ok" };
	},

	async fetchJobs(slug: string): Promise<FetchResult> {
		const jobs: Job[] = [];
		const res = await this.fetchJobsStream!(slug, async (page) => { jobs.push(...page); });
		return res.status === "gone" ? { status: "gone" } : { status: "ok", jobs };
	},

	async fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (res.status === 403) return null; // WAF blocks Cloudflare egress (e.g. RTX): retrying daily won't help; keep the listing teaser
		if (!res.ok) throw new Error(`phenom job page HTTP ${res.status}`);
		const ddo = ddoJson(await res.text()) as { jobDetail?: { data?: { job?: { description?: string; qualifications?: string } } } } | null;
		const j = ddo?.jobDetail?.data?.job;
		if (!j?.description) return null;
		const content = unescapeHtml(j.description + (j.qualifications ? `\n${j.qualifications}` : ""));
		// content only: storing the whole ddo job object as raw (~50-100 KB/job) OOMs a 10k-job board's DO
		return { content };
	},
};

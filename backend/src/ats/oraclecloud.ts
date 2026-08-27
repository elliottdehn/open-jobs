import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Oracle Cloud HCM Recruiting (Candidate Experience). Slug is the Fusion hostname, e.g.
 * `eeho.fa.us2.oraclecloud.com`. Sites are discovered via GET /hcmRestApi/resources/latest/recruitingCESites
 * (only ORA_ACTIVE ones are used; falls back to CX_1). Listing: recruitingCEJobRequisitions with
 * finder=findReqs;siteNumber=...,limit=200,offset=N (server caps pages at 200).
 */

interface OcSite {
	SiteNumber: string;
	SiteName?: string;
	StatusCode?: string;
}

interface OcReq {
	Id: string | number;
	Title: string;
	PostedDate?: string | null;
	PrimaryLocation?: string | null;
	JobFamily?: string | null;
	Department?: string | null;
	Organization?: string | null;
	ShortDescriptionStr?: string | null;
	ExternalQualificationsStr?: string | null;
	ExternalResponsibilitiesStr?: string | null;
	secondaryLocations?: { Name?: string }[];
}

interface OcList {
	items: { TotalJobsCount?: number; requisitionList?: OcReq[] }[];
}

const UA = "open-jobs/0.1";
const PAGE = 200;
const CONCURRENCY = 6;
const HEADERS = { "user-agent": UA, accept: "application/json" };

function base(host: string): string {
	return `https://${host}/hcmRestApi/resources/latest`;
}

/** Unknown Fusion hosts are answered by the Akamai edge with a generic 503/504 error page. */
async function isEdgeError(res: Response): Promise<boolean> {
	if (res.status !== 503 && res.status !== 504) return false;
	if (res.headers.has("akgrn")) return true; // Akamai reference header, only on edge-generated errors
	const t = await res.text();
	return /edgesuite(\.|&#46;)net|DNS failure|An error occurred while processing your request/i.test(t);
}

async function discoverSites(host: string): Promise<string[] | "gone"> {
	const res = await fetch(`${base(host)}/recruitingCESites?onlyData=true`, { headers: HEADERS });
	if (res.status === 404) return "gone";
	if (await isEdgeError(res)) return "gone";
	if (!res.ok) return ["CX_1"]; // site listing unavailable; try the default site
	const body = (await res.json()) as { items?: OcSite[] };
	const items = body.items ?? [];
	const active = items.filter((s) => !s.StatusCode || s.StatusCode === "ORA_ACTIVE").map((s) => s.SiteNumber);
	if (active.length) return [...new Set(active)];
	if (items.length) return [...new Set(items.map((s) => s.SiteNumber))];
	return ["CX_1"];
}

async function fetchPage(host: string, site: string, offset: number): Promise<{ total: number; reqs: OcReq[] }> {
	const finder = `findReqs;siteNumber=${site},limit=${PAGE},offset=${offset},sortBy=POSTING_DATES_DESC`;
	const url = `${base(host)}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=${finder}`;
	const res = await fetch(url, { headers: HEADERS });
	if (!res.ok) throw new Error(`oraclecloud ${host}/${site}: HTTP ${res.status}`);
	const body = (await res.json()) as OcList;
	const item = body.items?.[0];
	if (!item) throw new Error(`oraclecloud ${host}/${site}: unexpected shape`);
	return { total: item.TotalJobsCount ?? 0, reqs: item.requisitionList ?? [] };
}

function toJob(host: string, site: string, r: OcReq): Job {
	const parts = [r.ShortDescriptionStr, r.ExternalResponsibilitiesStr, r.ExternalQualificationsStr].filter(
		(s): s is string => !!s && s.trim().length > 0,
	);
	const locs = [r.PrimaryLocation, ...(r.secondaryLocations ?? []).map((l) => l.Name)].filter((l): l is string => !!l);
	return {
		id: String(r.Id),
		title: r.Title,
		location: locs.length ? locs.join("; ") : null,
		url: `https://${host}/hcmUI/CandidateExperience/en/sites/${site}/job/${encodeURIComponent(String(r.Id))}`,
		departments: [...new Set([r.JobFamily, r.Department].filter((d): d is string => !!d))],
		publishedAt: r.PostedDate ?? null,
		updatedAt: null,
		content: parts.length ? parts.join("\n\n") : null,
		raw: r,
	};
}

async function fetchSite(host: string, site: string, out: Map<string, Job>): Promise<void> {
	const first = await fetchPage(host, site, 0);
	const add = (r: OcReq) => {
		const id = String(r.Id);
		if (!out.has(id)) out.set(id, toJob(host, site, r));
	};
	first.reqs.forEach(add);
	const offsets: number[] = [];
	for (let o = PAGE; o < first.total; o += PAGE) offsets.push(o);
	let next = 0;
	const worker = async () => {
		while (next < offsets.length) {
			const page = await fetchPage(host, site, offsets[next++]);
			page.reqs.forEach(add);
		}
	};
	await Promise.all(Array.from({ length: Math.min(CONCURRENCY, offsets.length) }, worker));
}

/** Listing content at least this long is kept as-is; shorter ones get a detail request. */
const DETAIL_MIN_CONTENT = 1500;

interface OcDetail extends OcReq {
	ExternalDescriptionStr?: string | null;
	ExternalPostedStartDate?: string | null;
}

/**
 * Detail: GET recruitingCEJobRequisitionDetails?onlyData=true&q=Id={Id} → ExternalDescriptionStr +
 * ExternalResponsibilitiesStr + ExternalQualificationsStr (HTML). (The documented `ByRequisitionId`
 * finder answers 400 on every tenant tested; the `q` filter works.) Unknown/closed ids return an empty list.
 * Skipped when the listing already carried a substantial description.
 */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	if (job.content && job.content.length >= DETAIL_MIN_CONTENT) return { content: job.content };
	const host = new URL(job.url).hostname;
	const id = String((job.raw as OcReq | undefined)?.Id ?? job.id);
	if (!/^[\w-]+$/.test(id)) throw new Error(`oraclecloud detail ${host}: bad id ${id}`);
	const url = `${base(host)}/recruitingCEJobRequisitionDetails?onlyData=true&q=Id=${id}`;
	const res = await fetchRetry(url, { headers: HEADERS });
	if (res.status === 404) return null;
	if (!res.ok) throw new Error(`oraclecloud detail ${host}/${id}: HTTP ${res.status}`);
	const body = (await res.json()) as { items?: OcDetail[] };
	const d = body.items?.[0];
	if (!d) return null;
	const parts = [d.ExternalDescriptionStr, d.ExternalResponsibilitiesStr, d.ExternalQualificationsStr].filter(
		(s): s is string => !!s && s.trim().length > 0,
	);
	const locs = [d.PrimaryLocation, ...(d.secondaryLocations ?? []).map((l) => l.Name)].filter((l): l is string => !!l);
	return {
		content: parts.length ? parts.join("\n\n") : job.content,
		raw: d,
		location: locs.length ? locs.join("; ") : undefined,
		publishedAt: d.ExternalPostedStartDate ?? undefined,
	};
}

export const oraclecloud: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const host = slug.replace(/^https?:\/\//, "").replace(/\/.*$/, "").toLowerCase();
		if (!host.endsWith("oraclecloud.com")) throw new Error(`oraclecloud: bad slug ${slug}`);
		const sites = await discoverSites(host);
		if (sites === "gone") return { status: "gone" };
		const out = new Map<string, Job>();
		for (const site of sites) await fetchSite(host, site, out);
		return { status: "ok", jobs: [...out.values()] };
	},
};

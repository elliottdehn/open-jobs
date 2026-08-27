import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Workday (myworkdayjobs.com). Slug is the tenant hostname, e.g. `gdit.wd5.myworkdayjobs.com`.
 * Tenant = first host label. Career-site names are discovered from /robots.txt (`Allow: /<site>/`
 * lines and `Sitemap: .../<site>/siteMap.xml`; the host root itself answers 406 to non-browsers).
 * Listing: POST /wday/cxs/{tenant}/{site}/jobs {limit:20, offset, searchText:""}; max page size is 20.
 */

interface WdPosting {
	title: string;
	externalPath: string;
	locationsText?: string;
	postedOn?: string;
	bulletFields?: string[];
}

interface WdList {
	total: number;
	jobPostings: WdPosting[];
}

const UA = "open-jobs/0.1";
const PAGE = 20;
const CONCURRENCY = 6;
const NOT_SITES = new Set(["refreshFacet", "events", "wday"]);

async function discoverSites(host: string): Promise<string[] | "gone"> {
	const res = await fetch(`https://${host}/robots.txt`, { headers: { "user-agent": UA } });
	if (res.status === 422 || res.status === 404) return "gone";
	// Unknown tenants answer 500 on every path (a real tenant would 404 or serve robots).
	if (res.status === 500) return "gone";
	if (!res.ok) throw new Error(`workday ${host}: robots.txt HTTP ${res.status}`);
	const text = await res.text();
	const allow: string[] = [];
	const disallow: string[] = [];
	for (const line of text.split(/\r?\n/)) {
		const m = /^\s*(Allow|Disallow|Sitemap)\s*:\s*(\S+)/i.exec(line);
		if (!m) continue;
		const kind = m[1].toLowerCase();
		let site: string | undefined;
		if (kind === "sitemap") site = /\/([^/]+)\/siteMap\.xml/i.exec(m[2])?.[1];
		else site = /^\/([^/]+)\/?$/.exec(m[2])?.[1];
		if (!site || NOT_SITES.has(site)) continue;
		(kind === "disallow" ? disallow : allow).push(site);
	}
	const uniq = (a: string[]) => [...new Set(a)];
	// Public sites are the Allow'd/sitemapped ones; some tenants only list Disallow'd site paths.
	return allow.length ? uniq(allow) : uniq(disallow);
}

/** "Posted Today" / "Posted Yesterday" / "Posted 3 Days Ago" -> ISO date; "30+ Days Ago" -> null. */
function parsePostedOn(s: string | undefined): string | null {
	if (!s) return null;
	const t = s.toLowerCase();
	let days: number;
	if (/today/.test(t)) days = 0;
	else if (/yesterday/.test(t)) days = 1;
	else {
		const m = /(\d+)\s*days?\s*ago/.exec(t);
		if (!m || /\+/.test(t)) return null;
		days = Number(m[1]);
	}
	const d = new Date(Date.now() - days * 86400_000);
	return d.toISOString().slice(0, 10);
}

async function fetchPage(host: string, tenant: string, site: string, offset: number): Promise<WdList | "nosite"> {
	const res = await fetch(`https://${host}/wday/cxs/${tenant}/${site}/jobs`, {
		method: "POST",
		headers: { "user-agent": UA, accept: "application/json", "content-type": "application/json" },
		body: JSON.stringify({ limit: PAGE, offset, searchText: "" }),
	});
	if (res.status === 404 || res.status === 400) return "nosite";
	if (!res.ok) throw new Error(`workday ${host}/${site}: HTTP ${res.status}`);
	const body = (await res.json()) as WdList;
	if (!Array.isArray(body.jobPostings)) throw new Error(`workday ${host}/${site}: unexpected shape`);
	return body;
}

async function fetchSite(host: string, tenant: string, site: string, out: Map<string, Job>): Promise<void> {
	const first = await fetchPage(host, tenant, site, 0);
	if (first === "nosite") return;
	const add = (p: WdPosting) => {
		const id = p.externalPath;
		if (!id || out.has(id)) return;
		out.set(id, {
			id,
			title: p.title,
			location: p.locationsText ?? null,
			url: `https://${host}/${site}${p.externalPath}`,
			departments: [],
			publishedAt: parsePostedOn(p.postedOn),
			updatedAt: null,
			content: null,
			raw: p,
		});
	};
	first.jobPostings.forEach(add);
	const total = first.total ?? first.jobPostings.length;
	const offsets: number[] = [];
	for (let o = PAGE; o < total; o += PAGE) offsets.push(o);
	let next = 0;
	const worker = async () => {
		while (next < offsets.length) {
			const o = offsets[next++];
			const page = await fetchPage(host, tenant, site, o);
			if (page === "nosite" || page.jobPostings.length === 0) return;
			// Note: only the first page carries a real `total`; later pages report 0.
			page.jobPostings.forEach(add);
		}
	};
	await Promise.all(Array.from({ length: Math.min(CONCURRENCY, offsets.length) }, worker));
}

interface WdDetail {
	jobPostingInfo?: {
		jobDescription?: string;
		location?: string;
		additionalLocations?: string[];
		startDate?: string;
		posted?: boolean;
		jobReqId?: string;
	};
}

/** Detail: GET /wday/cxs/{tenant}/{site}{externalPath} → jobPostingInfo.jobDescription (HTML). */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const u = new URL(job.url);
	const host = u.hostname;
	const tenant = host.split(".")[0];
	const externalPath = (job.raw as { externalPath?: string } | undefined)?.externalPath ?? u.pathname.replace(/^\/[^/]+/, "");
	const site = u.pathname.split("/")[1];
	const res = await fetchRetry(`https://${host}/wday/cxs/${tenant}/${site}${externalPath}`, {
		headers: { accept: "application/json", "user-agent": UA },
	});
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`workday detail ${host}${externalPath}: HTTP ${res.status}`);
	const body = (await res.json()) as WdDetail;
	const info = body.jobPostingInfo;
	if (!info) return null;
	const locs = [info.location, ...(info.additionalLocations ?? [])].filter(Boolean) as string[];
	return {
		content: info.jobDescription ?? null,
		raw: info,
		location: locs.length ? locs.join("; ") : undefined,
	};
}

export const workday: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const host = slug.replace(/^https?:\/\//, "").replace(/\/.*$/, "").toLowerCase();
		const tenant = host.split(".")[0];
		if (!tenant || !host.includes("myworkdayjobs.com")) throw new Error(`workday: bad slug ${slug}`);
		const sites = await discoverSites(host);
		if (sites === "gone") return { status: "gone" };
		if (sites.length === 0) throw new Error(`workday ${host}: no career sites found in robots.txt`);
		const out = new Map<string, Job>();
		for (const site of sites) await fetchSite(host, tenant, site, out);
		return { status: "ok", jobs: [...out.values()] };
	},
};

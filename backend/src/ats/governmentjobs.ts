import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * GovernmentJobs.com (NEOGOV) — the consolidated public job search for US state & local government
 * (cities, counties, school districts, special districts). One global search at /jobs spans every
 * agency (~33k live postings); the page size is fixed at 10, so a single board can't drain it in one
 * alarm. We shard by US state via the ?location= filter (slug = the state name, e.g. "California"):
 * a job's state is stable across runs, so per-board diffs stay clean, and the 51 state/territory
 * shards cover ~99% of the global count with negligible overlap.
 *
 * Two stages, like the other JSON-LD fetchers:
 *   fetchJobs   — page /jobs?location={state}&page=N, harvest the job-item rows (id/title/org/loc).
 *   fetchDetail — fetch /jobs/{id}/{slug} and pull the JobPosting JSON-LD (full JD, structured
 *                 salary, datePosted, hiringOrganization). NEOGOV serves it statically.
 */
const HOST = "https://www.governmentjobs.com";
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs-crawler/0.1 (+github.com/elliottdehn/open-jobs)";
const PER_PAGE = 10; // server-fixed
// NEOGOV reshuffles its paging after ~65 pages, repeating earlier jobs before surfacing the rest, so
// distinct jobs accrue at ~6-7/page (not 10) and the full set for a big shard needs far more than
// total/10 pages. Cap generously (California ~6.5k jobs ≈ ~1k pages) and stop on the parsed total or a
// stall. Cost is local CPU/bandwidth (governmentjobs is localOnly), so a slow shard is fine.
const MAX_PAGES = 400; // ~4k distinct at the reshuffle's ~6-7 new/page; bounds the biggest shards' tails
const STALL_PAGES = 30; // consecutive pages with no new job -> the shard is exhausted

function decodeEntities(s: string): string {
	return s.replace(/&(amp|lt|gt|quot|#0?39|apos|nbsp|#\d+|#x[0-9a-f]+);/gi, (m, e: string) => {
		const l = e.toLowerCase();
		if (l === "amp") return "&"; if (l === "lt") return "<"; if (l === "gt") return ">";
		if (l === "quot") return '"'; if (l === "apos" || l === "#39" || l === "#039") return "'"; if (l === "nbsp") return " ";
		if (l.startsWith("#x")) return String.fromCodePoint(parseInt(l.slice(2), 16));
		if (l.startsWith("#")) return String.fromCodePoint(parseInt(l.slice(1), 10));
		return m;
	});
}
function textOf(html: string): string { return decodeEntities(html.replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim(); }

/** Parse the job-item rows out of one /jobs listing page. */
function parseListing(html: string): Job[] {
	const jobs: Job[] = [];
	// each posting is a <li class="job-item" data-job-id="ID">...</li>
	const parts = html.split(/<li class="job-item"/);
	for (let i = 1; i < parts.length; i++) {
		const blk = parts[i];
		const id = blk.match(/data-job-id="([^"]+)"/)?.[1];
		const a = blk.match(/<a[^>]*class="job-details-link"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/);
		if (!id || !a) continue;
		const href = a[1]; const title = textOf(a[2]);
		if (!title) continue;
		const org = blk.match(/class="[^"]*job-organization"[^>]*>([\s\S]*?)<\/div>/)?.[1];
		const loc = blk.match(/class="job-location"[^>]*>([\s\S]*?)<\/span>/)?.[1];
		// the salary/type/closing summary line: the primaryInfo div that isn't org/location
		const summary = blk.match(/<div class="primaryInfo">\s*([^<][\s\S]*?)<\/div>/)?.[1];
		jobs.push({
			id,
			title,
			location: loc ? textOf(loc) : null,
			url: href.startsWith("http") ? href : HOST + href,
			departments: [],
			publishedAt: null,
			updatedAt: null,
			content: summary ? textOf(summary) : null, // short snippet; full JD arrives via fetchDetail
			raw: org ? { org: textOf(org) } : null,
		});
	}
	return jobs;
}

interface JobPostingLD {
	"@type"?: string | string[]; title?: string; description?: string; datePosted?: string;
	hiringOrganization?: { name?: string } | string;
	jobLocation?: JobLoc | JobLoc[];
	applicantLocationRequirements?: { name?: string } | { name?: string }[]; jobLocationType?: string;
}
interface JobLoc { address?: { addressLocality?: string; addressRegion?: string; addressCountry?: string | { name?: string } } }

function isType(t: unknown, name: string): boolean { return t === name || (Array.isArray(t) && t.includes(name)); }

function findJobPosting(html: string): JobPostingLD | null {
	for (const m of html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
		let data: unknown;
		try { data = JSON.parse(m[1].trim()); } catch { continue; }
		const stack: unknown[] = [data];
		while (stack.length) {
			const x = stack.pop();
			if (Array.isArray(x)) { stack.push(...x); continue; }
			if (x && typeof x === "object") {
				const o = x as Record<string, unknown>;
				if (isType(o["@type"], "JobPosting")) return o as JobPostingLD;
				if (Array.isArray(o["@graph"])) stack.push(...(o["@graph"] as unknown[]));
				else for (const v of Object.values(o)) if (v && typeof v === "object") stack.push(v);
			}
		}
	}
	return null;
}

function countryStr(c: NonNullable<JobLoc["address"]>["addressCountry"]): string | undefined {
	if (!c) return undefined; return typeof c === "string" ? c : c.name;
}
function locationOf(d: JobPostingLD): string | null {
	const arr = Array.isArray(d.jobLocation) ? d.jobLocation : d.jobLocation ? [d.jobLocation] : [];
	const parts = arr.map((l) => [l?.address?.addressLocality, l?.address?.addressRegion, countryStr(l?.address?.addressCountry)]
		.filter(Boolean).join(", ")).filter(Boolean);
	if (parts.length) return parts.slice(0, 4).join("; ");
	const rem = Array.isArray(d.applicantLocationRequirements) ? d.applicantLocationRequirements : d.applicantLocationRequirements ? [d.applicantLocationRequirements] : [];
	const remote = rem.map((r) => r?.name).filter(Boolean).join(", ");
	if (d.jobLocationType === "TELECOMMUTE" || remote) return `Remote${remote ? " - " + remote : ""}`;
	return null;
}

export const governmentjobs: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const loc = encodeURIComponent(slug);
		const jobs: Job[] = []; const seen = new Set<string>();
		// NEOGOV paginates statefully per visitor session: the first request sets an `online_visitor`
		// cookie, and only requests carrying it get stable, non-overlapping pages. Cloudflare egresses
		// from rotating IPs, so without pinning the cookie each page returns a fresh random slice and
		// pagination collapses to a few hundred jobs. Capture the cookie from page 1 and send it on.
		let cookie = "";
		let total = Infinity; // parsed from the first page's "N jobs" header
		let stall = 0;
		for (let page = 1; page <= MAX_PAGES && seen.size < total; page++) {
			const headers: Record<string, string> = { "user-agent": UA, "x-requested-with": "XMLHttpRequest", accept: "text/html" };
			if (cookie) headers.cookie = cookie;
			const res = await fetchRetry(`${HOST}/jobs?location=${loc}&page=${page}`, { headers });
			if (res.status === 404 || res.status === 410) return { status: "gone" };
			if (!res.ok) throw new Error(`governmentjobs listing HTTP ${res.status}`);
			const html = await res.text();
			if (!cookie) {
				const sc = res.headers.get("set-cookie") ?? "";
				const ov = sc.match(/online_visitor=[^;]+/)?.[0];
				if (ov) cookie = ov;
			}
			if (total === Infinity) { const m = html.match(/([0-9,]+)\s*jobs\b/); if (m) total = Number(m[1].replace(/,/g, "")); }
			const page_jobs = parseListing(html);
			if (page_jobs.length === 0) break; // ran off the end
			let added = 0;
			for (const j of page_jobs) { if (seen.has(j.id)) continue; seen.add(j.id); jobs.push(j); added++; }
			// NEOGOV repeats jobs once it exhausts fresh ones, so stop on a run of no-new pages rather
			// than the first duplicate (deeper pages still surface jobs earlier ones didn't).
			stall = added === 0 ? stall + 1 : 0;
			if (stall >= STALL_PAGES) break;
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`governmentjobs job page HTTP ${res.status}`);
		const d = findJobPosting(await res.text());
		if (!d || !d.description) return null; // posting pulled or not yet rendered
		const org = typeof d.hiringOrganization === "object" ? d.hiringOrganization?.name : d.hiringOrganization;
		return {
			content: decodeEntities(d.description),
			title: d.title ? decodeEntities(d.title).slice(0, 200) : null,
			location: locationOf(d),
			publishedAt: d.datePosted ?? null,
			org: org ? decodeEntities(org).slice(0, 120) : null,
		};
	},
};

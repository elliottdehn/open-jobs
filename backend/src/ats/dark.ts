import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * "Dark pool" — bespoke company career sites that don't use a shared ATS but publish schema.org
 * JobPosting JSON-LD in static HTML (the set Web Data Commons extracts from Common Crawl; CC doesn't
 * run JS, so anything WDC found is statically readable — which is exactly what a DO can fetch).
 *
 * Slug = the careers host (e.g. "careers.acme.com" or "www.acme.com"). Two stages, like the other
 * JSON-LD fetchers:
 *   fetchJobs  — discovery: read the site's sitemaps, return the current job-page URLs as the listing.
 *   fetchDetail— fetch one job page, extract the JobPosting JSON-LD (title/location/description/date).
 *
 * Sites that went SPA-only since the WDC crawl, or that bot-wall egress, yield nothing here and belong
 * to the render-needed residual (see CRAWLER.md); the seed-builder only creates `dark` boards for hosts
 * a static probe confirms.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs-crawler/0.1 (+github.com/elliottdehn/open-jobs)";
const MAX_JOBS = 3000;
const MAX_SITEMAPS = 25; // sub-sitemaps followed per board
// Static/asset URLs that pattern-match a job path (career.css, /feed/, bundle.js) but aren't jobs.
const ASSET = /\.(css|js|mjs|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|pdf|xml|json|rss|zip|mp4|webm)(\?|#|$)/i;
// A job DETAIL url: a job/career/vacancy segment followed by a slug or id (excludes bare landings and
// listing/category/search pages), or a job-id query param. Callers also apply !ASSET.
const JOB_URL_RE = /(?:\/job[s]?\/(?![s]?\/?$)[^/?#]{2,}|\/(?:career|careers|vacan\w*|position|opening|stelle|offre|emploi|puesto)\/[^/?#]{2,}|[?&](?:jobid|job_id|gh_jid|reqid|requisitionid|opportunityid)=)/i;
const NOT_JOB = /\/(feed|rss|sitemap|category|categories|search|tag|tags|page|about|contact|privacy|cookie|login|apply-tips|faq)(\/|$|\?)/i;
function isJobUrl(u: string): boolean { return JOB_URL_RE.test(u) && !ASSET.test(u) && !NOT_JOB.test(u); }
const JOB_SITEMAP = /job|career|vacan|stelle|offre|position|emploi|puesto/i;

function locs(xml: string): string[] {
	const out: string[] = [];
	for (const m of xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)) out.push(m[1]);
	return out;
}

/** Collect current job-page URLs from the host's sitemaps (index → sub-sitemaps → job URLs). */
async function discover(slug: string): Promise<string[]> {
	const roots = new Set<string>();
	for (const p of ["/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/job-sitemap.xml", "/sitemaps/jobs.xml"]) {
		try {
			const res = await fetchRetry(`https://${slug}${p}`, { headers: { "user-agent": UA } });
			if (!res.ok) continue;
			const body = await res.text();
			if (p === "/robots.txt") for (const m of body.matchAll(/(?:^|\n)\s*sitemap:\s*(\S+)/gi)) roots.add(m[1].trim());
			else for (const u of locs(body)) roots.add(u);
		} catch { /* ignore */ }
	}
	const jobs = new Set<string>();
	const subs = [...roots].filter((u) => JOB_SITEMAP.test(u) && /\.xml($|\?|\.gz)/i.test(u));
	const toScan = (subs.length ? subs : [...roots].filter((u) => /\.xml($|\?)/i.test(u))).slice(0, MAX_SITEMAPS);
	// any job URLs already listed directly in the roots
	for (const u of roots) if (isJobUrl(u)) jobs.add(u);
	for (const sm of toScan) {
		if (jobs.size >= MAX_JOBS) break;
		try {
			const res = await fetchRetry(sm, { headers: { "user-agent": UA } });
			if (!res.ok) continue;
			for (const u of locs(await res.text())) if (isJobUrl(u)) jobs.add(u);
		} catch { /* ignore */ }
	}
	// Discovery fallback: many sites list jobs only in the careers-page HTML, not in a sitemap. When the
	// sitemap pass came up short, harvest job-shaped links straight from the careers landing pages.
	if (jobs.size < 3) {
		for (const p of ["/careers", "/jobs", "/", "/en/careers", "/karriere", "/careers/jobs", "/join-us"]) {
			if (jobs.size >= MAX_JOBS) break;
			try {
				const res = await fetchRetry(`https://${slug}${p}`, { headers: { "user-agent": UA } });
				if (!res.ok) continue;
				const html = await res.text();
				for (const m of html.matchAll(/<a[^>]+href=["']([^"'#]+)["']/gi)) {
					if (!isJobUrl(m[1])) continue;
					try { jobs.add(new URL(m[1], `https://${slug}${p}`).href.replace(/#.*$/, "")); } catch { /* skip bad href */ }
				}
				if (jobs.size >= 3) break; // a landing page that yielded links is enough
			} catch { /* ignore */ }
		}
	}
	return [...jobs].slice(0, MAX_JOBS);
}

/** Title guess from a job URL slug, used until fetchDetail supplies the real one. */
function titleFromUrl(u: string): string {
	try {
		const seg = new URL(u).pathname.split("/").filter(Boolean).pop() ?? "";
		const t = decodeURIComponent(seg).replace(/\.(html?|aspx?|php)$/i, "").replace(/[-_]+/g, " ").replace(/\b\d{4,}\b/g, "").trim();
		return t ? t.replace(/\b\w/g, (c) => c.toUpperCase()).slice(0, 140) : "(position)";
	} catch { return "(position)"; }
}

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

interface JobPostingLD {
	"@type"?: string | string[]; title?: string; description?: string; datePosted?: string; validThrough?: string;
	employmentType?: string | string[]; identifier?: unknown;
	hiringOrganization?: { name?: string } | string;
	jobLocation?: JobLoc | JobLoc[];
	applicantLocationRequirements?: { name?: string } | { name?: string }[]; jobLocationType?: string;
}
interface JobLoc { address?: { addressLocality?: string; addressRegion?: string; addressCountry?: string | { name?: string } } }

function isType(t: unknown, name: string): boolean {
	return t === name || (Array.isArray(t) && t.includes(name));
}

/** Find the first JobPosting node in any JSON-LD block on the page (handles arrays and @graph). */
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
			}
		}
	}
	return null;
}

function countryStr(c: JobLoc["address"] extends undefined ? never : NonNullable<JobLoc["address"]>["addressCountry"]): string | undefined {
	if (!c) return undefined;
	return typeof c === "string" ? c : c.name;
}

function locationOf(d: JobPostingLD): string | null {
	const arr = Array.isArray(d.jobLocation) ? d.jobLocation : d.jobLocation ? [d.jobLocation] : [];
	const parts = arr.map((l) => [l?.address?.addressLocality, l?.address?.addressRegion, countryStr(l?.address?.addressCountry)].filter(Boolean).join(", ")).filter(Boolean);
	if (parts.length) return parts.slice(0, 4).join("; ");
	const rem = Array.isArray(d.applicantLocationRequirements) ? d.applicantLocationRequirements : d.applicantLocationRequirements ? [d.applicantLocationRequirements] : [];
	const remote = rem.map((r) => r?.name).filter(Boolean).join(", ");
	if (d.jobLocationType === "TELECOMMUTE" || remote) return `Remote${remote ? " - " + remote : ""}`;
	return null;
}

export const dark: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const urls = await discover(slug);
		if (!urls.length) return { status: "gone" }; // no sitemap job URLs: not a static-crawlable board
		const jobs: Job[] = urls.map((u) => ({
			id: u.replace(/^https?:\/\//, "").replace(/[#?].*$/, ""), // stable per job URL
			title: titleFromUrl(u),
			location: null,
			url: u,
			departments: [],
			publishedAt: null,
			updatedAt: null,
			content: null, // the JobPosting JSON-LD arrives via fetchDetail
			raw: null,
		}));
		return { status: "ok", jobs };
	},

	async fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`dark job page HTTP ${res.status}`);
		const d = findJobPosting(await res.text());
		if (!d || !d.description) return null; // no JobPosting markup on this page (SPA/removed): drop it
		const content = decodeEntities(d.description);
		const org = typeof d.hiringOrganization === "object" ? d.hiringOrganization?.name : d.hiringOrganization;
		return {
			content,
			title: d.title ? decodeEntities(d.title).slice(0, 200) : null,
			location: locationOf(d),
			publishedAt: d.datePosted ?? null,
			org: org ? decodeEntities(org).slice(0, 120) : null,
		};
	},
};

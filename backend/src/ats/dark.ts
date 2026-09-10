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
const PAGE = 500;              // job URLs per streamed page (the Board applies each page to SQLite as it arrives)
const MAX_SITEMAPS = 400;      // sitemaps followed per board (an aggregator index can list hundreds)
const MAX_URLS = 250_000;      // hard safety on one board; the Board keeps one id per row in memory for the diff
const DISCOVERY_BUDGET_MS = 15 * 60_000; // best effort: stop here, keep what was found, report partial
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

/** Sitemap body as text; .gz sitemaps (common on big boards) are decompressed on the fly. */
async function sitemapText(res: Response, url: string): Promise<string> {
	const gz = /\.gz(\?|$)/i.test(url) || /gzip/i.test(res.headers.get("content-type") ?? "") || /gzip/i.test(res.headers.get("content-encoding") ?? "");
	if (gz && res.body && !(res.headers.get("content-encoding") ?? "").includes("gzip")) {
		try { return await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text(); } catch { return ""; }
	}
	return res.text();
}
const isSitemapUrl = (u: string) => /\.xml($|\?|\.gz)/i.test(u);

/**
 * Stream the host's current job-page URLs to `sink` in pages as they are found, never holding the whole
 * list: sitemap index -> sub-sitemaps (one more level of index allowed) -> job URLs. No cap on jobs; the
 * only bounds are MAX_SITEMAPS and MAX_URLS, sized for aggregators, not employers. Returns the count.
 */
async function discoverStream(slug: string, sink: (urls: string[]) => Promise<void>): Promise<{ total: number; partial: boolean }> {
	const deadline = Date.now() + DISCOVERY_BUDGET_MS; let partial = false;
	const roots = new Set<string>();
	for (const p of ["/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/job-sitemap.xml", "/sitemaps/jobs.xml"]) {
		try {
			const res = await fetchRetry(`https://${slug}${p}`, { headers: { "user-agent": UA } });
			if (!res.ok) continue;
			const body = await sitemapText(res, p);
			if (p === "/robots.txt") for (const m of body.matchAll(/(?:^|\n)\s*sitemap:\s*(\S+)/gi)) roots.add(m[1].trim());
			else for (const u of locs(body)) roots.add(u);
		} catch { /* ignore */ }
	}
	let total = 0; let page: string[] = [];
	const emit = async (u: string) => { page.push(u); total++; if (page.length >= PAGE) { const p = page; page = []; await sink(p); } };
	for (const u of roots) if (isJobUrl(u)) await emit(u);
	const subs = [...roots].filter((u) => JOB_SITEMAP.test(u) && isSitemapUrl(u));
	// rotate the walk order by day so a board too big for one budget is covered from a different start each night
	const day = Math.floor(Date.now() / 86_400_000);
	const queue = (subs.length ? subs : [...roots].filter(isSitemapUrl)).sort();
	if (queue.length > 1) { const k = day % queue.length; queue.push(...queue.splice(0, k)); }
	const scanned = new Set<string>();
	while (queue.length && scanned.size < MAX_SITEMAPS && total < MAX_URLS) {
		if (Date.now() > deadline) { partial = true; break; }
		const sm = queue.shift()!;
		if (scanned.has(sm)) continue;
		scanned.add(sm);
		try {
			const res = await fetchRetry(sm, { headers: { "user-agent": UA } });
			if (!res.ok) continue;
			const body = await sitemapText(res, sm);
			const isIndex = /<sitemapindex/i.test(body);
			for (const u of locs(body)) {
				if (isIndex && isSitemapUrl(u)) { if (subs.length === 0 || JOB_SITEMAP.test(u) || !/\b(page|post|blog|news|categor|tag)/i.test(u)) queue.push(u); }
				else if (isJobUrl(u)) { await emit(u); if (total >= MAX_URLS) break; }
			}
		} catch { /* ignore */ }
	}
	// Discovery fallback: many sites list jobs only in the careers-page HTML, not in a sitemap. When the
	// sitemap pass came up short, harvest job-shaped links straight from the careers landing pages.
	if (total < 3) {
		for (const p of ["/careers", "/jobs", "/", "/en/careers", "/karriere", "/careers/jobs", "/join-us"]) {
			try {
				const res = await fetchRetry(`https://${slug}${p}`, { headers: { "user-agent": UA } });
				if (!res.ok) continue;
				const html = await res.text();
				for (const m of html.matchAll(/<a[^>]+href=["']([^"'#]+)["']/gi)) {
					if (!isJobUrl(m[1])) continue;
					try { await emit(new URL(m[1], `https://${slug}${p}`).href.replace(/#.*$/, "")); } catch { /* skip bad href */ }
				}
				if (total >= 3) break; // a landing page that yielded links is enough
			} catch { /* ignore */ }
		}
	}
	if (page.length) await sink(page);
	return { total, partial: partial || scanned.size >= MAX_SITEMAPS || total >= MAX_URLS };
}

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

const toJob = (u: string): Job => ({
	id: u.replace(/^https?:\/\//, "").replace(/[#?].*$/, ""), // stable per job URL
	title: titleFromUrl(u),
	location: null,
	url: u,
	departments: [],
	publishedAt: null,
	updatedAt: null,
	content: null, // the JobPosting JSON-LD arrives via fetchDetail
	raw: null,
});

/** Error carrying the HTTP status so the Board's detail loop can tell "back off" (429/503) from "gone" (404). */
export class HttpError extends Error { constructor(public status: number, msg: string) { super(msg); } }

export const dark: AtsFetcher = {
	fetchTimeoutMs: 20 * 60_000, // up to MAX_SITEMAPS sitemap fetches, streamed; must not be cut short (see types.ts)
	/** Non-streaming variant (ingest path, tests): collects what the stream yields. */
	async fetchJobs(slug: string): Promise<FetchResult> {
		const jobs: Job[] = [];
		const { total: n } = await discoverStream(slug, async (urls) => { for (const u of urls) jobs.push(toJob(u)); });
		if (!n) return { status: "gone" }; // no sitemap job URLs: not a static-crawlable board
		return { status: "ok", jobs };
	},

	/** Streaming: each page of URLs goes to the Board as soon as a sitemap yields it. */
	async fetchJobsStream(slug: string, sink: (page: Job[]) => Promise<void>): Promise<{ status: "ok"; partial?: boolean } | { status: "gone" }> {
		const { total: n, partial } = await discoverStream(slug, async (urls) => sink(urls.map(toJob)));
		return n ? { status: "ok", partial } : { status: "gone" };
	},

	async fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
		// One attempt, no internal retry: the Board's detail loop owns pacing and backs the whole board off on
		// 429/503/Retry-After, which is cheaper than every worker sleeping through its own retry ladder.
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } }, 1);
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) { await res.body?.cancel(); throw new HttpError(res.status, `dark job page HTTP ${res.status}` + (res.headers.get("retry-after") ? ` retry-after=${res.headers.get("retry-after")}` : "")); }
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

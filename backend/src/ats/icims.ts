import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

const UA = "open-jobs/0.1";
const CONCURRENCY = 6;

function decode(s: string): string {
	return s
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
		.replace(/&amp;/g, "&")
		.replace(/\s+/g, " ")
		.trim();
}

async function mapLimit<T, R>(items: T[], limit: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let i = 0;
	await Promise.all(
		Array.from({ length: Math.min(limit, items.length) }, async () => {
			while (i < items.length) {
				const idx = i++;
				out[idx] = await fn(items[idx]);
			}
		}),
	);
	return out;
}

interface Page {
	status: "ok" | "gone" | "login" | "external";
	html: string;
	target?: string;
}

async function getPage(slug: string, pr: number): Promise<Page> {
	const url = `https://${slug}.icims.com/jobs/search?ss=1&in_iframe=1&pr=${pr}`;
	const res = await fetch(url, { headers: { "user-agent": UA, accept: "text/html" }, redirect: "manual" });
	if (res.status === 404) return { status: "gone", html: "" };
	if (res.status >= 300 && res.status < 400) {
		const loc = res.headers.get("location") ?? "";
		if (/\/jobs\/login/.test(loc)) return { status: "login", html: "", target: loc };
		// Board renamed (301 to another icims host) — follow once.
		const m = loc.match(/^https:\/\/([a-z0-9-]+)\.icims\.com\/jobs\/search/i);
		if (m && m[1] !== slug) return getPage(m[1], pr);
		return { status: "external", html: "", target: loc };
	}
	if (!res.ok) throw new Error(`icims ${slug}: HTTP ${res.status} on page ${pr}`);
	const html = await res.text();
	const jsRedirect = html.match(/window\.top\.location\.href\s*=\s*'([^']+)'/);
	if (jsRedirect && !/iCIMS_JobCardItem/.test(html)) {
		return { status: "external", html, target: jsRedirect[1].replace(/\\\//g, "/") };
	}
	return { status: "ok", html };
}

function parseCards(slug: string, html: string): Job[] {
	const jobs: Job[] = [];
	const cardRe = /<li class="iCIMS_JobCardItem">([\s\S]*?)<\/li>/g;
	let m: RegExpExecArray | null;
	while ((m = cardRe.exec(html))) {
		const card = m[1];
		const a = card.match(/<a href="([^"]+)"[^>]*class="iCIMS_Anchor"[^>]*>([\s\S]*?)<\/a>/);
		if (!a) continue;
		const url = a[1]
			.replace(/&amp;/g, "&")
			.replace(/([?&])in_iframe=1(&|$)/, "$1")
			.replace(/[?&]$/, "");
		const h = a[2].match(/<h3[^>]*>([\s\S]*?)<\/h3>/);
		const title = decode(h ? h[1] : a[2].replace(/<span class="sr-only[^"]*">[^<]*<\/span>/, ""));
		const idM = url.match(/\/jobs\/(\d+)\//);
		const headerLeft = card.match(/<div class="col-xs-6 header left">([\s\S]*?)<\/div>/);
		const location = headerLeft
			? decode(headerLeft[1].replace(/<span class="sr-only field-label">[^<]*<\/span>/, "")) || null
			: null;
		const descM = card.match(/<div class="col-xs-12 description">([\s\S]*?)<\/div>/);
		const description = descM ? decode(descM[1]) : "";
		const fields: Record<string, string> = {};
		const fieldRe = /<dt class="iCIMS_JobHeaderField">([\s\S]*?)<\/dt>\s*<dd class="iCIMS_JobHeaderData">([\s\S]*?)<\/dd>/g;
		let f: RegExpExecArray | null;
		while ((f = fieldRe.exec(card))) {
			const k = decode(f[1]);
			if (k) fields[k] = decode(f[2]);
		}
		const departments = Object.entries(fields)
			.filter(([k]) => /categor|department|job family|function|business unit|division/i.test(k))
			.map(([, v]) => v)
			.filter(Boolean);
		const posted = Object.entries(fields).find(([k]) => /posted|post date/i.test(k))?.[1];
		let publishedAt: string | null = null;
		if (posted) {
			const d = new Date(posted);
			if (!Number.isNaN(d.getTime())) publishedAt = d.toISOString();
		}
		jobs.push({
			id: idM ? idM[1] : url,
			title,
			location,
			url,
			departments,
			publishedAt,
			updatedAt: null,
			content: description || null,
			raw: { slug, url, title, location, description, fields },
		});
	}
	return jobs;
}

/**
 * Detail: GET the posting page with `in_iframe=1` and stitch the `iCIMS_InfoField_Job` headings with their
 * `iCIMS_InfoMsg_Job` bodies (Overview / Responsibilities / Qualifications ...). Closed postings answer 410.
 */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const u = new URL(job.url);
	u.searchParams.set("in_iframe", "1");
	const res = await fetchRetry(u.toString(), { headers: { "user-agent": UA, accept: "text/html" }, redirect: "manual" });
	if (res.status === 404 || res.status === 410) return null;
	if (res.status >= 300 && res.status < 400) {
		const loc = res.headers.get("location") ?? "";
		if (/\/jobs\/login/.test(loc)) return null;
		throw new Error(`icims detail ${slug}/${job.id}: redirected to ${loc}`);
	}
	if (!res.ok) throw new Error(`icims detail ${slug}/${job.id}: HTTP ${res.status}`);
	const html = await res.text();
	const start = html.indexOf('class="iCIMS_JobContent"');
	const end = html.indexOf('class="iCIMS_JobOptions"', start);
	if (start < 0) return null;
	const body = html.slice(start, end > start ? end : undefined);
	const parts: string[] = [];
	const secRe =
		/<h2 class="iCIMS_InfoMsg iCIMS_InfoField_Job">([\s\S]*?)<\/h2>\s*<div class="iCIMS_InfoMsg iCIMS_InfoMsg_Job">\s*<div class="iCIMS_Expandable_Container">\s*<div class="iCIMS_Expandable_Text">([\s\S]*?)<\/div>\s*<\/div>\s*<\/div>/g;
	let m: RegExpExecArray | null;
	while ((m = secRe.exec(body))) {
		const title = decode(m[1]);
		const text = m[2].trim();
		if (!text) continue;
		parts.push(title ? `<h3>${title}</h3>\n${text}` : text);
	}
	if (parts.length === 0) {
		// Layout without headings: take every expandable text block.
		const blockRe = /<div class="iCIMS_Expandable_Text">([\s\S]*?)<\/div>\s*<\/div>\s*<\/div>/g;
		while ((m = blockRe.exec(body))) if (m[1].trim()) parts.push(m[1].trim());
	}
	const headerLeft = body.match(/<div class="col-xs-6 header left">([\s\S]*?)<\/div>/);
	const location = headerLeft
		? decode(headerLeft[1].replace(/<span class="sr-only field-label">[^<]*<\/span>/, "")) || null
		: null;
	return { content: parts.length ? parts.join("\n") : null, location: location ?? undefined };
}

export const icims: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		let first: Page;
		try {
			first = await getPage(slug, 0);
		} catch (e) {
			// Non-existent subdomain: DNS failure surfaces as a TypeError from fetch.
			if (e instanceof TypeError) return { status: "gone" };
			throw e;
		}
		if (first.status === "gone") return { status: "gone" };
		if (first.status === "login") throw new Error(`icims ${slug}: board requires login (internal portal)`);
		if (first.status === "external") throw new Error(`icims ${slug}: board redirects to external site ${first.target}`);
		const pm = first.html.match(/Page\s+\d+\s+of\s+(\d+)/);
		const totalPages = pm ? Number(pm[1]) : 1;
		const jobs = parseCards(slug, first.html);
		if (totalPages > 1) {
			const rest = await mapLimit(
				Array.from({ length: totalPages - 1 }, (_, i) => i + 1),
				CONCURRENCY,
				async (pr) => {
					const p = await getPage(slug, pr);
					if (p.status !== "ok") throw new Error(`icims ${slug}: page ${pr} returned ${p.status}`);
					return parseCards(slug, p.html);
				},
			);
			for (const r of rest) jobs.push(...r);
		}
		const seen = new Set<string>();
		return { status: "ok", jobs: jobs.filter((j) => (seen.has(j.id) ? false : (seen.add(j.id), true))) };
	},
};

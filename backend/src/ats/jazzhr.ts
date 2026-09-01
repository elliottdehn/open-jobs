import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * JazzHR (formerly The Resumator) hosted boards at {slug}.applytojob.com. Slug = the company
 * subdomain, mined from the Common Crawl index (*.applytojob.com). Two stages:
 *   fetchJobs   — GET /apply/jobs/ : a server-rendered #jobs_table; each row is
 *                 <a class="job_title_link" href="/apply/jobs/details/{hash}">Title</a> + a location cell.
 *   fetchDetail — GET /apply/jobs/details/{hash} : the page carries a schema.org JobPosting JSON-LD
 *                 (full HTML description, structured location, datePosted).
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";

interface JazzLoc { address?: { addressLocality?: string; addressRegion?: string; addressCountry?: string } }
interface JazzPosting { title?: string; description?: string; datePosted?: string; jobLocation?: JazzLoc | JazzLoc[]; hiringOrganization?: { name?: string } }

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
function text(html: string): string { return decodeEntities(html.replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim(); }

export const jazzhr: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const base = `https://${slug}.applytojob.com`;
		const res = await fetchRetry(`${base}/apply/jobs/`, { headers: { "user-agent": UA }, redirect: "manual" });
		if (res.status >= 300 && res.status < 400) return { status: "gone" };
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`jazzhr list HTTP ${res.status}`);
		const html = await res.text();
		const table = html.match(/id="jobs_table"[\s\S]*?<\/table>/)?.[0] ?? html;
		const jobs: Job[] = []; const seen = new Set<string>();
		for (const row of table.matchAll(/<tr\b[^>]*id="row_job_[^"]*"[^>]*>([\s\S]*?)<\/tr>/g)) {
			const cells = [...row[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map((m) => m[1]);
			const a = row[1].match(/class="job_title_link"\s+href="(\/apply\/jobs\/details\/([^"?]+)[^"]*)"[^>]*>([\s\S]*?)<\/a>/);
			if (!a) continue;
			const id = a[2]; const title = text(a[3]);
			if (!title || seen.has(id)) continue;
			seen.add(id);
			const dept = row[1].match(/class="resumator_department"[^>]*>([\s\S]*?)<\/span>/)?.[1];
			const loc = cells[1] ? text(cells[1]) : null;
			jobs.push({
				id,
				title,
				location: loc || null,
				url: `${base}/apply/jobs/details/${id}`,
				departments: dept && text(dept) ? [text(dept)] : [],
				publishedAt: null,
				updatedAt: null,
				content: null, // full description arrives via fetchDetail
				raw: null,
			});
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA }, redirect: "manual" });
		if (res.status === 404 || res.status === 410 || (res.status >= 300 && res.status < 400)) return null;
		if (!res.ok) throw new Error(`jazzhr detail HTTP ${res.status}`);
		const html = await res.text();
		let jp: JazzPosting | null = null;
		for (const m of html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
			let o: unknown;
			try { o = JSON.parse(m[1].trim()); } catch { continue; }
			if (o && typeof o === "object" && (o as { "@type"?: string })["@type"] === "JobPosting") { jp = o as JazzPosting; break; }
		}
		if (!jp || !jp.description) return null;
		const loc = (() => {
			const arr = Array.isArray(jp.jobLocation) ? jp.jobLocation : jp.jobLocation ? [jp.jobLocation] : [];
			const parts = (arr as JazzLoc[])
				.map((l) => [l?.address?.addressLocality, l?.address?.addressRegion, l?.address?.addressCountry].filter(Boolean).join(", "))
				.filter(Boolean);
			return parts.length ? parts.slice(0, 4).join("; ") : null;
		})();
		return {
			content: decodeEntities(jp.description),
			title: jp.title ? decodeEntities(jp.title).slice(0, 200) : null,
			location: loc,
			publishedAt: jp.datePosted ?? null,
			org: jp.hiringOrganization?.name ? decodeEntities(jp.hiringOrganization.name).slice(0, 120) : null,
		};
	},
};

import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Teamtailor career sites (Nordic/European SMEs; tens of thousands of tenants). Slug = the site
 * hostname: either <tenant>.teamtailor.com or a custom domain (career.acme.se). Listing: the public
 * /jobs HTML pages (paginated ?page=N until a page has no job links); themes vary, so the listing is
 * only trusted for job id/URL/title, and everything else (description, location, dates, org) comes
 * from the job page's JSON-LD JobPosting via fetchDetail.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";
const MAX_PAGES = 200;

const LINK_RE = /href="([^"]*\/jobs\/(\d+)-[a-z0-9-]*)"[^>]*>([\s\S]{0,400}?)<\/a>/g;

function anchorTitle(inner: string): string {
	// strip tags, collapse whitespace; anchors wrap the title (sometimes with a decorative span first)
	return inner.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

export const teamtailor: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const jobs: Job[] = []; const seen = new Set<string>();
		for (let page = 1; page <= MAX_PAGES; page++) {
			const res = await fetchRetry(`https://${slug}/jobs?page=${page}`, { headers: { "user-agent": UA } });
			if (res.status === 404 || res.status === 410) return page === 1 ? { status: "gone" } : { status: "ok", jobs };
			if (!res.ok) throw new Error(`teamtailor /jobs HTTP ${res.status}`);
			const html = await res.text();
			let added = 0;
			for (const m of html.matchAll(LINK_RE)) {
				const [, href, id, inner] = m;
				if (seen.has(id)) continue;
				const title = anchorTitle(inner);
				if (!title || title.length > 200) continue;
				seen.add(id); added++;
				jobs.push({
					id,
					title,
					location: null, // filled by the JSON-LD detail
					url: href.startsWith("http") ? href : `https://${slug}${href.startsWith("/") ? "" : "/"}${href}`,
					departments: [],
					publishedAt: null,
					updatedAt: null,
					content: null,
					raw: null,
				});
			}
			if (added === 0) break; // past the last page (or a themed page without parseable links)
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`teamtailor job page HTTP ${res.status}`);
		const html = await res.text();
		for (const m of html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)) {
			let d: {
				"@type"?: string; description?: string; datePosted?: string; title?: string; employmentType?: string;
				jobLocation?: { address?: { addressLocality?: string; addressRegion?: string; addressCountry?: string } }[] | { address?: { addressLocality?: string; addressCountry?: string } };
				hiringOrganization?: { name?: string };
			};
			try { d = JSON.parse(m[1]); } catch { continue; }
			if (d["@type"] !== "JobPosting") continue;
			const locs = Array.isArray(d.jobLocation) ? d.jobLocation : d.jobLocation ? [d.jobLocation] : [];
			const location = locs.map((l) => [l?.address?.addressLocality, (l?.address as { addressCountry?: string } | undefined)?.addressCountry]
				.filter(Boolean).join(", ")).filter(Boolean).join("; ") || null;
			return { content: d.description ?? null, publishedAt: d.datePosted ?? null, location };
		}
		return null; // page without JSON-LD (unusual theme); keep the listing row as-is
	},
};

import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * softgarden career portals (DACH-heavy). Slug = the tenant hostname (<tenant>.softgarden.io or a
 * custom domain). Listing: the /vacancies page (falling back to the root) renders every posting with
 * `id="job_id_<id>"` blocks — title in the anchor, company/category in sibling matchValue divs.
 * Detail: the /job/<id> page carries a JSON-LD JobPosting with the full description.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";

const BLOCK_RE = /id="job_id_(\d+)"[\s\S]{0,900}?<a href="([^"]+)"[^>]*>([\s\S]{0,300}?)<\/a>/g;

function text(s: string): string {
	return s.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
}

async function listing(slug: string): Promise<{ html: string; gone: boolean }> {
	for (const path of ["/vacancies", "/"]) {
		const res = await fetchRetry(`https://${slug}${path}`, { headers: { "user-agent": UA } });
		if (res.status === 404) continue;
		if (!res.ok) throw new Error(`softgarden ${path} HTTP ${res.status}`);
		const html = await res.text();
		if (html.includes("job_id_") || path === "/") return { html, gone: false };
	}
	return { html: "", gone: true };
}

export const softgarden: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const { html, gone } = await listing(slug);
		if (gone) return { status: "gone" };
		const jobs: Job[] = []; const seen = new Set<string>();
		for (const m of html.matchAll(BLOCK_RE)) {
			const [, id, href, inner] = m;
			if (seen.has(id)) continue;
			const title = text(inner);
			if (!title) continue;
			seen.add(id);
			jobs.push({
				id,
				title,
				location: null, // JSON-LD detail fills it
				url: `https://${slug}/job/${id}`,
				departments: [],
				publishedAt: null,
				updatedAt: null,
				content: null,
				raw: null,
			});
		}
		if (!jobs.length && !html.includes("job_id_")) return { status: "gone" }; // not a softgarden portal
		return { status: "ok", jobs };
	},

	async fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`softgarden job page HTTP ${res.status}`);
		const html = await res.text();
		for (const m of html.matchAll(/<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)) {
			let d: { "@type"?: string; description?: string; datePosted?: string;
				jobLocation?: { address?: { addressLocality?: string; addressCountry?: string } } | { address?: { addressLocality?: string; addressCountry?: string } }[] };
			try { d = JSON.parse(m[1]); } catch { continue; }
			if (d["@type"] !== "JobPosting") continue;
			const locs = Array.isArray(d.jobLocation) ? d.jobLocation : d.jobLocation ? [d.jobLocation] : [];
			const location = locs.map((l) => [l?.address?.addressLocality, l?.address?.addressCountry].filter(Boolean).join(", ")).filter(Boolean).join("; ") || null;
			return { content: d.description ?? null, publishedAt: d.datePosted ?? null, location };
		}
		return null;
	},
};

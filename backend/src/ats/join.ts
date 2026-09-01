import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * JOIN.com company pages (European SMEs). Slug = the company slug in join.com/companies/<slug>.
 * Listing: the company page's __NEXT_DATA__ carries initialState.jobs.items (title, idParam, city,
 * country, employment type, createdAt). Detail: the job page (companies/<slug>/<idParam>) has a
 * JSON-LD JobPosting with the full description.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";

interface JoinItem {
	id?: number; idParam?: string; title?: string; createdAt?: string; workplaceType?: string;
	city?: { cityName?: string; countryName?: string }; country?: { iso3166?: string };
	employmentType?: { name?: string }; category?: { name?: string };
}

function nextData(html: string): unknown | null {
	const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
	if (!m) return null;
	try { return JSON.parse(m[1]); } catch { return null; }
}

export const join: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const res = await fetchRetry(`https://join.com/companies/${slug}`, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`join.com HTTP ${res.status}`);
		const d = nextData(await res.text()) as { props?: { pageProps?: { initialState?: { jobs?: { items?: JoinItem[] } } } } } | null;
		const items = d?.props?.pageProps?.initialState?.jobs?.items;
		if (!Array.isArray(items)) return { status: "gone" }; // not a JOIN company page
		const jobs: Job[] = []; const seen = new Set<string>();
		for (const it of items) {
			const id = String(it.id ?? it.idParam ?? "");
			if (!id || !it.title || seen.has(id)) continue;
			seen.add(id);
			const loc = [it.city?.cityName, it.city?.countryName ?? it.country?.iso3166].filter(Boolean).join(", ");
			jobs.push({
				id,
				title: it.title,
				location: (it.workplaceType === "REMOTE" ? "Remote" + (loc ? " - " + loc : "") : loc) || null,
				url: `https://join.com/companies/${slug}/${it.idParam ?? id}`,
				departments: it.category?.name ? [it.category.name] : [],
				publishedAt: it.createdAt ?? null,
				updatedAt: null,
				content: null,
				raw: it,
			});
		}
		return { status: "ok", jobs };
	},

	async fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
		const res = await fetchRetry(job.url, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return null;
		if (!res.ok) throw new Error(`join job page HTTP ${res.status}`);
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

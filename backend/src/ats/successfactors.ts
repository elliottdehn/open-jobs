import type { AtsFetcher, FetchResult, Job } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * SAP SuccessFactors "Career Site Builder" (RMK) sites — custom-domain career sites like
 * jobs.exxonmobil.com or careers.phillips66.com. Slug = the site hostname. These sites publish a
 * Google-Jobs RSS feed at /sitemap.xml with every posting including the full description, so the
 * whole board is one request and there is no detail stage.
 *
 * (The legacy slugs this ATS once carried — rmkcdn, performancemanager5na — were SF CDN hostnames
 * with nothing to fetch; real RMK hostnames are mined via the shared rmkcdn CDN references.)
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";

function tag(item: string, name: string): string | null {
	const m = item.match(new RegExp(`<${name}[^>]*>([\\s\\S]*?)</${name}>`));
	if (!m) return null;
	return m[1].replace(/^<!\[CDATA\[/, "").replace(/\]\]>$/, "").trim() || null;
}

function unescapeXml(s: string): string {
	return s.replace(/&(amp|lt|gt|quot|apos|#39);/g, (m, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", "#39": "'" })[e as string] ?? m);
}

export const successfactors: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const res = await fetchRetry(`https://${slug}/sitemap.xml`, { headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`successfactors rss HTTP ${res.status}`);
		const xml = await res.text();
		if (!xml.includes("<rss")) return { status: "gone" }; // a plain sitemap or an error page: not an RMK jobs feed
		const jobs: Job[] = []; const seen = new Set<string>();
		for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)) {
			const item = m[1];
			const link = tag(item, "link") ?? "";
			const gid = tag(item, "g:id");
			const idm = gid ?? (link.match(/\/(\d+)\/?$/)?.[1] ?? null);
			const title = tag(item, "title");
			if (!idm || !title || !link || seen.has(idm)) continue;
			seen.add(idm);
			// titles arrive as "Role (City, ST, CC, zip)"; keep the role, mine the location separately
			const location = tag(item, "g:location") ?? (title.match(/\(([^()]+)\)\s*$/)?.[1] ?? null);
			const desc = tag(item, "description");
			jobs.push({
				id: idm,
				title: unescapeXml(title.replace(/\s*\([^()]*\)\s*$/, "")),
				location: location ? unescapeXml(location) : null,
				url: unescapeXml(link),
				departments: [],
				publishedAt: tag(item, "pubDate"),
				updatedAt: null,
				content: desc ? unescapeXml(desc) : null,
				raw: null,
			});
		}
		if (!jobs.length) return { status: "ok", jobs: [] };
		return { status: "ok", jobs };
	},
};

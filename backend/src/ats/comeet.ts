import type { AtsFetcher, FetchResult, Job } from "./types";
import uids from "../comeet-uids.json";

/**
 * Comeet career pages live at https://www.comeet.com/jobs/<company>/<uid> where <uid> looks like "B4.007".
 * Our slugs only carry <company>, and comeet offers no lookup by name, so uids are resolved offline via the
 * Common Crawl URL index and baked into src/comeet-uids.json (scripts/build-comeet.mjs). Live discovery
 * (CC index, then DuckDuckGo) is only a fallback for slugs missing from that map. The careers page then
 * embeds the company_uid + API token needed for the public careers-api positions endpoint.
 */

interface ComeetPosition {
	uid: string;
	name: string;
	department?: string | null;
	location?: { name?: string | null } | null;
	url_comeet_hosted_page?: string;
	url_active_page?: string | null;
	time_updated?: string | null;
	details?: { name: string; value: string }[];
	categories?: { name: string; value: string }[];
}

const UA = "open-jobs/0.1";
const UID_RE = /comeet\.com\/jobs\/([^/"'&?\s]+)\/([0-9A-Za-z]{2}\.[0-9A-Za-z]{3})/g;
const CC_INDEX = "https://index.commoncrawl.org/CC-MAIN-2026-21-index";

function findUid(text: string, slug: string): { name: string; uid: string } | null {
	const want = slug.toLowerCase();
	for (const m of text.matchAll(UID_RE)) {
		if (m[1].toLowerCase() === want) return { name: m[1], uid: m[2].toUpperCase() };
	}
	return null;
}

export async function discoverUid(slug: string): Promise<{ name: string; uid: string } | null> {
	// 1) Common Crawl URL index (same crawl the slug list was built from).
	try {
		const url = `${CC_INDEX}?url=${encodeURIComponent(`comeet.com/jobs/${slug}/*`)}&output=json&limit=5&fl=url`;
		const res = await fetch(url, { headers: { "user-agent": UA } });
		if (res.ok) {
			const found = findUid(await res.text(), slug);
			if (found) return found;
		}
	} catch {
		/* fall through */
	}
	// 2) DuckDuckGo HTML search.
	try {
		const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(`site:comeet.com/jobs/${slug}`)}`;
		const res = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 " + UA } });
		if (res.ok) {
			const found = findUid(decodeURIComponent(await res.text()), slug);
			if (found) return found;
		}
	} catch {
		/* fall through */
	}
	return null;
}

const KNOWN = uids as Record<string, { name: string; uid: string } | null>;

export const comeet: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		// `null` in the map means the offline resolver already found nothing; don't hammer CC daily.
		const found = slug in KNOWN ? KNOWN[slug] : await discoverUid(slug);
		if (!found) return { status: "gone" };

		const pageUrl = `https://www.comeet.com/jobs/${encodeURIComponent(found.name)}/${found.uid}`;
		const page = await fetch(pageUrl, { headers: { accept: "text/html", "user-agent": UA }, redirect: "manual" });
		// Unknown/removed companies 404 or redirect to the comeet homepage.
		if (page.status === 404 || (page.status >= 300 && page.status < 400)) return { status: "gone" };
		if (!page.ok) throw new Error(`comeet ${slug}: HTTP ${page.status} on careers page`);
		const html = await page.text();
		const uid = /"company_uid":\s*"([^"]+)"/.exec(html)?.[1];
		const token = /"token":\s*"([0-9A-Fa-f]+)"/.exec(html)?.[1];
		if (!uid || !token) throw new Error(`comeet ${slug}: could not extract company_uid/token from careers page`);

		const apiUrl = `https://www.comeet.co/careers-api/2.0/company/${uid}/positions?token=${token}&details=true`;
		const res = await fetch(apiUrl, { headers: { accept: "application/json", "user-agent": UA } });
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`comeet ${slug}: HTTP ${res.status} on positions API`);
		const list = (await res.json()) as ComeetPosition[];
		if (!Array.isArray(list)) throw new Error(`comeet ${slug}: unexpected positions response shape`);

		const jobs: Job[] = list.map((p) => {
			const details = p.details ?? [];
			const content = details
				.filter((d) => d.value)
				.map((d) => (d.name ? `<h3>${d.name}</h3>\n${d.value}` : d.value))
				.join("\n");
			return {
				id: p.uid,
				title: p.name,
				location: p.location?.name ?? null,
				url: p.url_comeet_hosted_page ?? `${pageUrl}/${p.uid}`,
				departments: p.department ? [p.department] : [],
				publishedAt: null,
				updatedAt: p.time_updated ?? null,
				content: content || null,
				raw: p,
			};
		});
		return { status: "ok", jobs };
	},
};

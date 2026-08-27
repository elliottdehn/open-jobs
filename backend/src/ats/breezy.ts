import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

interface BreezyJob {
	id: string;
	friendly_id?: string;
	name: string;
	url: string;
	published_date?: string | null;
	type?: { id?: string; name?: string } | null;
	location?: { name?: string; is_remote?: boolean } | null;
	locations?: { name?: string }[];
	department?: string | null;
	salary?: string;
}

const HEADERS = { accept: "application/json", "user-agent": "open-jobs/0.1" };

/**
 * Detail: the position page (job.url, /p/{id}-{slug}) embeds a schema.org JobPosting ld+json with
 * the HTML description. Closed/unknown positions answer 404.
 */
async function fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
	const res = await fetchRetry(job.url, { headers: { accept: "text/html", "user-agent": "open-jobs/0.1" } });
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`breezy detail ${job.url}: HTTP ${res.status}`);
	const html = await res.text();
	const re = /<script type="application\/ld\+json">(\{"@context":"https?:\/\/schema\.org\/?","@type":"JobPosting".*?)<\/script>/s;
	const m = re.exec(html);
	if (!m) return null;
	let data: { description?: string; datePosted?: string };
	try {
		data = JSON.parse(m[1]);
	} catch {
		return null;
	}
	return { content: data.description ?? null, raw: data, publishedAt: data.datePosted ?? undefined };
}

export const breezy: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		// Slug is the company subdomain on breezy.hr.
		const url = `https://${slug}.breezy.hr/json`;
		const res = await fetch(url, { headers: HEADERS });
		// Unknown portals answer 404 with a "Career portal not found" HTML page.
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`breezy ${slug}: HTTP ${res.status}`);
		const text = await res.text();
		let list: BreezyJob[];
		try {
			list = JSON.parse(text) as BreezyJob[];
		} catch {
			throw new Error(`breezy ${slug}: non-JSON response`);
		}
		if (!Array.isArray(list)) throw new Error(`breezy ${slug}: unexpected response shape`);

		const jobs: Job[] = list.map((j) => {
			const locNames = (j.locations ?? []).map((l) => l.name).filter((n): n is string => !!n);
			const location = locNames.length > 1 ? locNames.join("; ") : (j.location?.name ?? locNames[0] ?? null);
			return {
				id: String(j.id),
				title: j.name,
				location,
				url: j.url,
				departments: j.department ? [j.department] : [],
				publishedAt: j.published_date ?? null,
				updatedAt: null,
				content: null,
				raw: j,
			};
		});
		return { status: "ok", jobs };
	},
};

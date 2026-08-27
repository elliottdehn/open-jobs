import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Paylocity Recruiting public job boards.
 * Slug: the board GUID from recruiting.paylocity.com/recruiting/jobs/All/{guid}/{company-slug}.
 * The list page is server-rendered with the full job list embedded as `window.pageData`
 * (no pagination). Descriptions in the list are truncated snippets, so `fetchDetail` reads the
 * JSON-LD JobPosting.description from /Recruiting/Jobs/Details/{JobId}.
 */

interface PlJobLocation {
	City?: string | null;
	State?: string | null;
	Country?: string | null;
	Name?: string | null;
}

interface PlJob {
	JobId: number;
	JobTitle: string;
	LocationName?: string | null;
	ShouldDisplayLocation?: boolean;
	PublishedDate?: string | null;
	Description?: string | null;
	IsInternal?: boolean;
	HiringDepartment?: string | null;
	JobLocation?: PlJobLocation | null;
	IsRemote?: boolean;
}

interface PlPageData {
	Jobs: PlJob[];
	ModuleId?: string;
	ModuleTitle?: string;
}

const BASE = "https://recruiting.paylocity.com";
const UA = "open-jobs/0.1";

function decodeEntities(s: string): string {
	return s
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&");
}

function slugify(s: string): string {
	return s
		.replace(/[^A-Za-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "")
		.slice(0, 80) || "x";
}

function extractPageData(html: string): PlPageData | null {
	const m = html.match(/window\.pageData\s*=\s*(\{[\s\S]*?\});\s*\n/);
	if (!m) return null;
	try {
		return JSON.parse(m[1]) as PlPageData;
	} catch {
		return null;
	}
}

/** Detail: GET /Recruiting/Jobs/Details/{JobId} -> JSON-LD JobPosting.description (HTML). */
async function fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
	const jobId = (job.raw as PlJob | undefined)?.JobId ?? job.id;
	const res = await fetchRetry(`${BASE}/Recruiting/Jobs/Details/${jobId}`, {
		headers: { "user-agent": UA, accept: "text/html" },
		redirect: "manual",
	});
	// Closed/unknown jobs redirect (302) to /Recruiting/Jobs/JobNotFound or the board list.
	if (res.status === 404 || res.status === 410 || (res.status >= 300 && res.status < 400)) return null;
	if (!res.ok) throw new Error(`paylocity detail ${jobId}: HTTP ${res.status}`);
	const html = await res.text();
	const m = html.match(/<script type="application\/ld\+json">\s*([\s\S]*?)\s*<\/script>/);
	if (m) {
		try {
			// Note: JSON-LD `datePosted` is the page render time, not the posting date; ignore it.
			const ld = JSON.parse(m[1]) as { description?: string };
			if (ld.description) return { content: decodeEntities(ld.description), raw: ld };
		} catch {
			/* fall through to HTML */
		}
	}
	// Some boards render the detail page without JSON-LD: take the Description/Requirements block.
	const start = html.indexOf('<div class="job-listing-header">Description</div>');
	const end = html.indexOf('<div class="preview-bottom-apply-btn">', start);
	if (start < 0 || end < 0) return null;
	const content = html
		.slice(start, end)
		.replace(/<div class="job-listing-header">([^<]*)<\/div>/g, "<h3>$1</h3>")
		.replace(/<div data-bind="[^"]*">/g, "<div>")
		.trim();
	return { content: content || null };
}

function location(j: PlJob): string | null {
	const l = j.JobLocation;
	if (l) {
		const parts = [l.City, l.State, l.Country].filter((p): p is string => !!p && p.trim() !== "");
		if (parts.length) return parts.join(", ");
	}
	if (j.LocationName && j.LocationName.trim()) return j.LocationName.trim();
	return j.IsRemote ? "Remote" : null;
}

export const paylocity: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		if (!/^[0-9a-f-]{36}$/i.test(slug)) throw new Error(`paylocity ${slug}: slug is not a board GUID`);
		const url = `${BASE}/recruiting/jobs/All/${slug}/x`;
		const res = await fetchRetry(url, {
			headers: { "user-agent": UA, accept: "text/html" },
			redirect: "manual",
		});
		if (res.status === 404) return { status: "gone" };
		if (res.status >= 300 && res.status < 400) {
			const loc = res.headers.get("location") ?? "";
			if (/JobNotFound/i.test(loc)) return { status: "gone" };
			throw new Error(`paylocity ${slug}: unexpected redirect to ${loc}`);
		}
		if (!res.ok) throw new Error(`paylocity ${slug}: HTTP ${res.status}`);
		const html = await res.text();
		const data = extractPageData(html);
		if (!data || !Array.isArray(data.Jobs)) throw new Error(`paylocity ${slug}: pageData not found in list page`);

		const company = slugify(data.ModuleTitle ?? "x");
		const visible = data.Jobs.filter((j) => !j.IsInternal);

		const jobs: Job[] = visible.map((j) => ({
			id: String(j.JobId),
			title: j.JobTitle,
			location: location(j),
			url: `${BASE}/recruiting/jobs/Details/${j.JobId}/${company}/${slugify(j.JobTitle)}`,
			departments: j.HiringDepartment ? [j.HiringDepartment] : [],
			publishedAt: j.PublishedDate ?? null,
			updatedAt: null,
			content: null,
			raw: j,
		}));
		return { status: "ok", jobs };
	},
};

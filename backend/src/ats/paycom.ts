import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Paycom applicant-tracking career portals.
 * Slug: the 32-hex `clientkey` from paycomonline.net/v4/ats/web.php/jobs?clientkey={hex}.
 *
 * Flow: GET the career-page HTML (it embeds `configsFromHost` with a per-portal session JWT and
 * the "mantle" service base URL), then POST to {mantle}/api/ats/job-posting-previews/search with
 * the JWT as `Authorization`, paging via skip/take until jobPostingPreviewsCount is reached.
 * Detail: GET {mantle}/api/ats/job-postings/{jobId} with the same JWT (404 ["Active job posting not found"] when closed).
 */

interface PcPreview {
	jobId: number;
	jobTitle: string;
	positionType?: string;
	remoteType?: string;
	locations?: string;
	description?: string;
	postedOn?: string;
	isHotJob?: boolean;
}

interface PcSearchResponse {
	jobPostingPreviews: PcPreview[];
	jobPostingPreviewsCount: number;
}

const PORTAL = "https://www.paycomonline.net/v4/ats/web.php/portal";
const UA = "open-jobs/0.1";
const PAGE = 500;

function emptyFilters() {
	return {
		distanceFrom: 0,
		workEnvironments: [],
		positionTypes: [],
		educationLevels: [],
		categories: [],
		travelTypes: [],
		shiftTypes: [],
		otherFilters: [],
		keywordSearchText: "",
		location: "",
		sortOption: "N",
	};
}

interface PcSession {
	mantle: string;
	headers: Record<string, string>;
}

/** Load the career page and extract the per-portal session JWT + mantle service URL. */
async function openSession(slug: string): Promise<PcSession | "gone"> {
	if (!/^[0-9A-Fa-f]{32}$/.test(slug)) throw new Error(`paycom ${slug}: slug is not a 32-hex clientkey`);
	const pageUrl = `${PORTAL}/${slug}/career-page`;
	const pageRes = await fetchRetry(pageUrl, { headers: { "user-agent": UA, accept: "text/html" } });
	if (pageRes.status === 404) return "gone";
	if (!pageRes.ok) throw new Error(`paycom ${slug}: HTTP ${pageRes.status} on career page`);
	const html = await pageRes.text();

	const start = html.indexOf("configsFromHost = ");
	if (start < 0) {
		if (/Job board does not exist|is unavailable at this time/i.test(html)) return "gone";
		throw new Error(`paycom ${slug}: configsFromHost not found in career page`);
	}
	const end = html.indexOf(";\n", start);
	let cfg: { sessionJWT?: string; libConfig?: string };
	try {
		cfg = JSON.parse(html.slice(start + "configsFromHost = ".length, end));
	} catch {
		throw new Error(`paycom ${slug}: could not parse configsFromHost`);
	}
	const jwt = cfg.sessionJWT;
	let mantle = "https://portal-applicant-tracking.us-cent.paycomonline.net/";
	try {
		const lib = JSON.parse(cfg.libConfig ?? "{}") as { atsPortalMantleServiceUrl?: string };
		if (lib.atsPortalMantleServiceUrl) mantle = lib.atsPortalMantleServiceUrl;
	} catch {
		/* keep default */
	}
	if (!jwt) throw new Error(`paycom ${slug}: no sessionJWT in career page`);
	if (!mantle.endsWith("/")) mantle += "/";
	return {
		mantle,
		headers: {
			"user-agent": UA,
			accept: "application/json",
			"content-type": "application/json",
			authorization: jwt,
			locale: "en-US",
		},
	};
}

interface PcPosting {
	jobId: number;
	jobTitle: string;
	location?: string;
	secondaryLocations?: string[];
	remoteType?: string;
	jobCategory?: string;
	description?: string;
	descriptionTitle?: string;
	qualifications?: string;
	qualificationsTitle?: string;
	salaryRange?: string;
	positionType?: string;
	startDate?: string;
	endDate?: string;
	googleJobJson?: string;
}

/** Session cache so the ~6 concurrent detail calls per board share one career-page load (JWT is per-portal). */
const sessions = new Map<string, { at: number; p: Promise<PcSession | "gone"> }>();
const SESSION_TTL = 10 * 60_000;

function cachedSession(slug: string, force = false): Promise<PcSession | "gone"> {
	const hit = sessions.get(slug);
	if (!force && hit && Date.now() - hit.at < SESSION_TTL) return hit.p;
	const p = openSession(slug).catch((e) => {
		sessions.delete(slug);
		throw e;
	});
	sessions.set(slug, { at: Date.now(), p });
	return p;
}

/** Detail: GET {mantle}/api/ats/job-postings/{jobId} → { jobPosting: {description, qualifications, ...} }. */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	let session = await cachedSession(slug);
	if (session === "gone") return null;
	const get = (s: PcSession) => fetchRetry(`${s.mantle}api/ats/job-postings/${encodeURIComponent(job.id)}`, { headers: s.headers });
	let res = await get(session);
	if (res.status === 401 || res.status === 403) {
		// JWT expired: refresh the session once.
		await res.body?.cancel();
		session = await cachedSession(slug, true);
		if (session === "gone") return null;
		res = await get(session);
	}
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`paycom ${slug}: HTTP ${res.status} on job-postings/${job.id}`);
	const body = (await res.json()) as { jobPosting?: PcPosting };
	const p = body.jobPosting;
	if (!p) return null;
	const parts: string[] = [];
	if (p.description?.trim()) parts.push(p.description);
	if (p.qualifications?.trim()) parts.push(`<h3>${p.qualificationsTitle || "Qualifications"}</h3>\n${p.qualifications}`);
	const locs = [p.location, ...(p.secondaryLocations ?? [])].map((l) => l?.trim()).filter(Boolean) as string[];
	let publishedAt: string | null | undefined;
	try {
		const g = JSON.parse(p.googleJobJson ?? "") as { datePosted?: string };
		if (g.datePosted) publishedAt = g.datePosted;
	} catch {
		/* no structured data */
	}
	return {
		content: parts.length ? parts.join("\n") : null,
		raw: p,
		location: locs.length ? locs.join("; ") : p.remoteType?.trim() || undefined,
		publishedAt,
		departments: p.jobCategory?.trim() ? [p.jobCategory.trim()] : undefined,
	};
}

export const paycom: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const session = await openSession(slug);
		if (session === "gone") return { status: "gone" };
		const { mantle, headers } = session;

		const previews: PcPreview[] = [];
		let skip = 0;
		let total = Infinity;
		while (skip < total) {
			const res = await fetchRetry(`${mantle}api/ats/job-posting-previews/search`, {
				method: "POST",
				headers,
				body: JSON.stringify({ skip, take: PAGE, filtersForQuery: emptyFilters() }),
			});
			if (!res.ok) throw new Error(`paycom ${slug}: HTTP ${res.status} on search (skip=${skip})`);
			const body = (await res.json()) as PcSearchResponse;
			if (!Array.isArray(body.jobPostingPreviews)) throw new Error(`paycom ${slug}: unexpected search response`);
			previews.push(...body.jobPostingPreviews);
			total = body.jobPostingPreviewsCount ?? previews.length;
			if (body.jobPostingPreviews.length === 0) break;
			skip += body.jobPostingPreviews.length;
		}

		const jobs: Job[] = previews.map((p) => ({
			id: String(p.jobId),
			title: p.jobTitle,
			location: p.locations && p.locations.trim() ? p.locations.trim() : p.remoteType && p.remoteType.trim() ? p.remoteType.trim() : null,
			url: `${PORTAL}/${slug}/jobs/${p.jobId}`,
			departments: [],
			publishedAt: null,
			updatedAt: null,
			// Previews only carry a truncated description snippet; full text needs a per-job request.
			content: null,
			raw: p,
		}));
		return { status: "ok", jobs };
	},
};

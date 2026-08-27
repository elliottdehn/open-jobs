import type { AtsFetcher, FetchResult, Job } from "./types";

/**
 * Dayforce (Ceridian) career sites on jobs.dayforcehcm.com.
 * Slug: "{clientNamespace}/{careerSiteXRefCode}" (e.g. "acv/CANDIDATEPORTAL"), or a bare
 * "{clientNamespace}" which we resolve as "{clientNamespace}/CANDIDATEPORTAL".
 *
 * Backend (Next.js proxy under /api/geo/{ns}/...):
 *   GET  /api/geo/{ns}/sitecontext/{ns}/{site}/{lang}   -> site info (404 => no such board)
 *   GET  /api/auth/csrf                                  -> next-auth csrf token + cookie
 *   POST /api/geo/{ns}/jobposting/search                 -> { jobPostings, maxCount, offset, count }
 * POSTs require the X-CSRF-TOKEN header AND the matching __Host-next-auth.csrf-token cookie.
 * Page size is fixed at 25 (paginationStart offset).
 */

interface DfLocation {
	formattedAddress?: string | null;
	cityName?: string | null;
	stateCode?: string | null;
	isoCountryCode?: string | null;
}

interface DfPosting {
	clientNamespace: string;
	jobBoardId: number;
	jobPostingId: number;
	jobReqId?: number;
	jobTitle: string;
	jobDescription?: string | null;
	hasVirtualLocation?: boolean;
	postingStartTimestampUTC?: string | null;
	postingExpiryTimestampUTC?: string | null;
	postingLocations?: DfLocation[] | null;
}

interface DfSearchResponse {
	jobPostings: DfPosting[];
	maxCount: number;
	offset?: number;
	count?: number;
}

const BASE = "https://jobs.dayforcehcm.com";
const UA = "open-jobs/0.1";
const LANG = "en-US";
const PAGE = 25;
const CONCURRENCY = 6;

function parseSlug(slug: string): { ns: string; site: string } {
	const parts = slug.split("/").filter(Boolean);
	if (parts.length === 0 || parts.length > 2) throw new Error(`dayforce ${slug}: bad slug`);
	return { ns: parts[0], site: parts[1] ?? "CANDIDATEPORTAL" };
}

function location(p: DfPosting): string | null {
	const locs = (p.postingLocations ?? [])
		.map((l) => {
			if (l.formattedAddress && l.formattedAddress.trim()) return l.formattedAddress.trim();
			const parts = [l.cityName, l.stateCode, l.isoCountryCode].filter((x): x is string => !!x && x.trim() !== "");
			return parts.join(", ");
		})
		.filter((s) => s !== "");
	const uniq = [...new Set(locs)];
	if (uniq.length) return uniq.join("; ");
	return p.hasVirtualLocation ? "Remote" : null;
}

async function csrf(): Promise<{ token: string; cookie: string }> {
	const res = await fetch(`${BASE}/api/auth/csrf`, { headers: { "user-agent": UA, accept: "application/json" } });
	if (!res.ok) throw new Error(`dayforce: HTTP ${res.status} fetching csrf token`);
	const body = (await res.json()) as { csrfToken?: string };
	if (!body.csrfToken) throw new Error("dayforce: csrf token missing");
	// Workers `fetch` doesn't expose getSetCookie on all runtimes; parse the raw header(s) by hand.
	const setCookie: string[] =
		typeof (res.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie === "function"
			? (res.headers as unknown as { getSetCookie: () => string[] }).getSetCookie()
			: [res.headers.get("set-cookie") ?? ""];
	const cookie = setCookie
		.flatMap((h) => h.split(/,(?=\s*[A-Za-z0-9_\-.]+=)/))
		.map((c) => c.split(";")[0].trim())
		.filter((c) => c.includes("next-auth"))
		.join("; ");
	return { token: body.csrfToken, cookie };
}

export const dayforce: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const { ns, site } = parseSlug(slug);
		const nsEnc = encodeURIComponent(ns);
		const siteEnc = encodeURIComponent(site);

		const ctxRes = await fetch(`${BASE}/api/geo/${nsEnc}/sitecontext/${nsEnc}/${siteEnc}/${LANG}`, {
			headers: { "user-agent": UA, accept: "application/json" },
		});
		if (ctxRes.status === 404) return { status: "gone" };
		if (!ctxRes.ok) throw new Error(`dayforce ${slug}: HTTP ${ctxRes.status} on sitecontext`);
		const ctx = (await ctxRes.json()) as { jobBoardCode?: string; clientNamespace?: string; isDisabled?: boolean | null };
		const boardCode = ctx.jobBoardCode ?? site;

		const { token, cookie } = await csrf();
		const headers: Record<string, string> = {
			"user-agent": UA,
			accept: "application/json",
			"content-type": "application/json",
			"x-csrf-token": token,
		};
		if (cookie) headers.cookie = cookie;

		const search = async (start: number): Promise<DfSearchResponse> => {
			const res = await fetch(`${BASE}/api/geo/${nsEnc}/jobposting/search`, {
				method: "POST",
				headers,
				body: JSON.stringify({ clientNamespace: ns, jobBoardCode: boardCode, cultureCode: LANG, paginationStart: start }),
			});
			if (res.status === 404) throw new Error(`dayforce ${slug}: search 404`);
			if (!res.ok) throw new Error(`dayforce ${slug}: HTTP ${res.status} on search (start=${start})`);
			const body = (await res.json()) as DfSearchResponse;
			if (!Array.isArray(body.jobPostings)) throw new Error(`dayforce ${slug}: unexpected search response`);
			return body;
		};

		const first = await search(0);
		const postings: DfPosting[] = [...first.jobPostings];
		const total = typeof first.maxCount === "number" ? first.maxCount : postings.length;
		const starts: number[] = [];
		for (let s = PAGE; s < total; s += PAGE) starts.push(s);
		const pages: DfSearchResponse[] = new Array(starts.length);
		let next = 0;
		await Promise.all(
			Array.from({ length: Math.min(CONCURRENCY, starts.length) }, async () => {
				while (next < starts.length) {
					const i = next++;
					pages[i] = await search(starts[i]);
				}
			}),
		);
		for (const p of pages) postings.push(...p.jobPostings);

		const seen = new Set<number>();
		const jobs: Job[] = [];
		for (const p of postings) {
			if (seen.has(p.jobPostingId)) continue;
			seen.add(p.jobPostingId);
			jobs.push({
				id: String(p.jobPostingId),
				title: p.jobTitle.trim(),
				location: location(p),
				url: `${BASE}/${LANG}/${nsEnc}/${siteEnc}/jobs/${p.jobPostingId}`,
				departments: [],
				publishedAt: p.postingStartTimestampUTC ?? null,
				updatedAt: null,
				content: p.jobDescription && p.jobDescription.trim() ? p.jobDescription : null,
				raw: p,
			});
		}
		return { status: "ok", jobs };
	},
};

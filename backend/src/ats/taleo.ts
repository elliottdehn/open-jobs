import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

const UA = "open-jobs/0.1";
const CONCURRENCY = 6;

async function mapLimit<T, R>(items: T[], limit: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let i = 0;
	await Promise.all(
		Array.from({ length: Math.min(limit, items.length) }, async () => {
			while (i < items.length) {
				const idx = i++;
				out[idx] = await fn(items[idx]);
			}
		}),
	);
	return out;
}

interface Requisition {
	jobId: string;
	contestNo: string;
	column: string[];
	linkedColumn: number;
	locationsColumns?: number[];
	hotJob?: boolean;
}
interface SearchResponse {
	requisitionList: Requisition[] | null;
	pagingData: { currentPageNo: number; pageSize: number; totalCount: number } | null;
	careerSectionUnAvailable?: boolean;
}

function parseDate(s: string): string | null {
	// Formats seen: "Aug 26, 2026", "08/26/2026", "26-Aug-2026"
	if (!/\d{4}/.test(s)) return null;
	const d = new Date(s + " UTC");
	if (!Number.isNaN(d.getTime())) return d.toISOString();
	return null;
}

function parseLocations(s: string): string | null {
	try {
		const v = JSON.parse(s);
		if (Array.isArray(v)) return v.map(String).filter(Boolean).join("; ") || null;
	} catch {
		/* plain string */
	}
	return s.trim() || null;
}

function unescapeHtmlAttr(s: string): string {
	return s
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
		.replace(/&amp;/g, "&");
}

/** decodeURIComponent that tolerates stray `%` (e.g. "100%") by decoding each run of %XX escapes on its own. */
function lenientDecode(s: string): string {
	try {
		return decodeURIComponent(s);
	} catch {
		return s.replace(/(%[0-9A-Fa-f]{2})+/g, (run) => {
			try {
				return decodeURIComponent(run);
			} catch {
				return run;
			}
		});
	}
}

/**
 * Detail: GET /careersection/{cs}/jobdetail.ftl?job={contestNo}&lang=en. The page is rendered client-side from
 * the hidden input `initialHistory`: a URL-encoded, `!|!`-delimited state blob whose rich-text fields
 * (description, qualifications, ...) are prefixed with `!*!`. Unavailable jobs render `requisitionUnavailableInterface`.
 */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const res = await fetchRetry(job.url, { headers: { "user-agent": UA, accept: "text/html" }, redirect: "follow" });
	if (res.status === 404 || res.status === 410) return null;
	if (!res.ok) throw new Error(`taleo ${slug}: HTTP ${res.status} on jobdetail for ${job.id}`);
	const html = await res.text();
	const m = /id="initialHistory"\s+value="([^"]*)"/.exec(html);
	if (!m || !m[1]) {
		if (/requisitionUnavailableInterface|unavailablerequisition/i.test(html)) return null;
		throw new Error(`taleo ${slug}: initialHistory not found on jobdetail for ${job.id}`);
	}
	const blob = lenientDecode(unescapeHtmlAttr(m[1]));
	const parts = blob.split("!|!").map((p) => p.replace(/\\(.)/g, "$1"));
	const rich: string[] = [];
	for (const p of parts) {
		if (!p.startsWith("!*!")) continue;
		const v = p.slice(3).trim();
		if (v && !rich.includes(v)) rich.push(v);
	}
	if (rich.length === 0) {
		if (/requisitionUnavailableInterface|unavailablerequisition/i.test(html)) return null;
		return { content: null, raw: parts };
	}
	// First date-like scalar after the rich-text fields is the posting date.
	let publishedAt: string | null | undefined;
	const lastRich = parts.findLastIndex((p) => p.startsWith("!*!"));
	for (const p of parts.slice(lastRich + 1)) {
		if (/^[A-Z][a-z]{2} \d{1,2}, \d{4}/.test(p) || /^\d{2}\/\d{2}\/\d{4}/.test(p)) {
			publishedAt = parseDate(p.replace(/,\s*\d{1,2}:\d{2}(:\d{2})?\s*[AP]M$/i, ""));
			break;
		}
	}
	return { content: rich.join("\n"), raw: parts, publishedAt };
}

export const taleo: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		if (slug === "tbe") {
			throw new Error("taleo tbe: tbe.taleo.net (Taleo Business Edition) needs a per-company org code, not derivable from slug");
		}
		const host = `https://${slug}.taleo.net`;
		// Discover the career section id and portal number by following the default redirect.
		let res: Response;
		try {
			res = await fetch(`${host}/careersection/jobsearch.ftl?lang=en`, {
				headers: { "user-agent": UA, accept: "text/html" },
				redirect: "follow",
			});
		} catch (e) {
			if (e instanceof TypeError) return { status: "gone" }; // NXDOMAIN
			throw e;
		}
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`taleo ${slug}: HTTP ${res.status} discovering career section`);
		const html = await res.text();
		const csM = res.url.match(/\/careersection\/([^/?#]+)\//);
		const portalM = html.match(/portal=(\d+)/);
		if (!csM || !portalM) {
			if (/TaleoSSO|login/i.test(html) && !/jobsearch/i.test(html)) throw new Error(`taleo ${slug}: career section requires SSO login`);
			throw new Error(`taleo ${slug}: could not discover career section/portal (final url ${res.url})`);
		}
		const cs = csM[1];
		const portal = portalM[1];
		const searchUrl = `${host}/careersection/rest/jobboard/searchjobs?lang=en&portal=${portal}`;

		const page = async (pageNo: number): Promise<SearchResponse> => {
			const r = await fetch(searchUrl, {
				method: "POST",
				headers: {
					"user-agent": UA,
					accept: "application/json",
					"content-type": "application/json",
					tz: "GMT+00:00",
					tzname: "UTC",
				},
				body: JSON.stringify({ pageNo }),
			});
			if (!r.ok) throw new Error(`taleo ${slug}: HTTP ${r.status} on searchjobs page ${pageNo}`);
			const body = (await r.json()) as SearchResponse;
			if (body.careerSectionUnAvailable || !body.requisitionList || !body.pagingData)
				throw new Error(`taleo ${slug}: career section ${cs} unavailable (portal ${portal})`);
			return body;
		};

		const first = await page(1);
		const { pageSize, totalCount } = first.pagingData!;
		const totalPages = pageSize > 0 ? Math.ceil(totalCount / pageSize) : 1;
		const pageNos: number[] = [];
		for (let p = 2; p <= totalPages; p++) pageNos.push(p);
		const rest = await mapLimit(pageNos, CONCURRENCY, async (p) => (await page(p)).requisitionList!);

		const seen = new Set<string>();
		const jobs: Job[] = [];
		for (const req of [first.requisitionList!, ...rest].flat()) {
			const id = req.contestNo || req.jobId;
			if (seen.has(id)) continue;
			seen.add(id);
			const cols = req.column ?? [];
			const title = cols[req.linkedColumn] ?? cols[0] ?? "";
			const locIdx = req.locationsColumns ?? [];
			const location = locIdx.length ? parseLocations(cols[locIdx[0]] ?? "") : null;
			let publishedAt: string | null = null;
			cols.forEach((c, i) => {
				if (i === req.linkedColumn || locIdx.includes(i) || publishedAt) return;
				publishedAt = parseDate(c);
			});
			jobs.push({
				id,
				title,
				location,
				url: `${host}/careersection/${cs}/jobdetail.ftl?job=${encodeURIComponent(req.contestNo)}&lang=en`,
				departments: [],
				publishedAt,
				updatedAt: null,
				content: null,
				raw: req,
			});
		}
		return { status: "ok", jobs };
	},
};

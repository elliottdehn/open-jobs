import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * GoHire.
 *
 * Slugs are careers-page path segments `{company-name}-{suffix}`. Two flavours exist:
 *  - `{name}-{hash}` (8-char alnum hash, e.g. `airhosted-gmbh-5xaxoynd`): the hash is the client
 *    hash used by the widget API directly.
 *  - `{name}-{numericId}` (e.g. `acme-15988`): legacy form; jobs.gohire.io 404s on these, and the
 *    numeric client id is not accepted by any public endpoint. We resolve numeric id -> client hash
 *    via the public sitemap (which lists job URLs under the legacy slug) + POST /getJobId (which
 *    returns `clientHash`).
 *
 * Job list: GET https://api2.gohire.io/widget-jobs/{clientHash} -> { jobs: [...] } (all jobs, no
 * paging; unknown hash returns `{}`). List items have an empty description; `fetchDetail` uses
 * POST https://api.gohire.io/getJobId (jobId) which returns HTML `jobDescr`.
 */

const UA = "open-jobs/0.1";
const SITEMAP_URL = "https://jobs.gohire.io/sitemap.txt";

interface GhWidgetJob {
	id: number;
	title: string;
	description?: string;
	location?: string;
	salary?: string;
	type?: string;
	date?: string; // "20 September, 2022"
	link: string;
}

interface GhJobDetail {
	clientHash?: string;
	jobId?: string;
	jobTitle?: string;
	jobDescr?: string;
	jobCreatedOn?: string; // unix seconds
	jobCounty?: string;
	countryName?: string;
	companyName?: string;
}

const MONTHS: Record<string, number> = {
	january: 0, february: 1, march: 2, april: 3, may: 4, june: 5, july: 6, august: 7,
	september: 8, october: 9, november: 10, december: 11,
};

function parseListDate(s: string | undefined): string | null {
	if (!s) return null;
	const m = /^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$/.exec(s.trim());
	if (!m) return null;
	const mon = MONTHS[m[2].toLowerCase()];
	if (mon === undefined) return null;
	return new Date(Date.UTC(Number(m[3]), mon, Number(m[1]))).toISOString();
}

/** `jobCreatedOn` is a unix timestamp, usually seconds but occasionally milliseconds. */
function parseCreatedOn(s: string | undefined): string | null {
	if (!s || !/^\d+$/.test(s)) return null;
	const n = Number(s);
	const ms = n > 1e11 ? n : n * 1000;
	const d = new Date(ms);
	return isNaN(d.getTime()) || d.getFullYear() > 2100 ? null : d.toISOString();
}

/** Cached sitemap index: legacy slug -> first job id. */
let sitemapIndex: Promise<Map<string, string>> | null = null;

function loadSitemapIndex(): Promise<Map<string, string>> {
	if (!sitemapIndex) {
		sitemapIndex = (async () => {
			const res = await fetchRetry(SITEMAP_URL, { headers: { "user-agent": UA } });
			if (!res.ok) throw new Error(`gohire sitemap: HTTP ${res.status}`);
			const text = await res.text();
			const map = new Map<string, string>();
			const re = /jobs\.gohire\.io\/([^/\s]+)\/[^/\s]*?-(\d+)\/?\s*$/gm;
			let m: RegExpExecArray | null;
			while ((m = re.exec(text))) {
				if (!map.has(m[1])) map.set(m[1], m[2]);
			}
			return map;
		})().catch((e) => {
			sitemapIndex = null;
			throw e;
		});
	}
	return sitemapIndex;
}

async function getJobDetail(jobId: string | number): Promise<GhJobDetail | null> {
	const form = new FormData();
	form.append("jobId", String(jobId));
	const res = await fetchRetry("https://api.gohire.io/getJobId", {
		method: "POST",
		body: form,
		headers: { "user-agent": UA, accept: "application/json" },
	});
	if (!res.ok) throw new Error(`gohire getJobId ${jobId}: HTTP ${res.status}`);
	const body = (await res.json()) as unknown;
	if (!Array.isArray(body) || !body[0]) return null;
	return body[0] as GhJobDetail;
}

async function resolveClientHash(slug: string): Promise<string | null> {
	const m = /-([A-Za-z0-9]+)$/.exec(slug);
	if (!m) return null;
	const suffix = m[1];
	if (!/^\d+$/.test(suffix)) return suffix; // hash form
	const index = await loadSitemapIndex();
	const jobId = index.get(slug);
	if (!jobId) return null;
	const detail = await getJobDetail(jobId);
	return detail?.clientHash ?? null;
}

/** Detail: POST api.gohire.io/getJobId -> [{ jobDescr (HTML), jobCreatedOn, ... }]; `[]` when unknown/closed. */
async function fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
	const d = await getJobDetail(job.id);
	if (!d) return null;
	const created = parseCreatedOn(d.jobCreatedOn);
	return {
		content: d.jobDescr || null,
		raw: d,
		publishedAt: created ?? undefined,
	};
}

export const gohire: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const hash = await resolveClientHash(slug);
		if (!hash) {
			// Legacy numeric slug with no sitemap entry: board is either gone or has no open jobs.
			// The public careers page 404s for these slugs, so we cannot distinguish; report empty.
			if (/-\d+$/.test(slug)) return { status: "ok", jobs: [] };
			return { status: "gone" };
		}
		const res = await fetchRetry(`https://api2.gohire.io/widget-jobs/${encodeURIComponent(hash)}`, {
			headers: { "user-agent": UA, accept: "application/json", "widget-host": "jobs.gohire.io" },
		});
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`gohire ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { jobs?: GhWidgetJob[] };
		if (!body || !Array.isArray(body.jobs)) {
			// Unknown client hash returns `{}`.
			if (body && typeof body === "object" && Object.keys(body).length === 0) return { status: "gone" };
			throw new Error(`gohire ${slug}: unexpected response shape`);
		}
		const list = body.jobs;

		const jobs: Job[] = list.map((j) => {
			return {
				id: String(j.id),
				title: (j.title ?? "").trim(),
				location: j.location?.trim() || null,
				url: j.link,
				departments: [],
				publishedAt: parseListDate(j.date),
				updatedAt: null,
				content: j.description || null,
				raw: j,
			};
		});
		return { status: "ok", jobs };
	},
};

import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

const UA = "open-jobs/0.1";
const CONCURRENCY = 6;
const PAGE = 50;

function text(s: string): string {
	return s
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
		.replace(/&amp;/g, "&")
		.replace(/\s*\n\s*/g, " ")
		.replace(/\s+/g, " ")
		.replace(/\s+,/g, ",")
		.trim()
		.replace(/,$/, "");
}

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

async function getHtml(url: string): Promise<{ status: number; location: string; html: string }> {
	const res = await fetch(url, { headers: { "user-agent": UA, accept: "text/html" }, redirect: "manual" });
	return { status: res.status, location: res.headers.get("location") ?? "", html: res.ok ? await res.text() : "" };
}

interface Row {
	id: string;
	title: string;
	location: string | null;
	shift: string | null;
	type: string | null;
}

function parseRows(slug: string, html: string): Row[] {
	const rows: Row[] = [];
	const esc = slug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const re = new RegExp(`<a [^>]*href="/${esc}/job/([A-Za-z0-9]+)"[^>]*>([\\s\\S]*?)</a>`, "gi");
	let m: RegExpExecArray | null;
	while ((m = re.exec(html))) {
		const id = m[1];
		let block = m[2];
		let title: string;
		const nameIn = block.match(/jv-job-list-name"[^>]*>([\s\S]*?)<\/(div|td|span|p)>/);
		if (nameIn) title = text(nameIn[1]);
		else title = text(block);
		// Table layout: the <a> only wraps the title; location lives in sibling <td>s up to </tr>.
		if (!/jv-job-list-location/.test(block)) {
			const tail = html.slice(m.index + m[0].length);
			const end = tail.search(/<\/tr>|<\/li>|<a /);
			block += end >= 0 ? tail.slice(0, end) : "";
		}
		const pick = (cls: string): string | null => {
			const x = block.match(new RegExp(`${cls}"[^>]*>([\\s\\S]*?)<\\/(div|td|span|p)>`));
			return x ? text(x[1]) || null : null;
		};
		rows.push({ id, title, location: pick("jv-job-list-location"), shift: pick("jv-job-list-shift"), type: pick("jv-job-list-type") });
	}
	return rows;
}

/** /{slug}/jobs groups openings under <h3> department headers; build id -> department. */
function parseDepartments(slug: string, html: string): Map<string, string> {
	const map = new Map<string, string>();
	const esc = slug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const re = new RegExp(`<h3[^>]*>([\\s\\S]*?)</h3>|<a [^>]*href="/${esc}/job/([A-Za-z0-9]+)"`, "gi");
	let current = "";
	let m: RegExpExecArray | null;
	while ((m = re.exec(html))) {
		if (m[1] !== undefined) current = text(m[1]);
		else if (m[2] && current) map.set(m[2], current);
	}
	return map;
}

/**
 * Detail: GET jobs.jobvite.com/{slug}/job/{id} (HTML). Description lives in <div class="jv-job-detail-description">,
 * department/location in <p class="jv-job-detail-meta">. Unknown ids 303-redirect to .../jobs?error=404.
 */
async function fetchDetail(slug: string, job: Job): Promise<JobDetail | null> {
	const res = await fetchRetry(job.url, { headers: { "user-agent": UA, accept: "text/html" }, redirect: "manual" });
	if (res.status === 404 || res.status === 410) return null;
	if (res.status >= 300 && res.status < 400) {
		const loc = res.headers.get("location") ?? "";
		await res.body?.cancel();
		if (/error=404|invalid=1|\/404\.html/.test(loc)) return null;
		throw new Error(`jobvite ${slug}: unexpected redirect to ${loc} for job ${job.id}`);
	}
	if (!res.ok) throw new Error(`jobvite ${slug}: HTTP ${res.status} on job ${job.id}`);
	const html = await res.text();
	const start = html.indexOf('class="jv-job-detail-description"');
	if (start < 0) return null;
	// Body runs until the apply-bar / meta / footer that follows the description block.
	const tail = html.slice(start);
	const end = tail.search(/<div class="jv-job-detail-bottom|<div class="jv-job-detail-top-actions|<div class="jv-footer|<div class="jv-page-footer|<\/section>/);
	let content = tail.slice(tail.indexOf(">") + 1, end > 0 ? end : undefined);
	// Drop the trailing unclosed wrapper tags.
	content = content.replace(/(\s*<\/div>)+\s*$/, "").trim();
	const meta = html.match(/class="jv-job-detail-meta"[^>]*>([\s\S]*?)<\/p>/);
	let location: string | null | undefined;
	let departments: string[] | undefined;
	if (meta) {
		const bits = meta[1]
			.split(/<span class=['"]jv-inline-separator['"]><\/span>/)
			.map((b) => text(b))
			.filter(Boolean);
		if (bits.length >= 2) {
			departments = [bits[0]];
			location = bits.slice(1).join(", ");
		} else if (bits.length === 1) location = bits[0];
	}
	return { content: content || null, location, departments };
}

export const jobvite: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const base = `https://jobs.jobvite.com/${encodeURIComponent(slug)}`;
		const searchUrl = (p: number) => `${base}/search/?p=${p}`;
		const first = await getHtml(searchUrl(0));
		if (first.status === 404) return { status: "gone" };
		if (first.status >= 300 && first.status < 400) {
			if (/invalid=1|\/404\.html/.test(first.location)) return { status: "gone" };
			throw new Error(`jobvite ${slug}: unexpected redirect to ${first.location}`);
		}
		if (first.status !== 200) throw new Error(`jobvite ${slug}: HTTP ${first.status}`);

		const rows = parseRows(slug, first.html);
		const tm = first.html.match(/jv-pagination-text">\s*\d+\s*-\s*\d+\s*of\s*(\d+)/);
		const total = tm ? Number(tm[1]) : rows.length;
		const pages: number[] = [];
		for (let p = 1; p * PAGE < total; p++) pages.push(p);
		const [rest, deptPage] = await Promise.all([
			mapLimit(pages, CONCURRENCY, async (p) => {
				const r = await getHtml(searchUrl(p));
				if (r.status !== 200) throw new Error(`jobvite ${slug}: HTTP ${r.status} on page ${p}`);
				return parseRows(slug, r.html);
			}),
			getHtml(`${base}/jobs`),
		]);
		const depts = deptPage.status === 200 ? parseDepartments(slug, deptPage.html) : new Map<string, string>();

		const seen = new Set<string>();
		const jobs: Job[] = [];
		for (const r of [rows, ...rest].flat()) {
			if (seen.has(r.id)) continue;
			seen.add(r.id);
			const dept = depts.get(r.id);
			jobs.push({
				id: r.id,
				title: r.title,
				location: r.location,
				url: `${base}/job/${r.id}`,
				departments: dept ? [dept] : [],
				publishedAt: null,
				updatedAt: null,
				content: null,
				raw: { ...r, department: dept ?? null },
			});
		}
		return { status: "ok", jobs };
	},
};

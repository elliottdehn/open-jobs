import type { AtsFetcher, FetchResult, Job } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Cornerstone OnDemand (CSOD) career sites at {corp}.csod.com. Slug = the corp subdomain, mined from
 * the Common Crawl index (*.csod.com). The site is a SPA, but its public search API returns the full
 * job (incl. externalDescription), so this is a single stage — no fetchDetail.
 *
 * Flow (all discovered at fetch time, so it is region-agnostic — CSOD shards its cloud API by region):
 *   1. GET /ats/careersite/search.aspx  -> 302 to /ux/ats/careersite/{siteId}/home : yields careerSiteId.
 *   2. GET /ux/ats/careersite/{siteId}/home : the shell embeds `csod.context` with a fresh JWT `token`,
 *      the regional cloud base (endpoints.cloud), and the culture.
 *   3. POST {cloud}rec-job-search/external/jobs  (Bearer token) : paged {data:{totalCount, requisitions}}.
 * SSO-gated / inactive corps redirect to RestrictedArea and yield no siteId -> gone.
 */
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) open-jobs/0.1";
const PAGE = 50;
const MAX_PAGES = 200; // 10k jobs ceiling per corp

interface CsodReq {
	requisitionId: number | string;
	displayJobTitle?: string;
	postingEffectiveDate?: string;
	locations?: { city?: string; state?: string; country?: string }[];
	externalDescription?: string;
}
interface CsodContext { cultureID?: number; cultureName?: string; endpoints?: { cloud?: string }; token?: string }

/** "9/1/2026" -> "2026-09-01"; passes through anything already ISO-ish. */
function toISO(d?: string): string | null {
	if (!d) return null;
	const us = d.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/); // US locale: M/D/YYYY
	if (us) return `${us[3]}-${us[1].padStart(2, "0")}-${us[2].padStart(2, "0")}`;
	const eu = d.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/); // EU locale: D.M.YYYY
	if (eu) return `${eu[3]}-${eu[2].padStart(2, "0")}-${eu[1].padStart(2, "0")}`;
	return d;
}
function locOf(r: CsodReq): string | null {
	const parts = (r.locations ?? []).map((l) => [l.city, l.state, l.country].filter(Boolean).join(", ")).filter(Boolean);
	return parts.length ? parts.slice(0, 4).join("; ") : null;
}

export const cornerstone: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const host = `https://${slug}.csod.com`;
		// 1. discover careerSiteId
		const disc = await fetchRetry(`${host}/ats/careersite/search.aspx`, { headers: { "user-agent": UA }, redirect: "manual" });
		const loc = disc.headers.get("location") ?? "";
		const siteId = loc.match(/\/careersite\/(\d+)\/home/)?.[1];
		if (!siteId) return { status: "gone" }; // SSO-gated / no public career site
		// 2. scrape the shell config (fresh token + regional cloud base + culture)
		const shellRes = await fetchRetry(`${host}/ux/ats/careersite/${siteId}/home`, { headers: { "user-agent": UA } });
		if (!shellRes.ok) return { status: "gone" };
		const shell = await shellRes.text();
		const cm = shell.match(/csod\.context=(\{[\s\S]*?\})\s*;?\s*<\/script>/);
		if (!cm) return { status: "gone" };
		let ctx: CsodContext;
		try { ctx = JSON.parse(cm[1]) as CsodContext; } catch { return { status: "gone" }; }
		const cloud = ctx.endpoints?.cloud; const token = ctx.token;
		if (!cloud || !token) return { status: "gone" };
		const cultureId = ctx.cultureID ?? 1; const cultureName = ctx.cultureName ?? "en-US";
		// 3. paged search (the response carries the full externalDescription, so no detail stage)
		const jobs: Job[] = []; const seen = new Set<string>();
		let total = Infinity;
		for (let page = 1; page <= MAX_PAGES && jobs.length < total; page++) {
			const body = JSON.stringify({
				careerSiteId: Number(siteId), careerSitePageId: Number(siteId), pageNumber: page, pageSize: PAGE,
				cultureId, cultureName, searchText: "", states: [], countryCodes: [], cities: [], placeID: "",
				radius: null, postingsWithinDays: null, customFieldCheckboxKeys: [], customFieldDropdowns: [], customFieldRadios: [],
			});
			const res = await fetchRetry(`${cloud}rec-job-search/external/jobs`, {
				method: "POST",
				headers: { "user-agent": UA, "content-type": "application/json", accept: "application/json", authorization: `Bearer ${token}`, origin: host },
				body,
			});
			if (!res.ok) { if (page === 1) throw new Error(`cornerstone search HTTP ${res.status}`); break; }
			const data = (await res.json()) as { data?: { totalCount?: number; requisitions?: CsodReq[] } };
			const reqs = data.data?.requisitions ?? [];
			total = data.data?.totalCount ?? reqs.length;
			if (!reqs.length) break;
			for (const r of reqs) {
				const id = String(r.requisitionId);
				if (!r.requisitionId || seen.has(id) || !r.displayJobTitle) continue;
				seen.add(id);
				jobs.push({
					id,
					title: r.displayJobTitle.trim(),
					location: locOf(r),
					url: `${host}/ux/ats/careersite/${siteId}/home/requisition/${id}`,
					departments: [],
					publishedAt: toISO(r.postingEffectiveDate),
					updatedAt: null,
					content: r.externalDescription ?? null,
					raw: null,
				});
			}
		}
		return { status: "ok", jobs };
	},
};

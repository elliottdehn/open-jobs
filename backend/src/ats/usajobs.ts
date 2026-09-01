import type { AtsFetcher, FetchResult, FetchCtx, Job } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * USAJobs — the US federal government's official job site (data.usajobs.gov public API).
 * Slug = an Organization code (e.g. "AF00" = Air Force); the search API caps any query at 10,000
 * results, so the board list is the ~1,067 federal orgs from /api/codelist/agencysubelements, each
 * well under the cap. Needs the free API key (env.USAJOBS_KEY) + the registered email as User-Agent.
 * The full posting is in the search result (no detail stage).
 */
const EMAIL = "dehnbostele@gmail.com";
const PER_PAGE = 500;
const MAX_PAGES = 20;

interface Loc { LocationName?: string; CountryCode?: string }
interface Remun { MinimumRange?: string; MaximumRange?: string; RateIntervalCode?: string }
interface Descriptor {
	PositionID?: string; PositionTitle?: string; PositionURI?: string; OrganizationName?: string;
	PositionLocation?: Loc[]; PositionRemuneration?: Remun[]; PublicationStartDate?: string; ApplicationCloseDate?: string;
	JobCategory?: { Name?: string }[];
	UserArea?: { Details?: { JobSummary?: string; MajorDuties?: string; Requirements?: string; Qualifications?: string; Education?: string; RemoteIndicator?: boolean; TeleworkEligible?: boolean } };
}

function jd(d: Descriptor): string {
	const x = d.UserArea?.Details ?? {};
	return [x.JobSummary, x.MajorDuties && `Major duties:\n${x.MajorDuties}`, x.Qualifications && `Qualifications:\n${x.Qualifications}`,
		x.Requirements && `Requirements:\n${x.Requirements}`, x.Education && `Education:\n${x.Education}`]
		.filter(Boolean).join("\n\n").slice(0, 20_000);
}
function loc(d: Descriptor): string | null {
	const det = d.UserArea?.Details;
	const remote = det?.RemoteIndicator ? "Remote" : det?.TeleworkEligible ? "Telework eligible" : "";
	const places = (d.PositionLocation ?? []).map((l) => l.LocationName).filter(Boolean).slice(0, 4).join("; ");
	return [remote, places].filter(Boolean).join(" · ") || null;
}

export const usajobs: AtsFetcher = {
	async fetchJobs(slug: string, ctx?: FetchCtx): Promise<FetchResult> {
		const key = ctx?.env?.USAJOBS_KEY;
		if (!key) throw new Error("USAJOBS_KEY not set");
		const headers = { Host: "data.usajobs.gov", "User-Agent": EMAIL, "Authorization-Key": key };
		const jobs: Job[] = []; const seen = new Set<string>();
		for (let page = 1; page <= MAX_PAGES; page++) {
			const url = `https://data.usajobs.gov/api/search?Organization=${encodeURIComponent(slug)}&ResultsPerPage=${PER_PAGE}&Page=${page}`;
			const res = await fetchRetry(url, { headers });
			if (res.status === 404) return { status: "gone" };
			if (!res.ok) throw new Error(`usajobs HTTP ${res.status}`);
			const data = (await res.json()) as { SearchResult?: { SearchResultItems?: { MatchedObjectDescriptor?: Descriptor }[] } };
			const items = data.SearchResult?.SearchResultItems ?? [];
			for (const it of items) {
				const d = it.MatchedObjectDescriptor; if (!d?.PositionID || !d.PositionTitle || seen.has(d.PositionID)) continue;
				seen.add(d.PositionID);
				const pay = (d.PositionRemuneration ?? [])[0];
				jobs.push({
					id: d.PositionID,
					title: d.PositionTitle,
					location: loc(d),
					url: d.PositionURI || `https://www.usajobs.gov/job/${encodeURIComponent(d.PositionID)}`,
					departments: (d.JobCategory ?? []).map((c) => c.Name).filter((x): x is string => !!x),
					publishedAt: d.PublicationStartDate ?? null,
					updatedAt: null,
					content: jd(d) || null,
					raw: pay?.MinimumRange ? { pay_min: pay.MinimumRange, pay_max: pay.MaximumRange, pay_interval: pay.RateIntervalCode } : null,
				});
			}
			if (items.length < PER_PAGE) break;
		}
		return { status: "ok", jobs };
	},
};

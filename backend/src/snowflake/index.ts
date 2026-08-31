import type { AtsFetcher, FetchResult } from "../ats/types";

/**
 * Abandoned experiment (2026-08-31): custom extractors for big companies' own career sites
 * ("snowflakes"). One board (snowflake/amazon) was created during the experiment; this stub answers
 * "gone" for every slug so that DO retires through the normal dead-board path instead of erroring on
 * its daily alarm. The useful outcomes were kept as real ATS fetchers: phenom.ts (CVS, Cencora) and
 * jibe.ts (Costco), plus jpmc.fa.oraclecloud.com on the existing oraclecloud fetcher.
 */
export const snowflake: AtsFetcher = {
	fetchJobs(): Promise<FetchResult> {
		return Promise.resolve({ status: "gone" });
	},
};

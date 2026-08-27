import type { AtsFetcher, FetchResult } from "./types";

/**
 * UKG Pro (UltiPro) recruiting.
 *
 * The slugs we have (`recruiting2`, `nw14`, `rn12`, `n23`, `signin`, `ew22`) are only the
 * `*.ultipro.com` datacenter hostnames, not company identifiers. A UKG job board lives at
 * https://{host}.ultipro.com/{COMPANY_CODE}/JobBoard/{boardGuid}/ and cannot be derived from the
 * hostname alone (the hosts redirect to login/maintenance pages). Nothing to fetch.
 */
export const ukg: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		throw new Error(
			`ukg: slug "${slug}" is an ultipro.com datacenter hostname, not a company board; UKG boards need a company code and board GUID (https://{host}.ultipro.com/{company}/JobBoard/{guid}/) which cannot be derived from the slug`,
		);
	},
};

import type { AtsFetcher, FetchResult } from "./types";

/**
 * SAP SuccessFactors.
 *
 * The slugs we have (`rmkcdn`, `dmscdn`, `performancemanager5na`) are SuccessFactors CDN /
 * datacenter hostnames (rmkcdn.successfactors.com etc.), not company identifiers. A SuccessFactors
 * career site lives at https://career{N}.successfactors.com/career?company={companyId} or
 * https://jobs.sap.com-style Recruiting Marketing sites; neither the companyId nor the RMK site can
 * be derived from these hostnames. Nothing to fetch.
 */
export const successfactors: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		throw new Error(
			`successfactors: slug "${slug}" is a successfactors.com CDN/datacenter hostname, not a company career site; SuccessFactors boards need a companyId (career{N}.successfactors.com/career?company=...) which cannot be derived from the slug`,
		);
	},
};

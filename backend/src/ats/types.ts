export interface Job {
	/** Provider-scoped job id, stable across fetches. */
	id: string;
	title: string;
	location: string | null;
	url: string;
	departments: string[];
	publishedAt: string | null;
	updatedAt: string | null;
	/** Raw HTML description if the provider exposes it. */
	content: string | null;
	/** Untouched provider payload for this job. */
	raw: unknown;
}

export type FetchResult =
	| { status: "ok"; jobs: Job[] }
	/** Board no longer exists (404 etc). */
	| { status: "gone" };

/** Extra per-job data obtained from a detail request (merged over the listing data at read time). */
export interface JobDetail {
	content: string | null;
	/** Provider detail payload, kept alongside the listing `raw`. */
	raw?: unknown;
	publishedAt?: string | null;
	updatedAt?: string | null;
	location?: string | null;
	departments?: string[];
}

export interface AtsFetcher {
	fetchJobs(slug: string): Promise<FetchResult>;
	/**
	 * Optional: page-streaming variant for big boards. The Board prefers this when present: each page
	 * is handed to `sink` and applied to storage immediately, so peak memory is one page instead of
	 * the whole snapshot (a 10k-job board OOMs a 128 MB DO isolate otherwise).
	 */
	fetchJobsStream?(slug: string, sink: (page: Job[]) => Promise<void>): Promise<{ status: "ok" } | { status: "gone" }>;
	/**
	 * Optional: fetch the full posting for one job (providers whose listing has no description).
	 * Called once per new job by the Board (never repeated), with <= 6 in flight per board.
	 * Return null if the posting is unavailable (job likely closed); throw on transient errors.
	 */
	fetchDetail?(slug: string, job: Job): Promise<JobDetail | null>;
}

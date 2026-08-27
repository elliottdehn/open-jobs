import type { AtsFetcher, FetchResult, Job } from "./types";

interface GhJob {
	id: number;
	title: string;
	absolute_url: string;
	location?: { name?: string } | null;
	departments?: { name: string }[];
	first_published?: string | null;
	updated_at?: string | null;
	content?: string | null;
}

/** Greenhouse decodes HTML entities in `content` (it is entity-escaped HTML). */
function decodeEntities(s: string): string {
	return s
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&amp;/g, "&");
}

export const greenhouse: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const url = `https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(slug)}/jobs?content=true`;
		const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`greenhouse ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { jobs: GhJob[] };
		const jobs: Job[] = body.jobs.map((j) => ({
			id: String(j.id),
			title: j.title,
			location: j.location?.name ?? null,
			url: j.absolute_url,
			departments: (j.departments ?? []).map((d) => d.name),
			publishedAt: j.first_published ?? null,
			updatedAt: j.updated_at ?? null,
			content: j.content ? decodeEntities(j.content) : null,
			raw: j,
		}));
		return { status: "ok", jobs };
	},
};

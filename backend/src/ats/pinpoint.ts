import type { AtsFetcher, FetchResult, Job } from "./types";

interface PinpointPosting {
	id: string | number;
	title: string;
	url: string;
	path?: string;
	description?: string | null;
	key_responsibilities?: string | null;
	skills_knowledge_expertise?: string | null;
	benefits?: string | null;
	description_header?: string | null;
	key_responsibilities_header?: string | null;
	skills_knowledge_expertise_header?: string | null;
	benefits_header?: string | null;
	employment_type_text?: string | null;
	workplace_type_text?: string | null;
	deadline_at?: string | null;
	job?: {
		id?: string | number;
		department?: { name?: string } | null;
		division?: { name?: string } | null;
	} | null;
	location?: { name?: string; city?: string; province?: string; country?: string } | null;
	created_at?: string | null;
	updated_at?: string | null;
	published_at?: string | null;
}

function section(header: string | null | undefined, body: string | null | undefined): string {
	if (!body) return "";
	return header ? `<h3>${header}</h3>\n${body}` : body;
}

export const pinpoint: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		// Slug is the company subdomain on pinpointhq.com. postings.json returns every open posting (no pagination).
		const url = `https://${slug}.pinpointhq.com/postings.json`;
		const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
		if (res.status === 404) return { status: "gone" };
		if (!res.ok) throw new Error(`pinpoint ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { data?: PinpointPosting[] };
		if (!Array.isArray(body?.data)) throw new Error(`pinpoint ${slug}: unexpected response shape`);

		const jobs: Job[] = body.data.map((p) => {
			const content = [
				section(p.description_header, p.description),
				section(p.key_responsibilities_header, p.key_responsibilities),
				section(p.skills_knowledge_expertise_header, p.skills_knowledge_expertise),
				section(p.benefits_header, p.benefits),
			]
				.filter(Boolean)
				.join("\n");
			const departments = [p.job?.department?.name, p.job?.division?.name].filter((d): d is string => !!d);
			return {
				id: String(p.id),
				title: p.title,
				location: p.location?.name ?? null,
				url: p.url ?? `https://${slug}.pinpointhq.com${p.path ?? ""}`,
				departments,
				publishedAt: p.published_at ?? p.created_at ?? null,
				updatedAt: p.updated_at ?? null,
				content: content || null,
				raw: p,
			};
		});
		return { status: "ok", jobs };
	},
};

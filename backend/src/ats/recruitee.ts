import type { AtsFetcher, FetchResult, Job } from "./types";

const UA = "open-jobs/0.1";

interface RtOffer {
	id: number;
	title: string;
	slug?: string;
	status?: string;
	location?: string | null;
	city?: string | null;
	country?: string | null;
	remote?: boolean;
	department?: string | null;
	careers_url?: string;
	published_at?: string | null;
	created_at?: string | null;
	updated_at?: string | null;
	description?: string | null;
	requirements?: string | null;
}

/** Recruitee dates look like "2026-08-19 13:16:05 UTC". */
function toIso(s: string | null | undefined): string | null {
	if (!s) return null;
	const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(UTC|Z)?$/.exec(s);
	if (m) return `${m[1]}T${m[2]}Z`;
	const d = new Date(s);
	return isNaN(d.getTime()) ? null : d.toISOString();
}

export const recruitee: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const host = slug.includes(".") ? slug : `${slug}.recruitee.com`;
		// The offers endpoint returns every published offer in one response (no pagination).
		const res = await fetch(`https://${host}/api/offers/`, {
			headers: { accept: "application/json", "user-agent": UA },
		});
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (!res.ok) throw new Error(`recruitee ${slug}: HTTP ${res.status}`);
		const body = (await res.json()) as { offers?: RtOffer[] };
		if (!Array.isArray(body.offers)) throw new Error(`recruitee ${slug}: unexpected response shape`);

		const jobs: Job[] = body.offers.map((o) => {
			const location =
				o.location || [o.city, o.country].filter(Boolean).join(", ") || (o.remote ? "Remote" : null);
			const content = [o.description, o.requirements].filter(Boolean).join("\n") || null;
			return {
				id: String(o.id),
				title: o.title,
				location: location || null,
				url: o.careers_url ?? `https://${host}/o/${o.slug ?? o.id}`,
				departments: o.department ? [o.department] : [],
				publishedAt: toIso(o.published_at ?? o.created_at),
				updatedAt: toIso(o.updated_at),
				content,
				raw: o,
			};
		});
		return { status: "ok", jobs };
	},
};

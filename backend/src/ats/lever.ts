import type { AtsFetcher, FetchResult, Job } from "./types";

interface LeverPosting {
	id: string;
	text: string;
	hostedUrl: string;
	applyUrl?: string;
	createdAt?: number;
	updatedAt?: number;
	categories?: {
		commitment?: string;
		location?: string;
		team?: string;
		department?: string;
		allLocations?: string[];
	};
	country?: string;
	workplaceType?: string;
	description?: string;
	descriptionBody?: string;
	opening?: string;
	additional?: string;
	lists?: { text: string; content: string }[];
}

const PAGE = 100;

function toIso(ms: number | undefined): string | null {
	return typeof ms === "number" && Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

/** Lever splits the description into several HTML fragments; stitch them back together. */
function buildContent(p: LeverPosting): string | null {
	const parts: string[] = [];
	if (p.description) parts.push(p.description);
	else if (p.opening || p.descriptionBody) parts.push(p.opening ?? "", p.descriptionBody ?? "");
	for (const l of p.lists ?? []) parts.push(`<h3>${l.text}</h3><ul>${l.content}</ul>`);
	if (p.additional) parts.push(p.additional);
	const html = parts.filter(Boolean).join("\n");
	return html || null;
}

/**
 * Lever public postings API: https://api.lever.co/v0/postings/<site>?mode=json
 * Slugs are the Lever site name (jobs.lever.co/<site>). Paginates with skip/limit.
 */
export const lever: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const all: LeverPosting[] = [];
		for (let skip = 0; ; skip += PAGE) {
			const url = `https://api.lever.co/v0/postings/${encodeURIComponent(slug)}?mode=json&limit=${PAGE}&skip=${skip}`;
			const res = await fetch(url, { headers: { accept: "application/json", "user-agent": "open-jobs/0.1" } });
			if (res.status === 404) return { status: "gone" };
			if (!res.ok) throw new Error(`lever ${slug}: HTTP ${res.status}`);
			const body = (await res.json()) as unknown;
			if (!Array.isArray(body)) {
				const err = (body as { error?: string } | null)?.error;
				if (err && /not found/i.test(err)) return { status: "gone" };
				throw new Error(`lever ${slug}: unexpected response ${JSON.stringify(body).slice(0, 200)}`);
			}
			all.push(...(body as LeverPosting[]));
			if (body.length < PAGE) break;
		}
		const jobs: Job[] = all.map((p) => {
			const cats = p.categories ?? {};
			const departments = [cats.department, cats.team].filter((x): x is string => !!x);
			const location = cats.allLocations?.length ? cats.allLocations.join("; ") : (cats.location ?? null);
			return {
				id: p.id,
				title: p.text,
				location,
				url: p.hostedUrl,
				departments,
				publishedAt: toIso(p.createdAt),
				updatedAt: toIso(p.updatedAt),
				content: buildContent(p),
				raw: p,
			};
		});
		return { status: "ok", jobs };
	},
};

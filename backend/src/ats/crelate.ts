import type { AtsFetcher, FetchResult, Job, JobDetail } from "./types";
import { fetchRetry } from "./http.ts";

/**
 * Crelate candidate portal (jobs.crelate.com/portal/{slug}).
 *
 * 1. GET https://jobs.crelate.com/api/candidateportal/getclientvars?onv=base64(slug)
 *    -> { ORG_ID, ORG_NAME, ... }; 404 "Organization not found" when the portal doesn't exist.
 * 2. GET https://app.crelate.com/api/candidateportal/GetAllJobs?requestEnvelope={"OrganizationId":...}
 *    -> { Jobs: [...], IsError, ErrorMessage }. Single response, no paging.
 * The list only carries a ~500 char plain-text description snippet (kept as `content`); `fetchDetail`
 * replaces it with the full HTML description via GetJob?requestEnvelope={"JobCode":...}.
 */

const UA = "open-jobs/0.1";

interface CrJob {
	Id: string;
	JobCode: string;
	JobNum?: string;
	Title: string;
	Description?: string | null;
	City?: string | null;
	State?: string | null;
	Country?: string | null;
	PostalCode?: string | null;
	LastPostedOnDate?: string | null;
	LastResetOn?: string | null;
	Tags?: { Id: string; Name: string }[] | null;
	Url?: string;
	CompanyName?: string;
}

interface ClientVars {
	ORG_ID?: string;
	ORG_NAME?: string;
	BASE_URL?: string;
	PORTAL_VERSION?: string;
}

function toIso(s: string | null | undefined): string | null {
	if (!s) return null;
	const d = new Date(s);
	return isNaN(d.getTime()) ? null : d.toISOString();
}

async function apiGet<T>(path: string, envelope: unknown): Promise<T> {
	const url = `https://app.crelate.com/api/candidateportal/${path}?requestEnvelope=${encodeURIComponent(JSON.stringify(envelope))}`;
	const res = await fetchRetry(url, { headers: { accept: "application/json", "user-agent": UA } });
	if (!res.ok) throw new Error(`crelate ${path}: HTTP ${res.status}`);
	return (await res.json()) as T;
}

/** Detail: GetJob?requestEnvelope={"JobCode"} -> { Job: { Description (HTML), ... } }; Job null / IsError when gone. */
async function fetchDetail(_slug: string, job: Job): Promise<JobDetail | null> {
	const jobCode = (job.raw as CrJob | undefined)?.JobCode ?? decodeURIComponent(job.url.split("/").pop() ?? "");
	if (!jobCode) return null;
	const r = await apiGet<{ Job?: CrJob | null; IsError?: boolean; ErrorMessage?: string | null }>("GetJob", { JobCode: jobCode });
	if (r.IsError || !r.Job) return null;
	const j = r.Job;
	const loc = [j.City, j.State, j.Country].map((s) => (s ?? "").trim()).filter(Boolean).join(", ");
	return {
		content: j.Description ?? null,
		raw: j,
		location: loc || undefined,
		publishedAt: toIso(j.LastPostedOnDate) ?? undefined,
		updatedAt: toIso(j.LastResetOn) ?? undefined,
	};
}

export const crelate: AtsFetcher = {
	fetchDetail,
	async fetchJobs(slug: string): Promise<FetchResult> {
		const onv = btoa(slug);
		const cvRes = await fetchRetry(`https://jobs.crelate.com/api/candidateportal/getclientvars?onv=${encodeURIComponent(onv)}`, {
			headers: { accept: "application/json", "user-agent": UA },
		});
		if (cvRes.status === 404) return { status: "gone" };
		if (!cvRes.ok) throw new Error(`crelate ${slug}: getclientvars HTTP ${cvRes.status}`);
		const vars = (await cvRes.json()) as ClientVars;
		const orgId = vars.ORG_ID;
		if (!orgId) throw new Error(`crelate ${slug}: no ORG_ID in client vars`);
		const orgName = vars.ORG_NAME ?? slug;
		const baseUrl = vars.BASE_URL || "jobs.crelate.com";
		const version = vars.PORTAL_VERSION ? `${vars.PORTAL_VERSION}/` : "";

		const body = await apiGet<{ Jobs: CrJob[] | null; IsError?: boolean; ErrorMessage?: string | null }>("GetAllJobs", {
			OrganizationId: orgId,
			Locations: null,
			SearchText: null,
			Tags: null,
		});
		if (body.IsError || !Array.isArray(body.Jobs)) {
			if (body.ErrorMessage && /not found|disabled|unavailable/i.test(body.ErrorMessage)) return { status: "gone" };
			throw new Error(`crelate ${slug}: ${body.ErrorMessage ?? "unexpected response"}`);
		}
		const list = body.Jobs;

		const jobs: Job[] = list.map((j) => {
			const loc = [j.City, j.State, j.Country].map((s) => (s ?? "").trim()).filter(Boolean).join(", ");
			return {
				id: j.Id ?? j.JobCode,
				title: (j.Title ?? "").trim(),
				location: loc || null,
				url: `https://${baseUrl}/portal/${version}${encodeURIComponent(orgName)}/job/${encodeURIComponent(j.JobCode)}`,
				departments: (j.Tags ?? []).map((t) => t.Name).filter(Boolean),
				publishedAt: toIso(j.LastPostedOnDate),
				updatedAt: toIso(j.LastResetOn),
				content: j.Description ?? null,
				raw: j,
			};
		});
		return { status: "ok", jobs };
	},
};

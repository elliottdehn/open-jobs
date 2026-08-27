import type { Job } from "./ats/types";
import { JOB_INSTRUCTIONS, JOB_SCHEMA, JOB_SCHEMA_VERSION } from "./jobschema";
import { OPENAI_MODEL, structuredResponse } from "./openai";

/**
 * Job enrichment (FIELDS.md §2): one structured-output call per job on OPENAI_MODEL, no tools.
 * Runs inside the Board DO. Two entry points:
 *  - lazy/on-demand: Board.enrichJobs(ids) via POST /jobs/enrich (idempotent per job)
 *  - automatic: the alarm enriches `pending` jobs when the JOB_ENRICH var is "on"
 * A job is enriched at most once (`enrich_status = done`) unless forced.
 */
export interface Enricher {
	enrich(jobs: Job[], env: Env): Promise<(unknown | null)[]>;
}

/** Jobs enriched per alarm tick; remaining pending jobs are picked up by a follow-up alarm. */
export const ENRICH_BATCH = 20;
/** Concurrent OpenAI calls per Board. */
export const ENRICH_CONCURRENCY = 4;
/** JD text passed to the model is capped here (chars) to bound cost; ~12k chars ≈ 3k tokens. */
export const JD_MAX_CHARS = 12_000;

export function jdText(job: Job): string {
	return (job.content ?? "")
		.replace(/<(br|\/p|\/li|\/h\d|\/div|\/tr)[^>]*>/gi, "\n")
		.replace(/<[^>]+>/g, " ")
		.replace(/&nbsp;/g, " ")
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/[ \t]+/g, " ")
		.replace(/\s*\n\s*/g, "\n")
		.trim()
		.slice(0, JD_MAX_CHARS);
}

export interface JobEnrichment {
	schema_version: number;
	model: string;
	enriched_at: number;
	usage: { input: number; output: number };
	data: Record<string, unknown>;
}

export async function enrichOne(env: Env, job: Job, context: { company?: string | null; ats: string }): Promise<JobEnrichment> {
	const input = [
		`Company: ${context.company ?? "unknown"} (ATS: ${context.ats})`,
		`Title: ${job.title}`,
		`Location: ${job.location ?? "n/a"}`,
		`Departments: ${job.departments.join(", ") || "n/a"}`,
		`Published: ${job.publishedAt ?? "n/a"}`,
		`URL: ${job.url}`,
		``,
		`Posting text:`,
		jdText(job) || "(no description available)",
	].join("\n");
	const r = await structuredResponse<Record<string, unknown>>(env, {
		instructions: JOB_INSTRUCTIONS,
		input,
		schemaName: `job_v${JOB_SCHEMA_VERSION}`,
		schema: JOB_SCHEMA as unknown as Record<string, unknown>,
		maxOutputTokens: 2500,
	});
	return { schema_version: JOB_SCHEMA_VERSION, model: OPENAI_MODEL, enriched_at: Date.now(), usage: r.usage, data: r.data };
}

/** Enricher used by the automatic (alarm) path; per-job errors are reported as thrown by the batch. */
export const enricher: Enricher = {
	async enrich(jobs, env) {
		const out: (unknown | null)[] = new Array(jobs.length).fill(null);
		let i = 0;
		await Promise.all(
			Array.from({ length: ENRICH_CONCURRENCY }, async () => {
				for (;;) {
					const k = i++;
					if (k >= jobs.length) return;
					out[k] = await enrichOne(env, jobs[k], { ats: "" });
				}
			}),
		);
		return out;
	},
};

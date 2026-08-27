/**
 * Board-level enrichment: identify the company behind a job board (name, website, HQ, industry…).
 * See FIELDS.md §1. Free signals from the crawl are derived first and handed to the model as
 * candidates; the model may use web_search to confirm or find the homepage.
 */
import type { Job } from "./ats/types";
import { OPENAI_MODEL, structuredResponse } from "./openai";

export const COMPANY_SCHEMA_VERSION = 1;
export const COMPANY_MODEL = OPENAI_MODEL;

export const INDUSTRIES = [
	"software", "it_services", "fintech", "financial_services", "insurance", "healthcare_provider", "healthtech",
	"biotech_pharma", "medical_devices", "retail", "ecommerce", "consumer_goods", "food_beverage", "hospitality",
	"travel", "media_entertainment", "gaming", "advertising_marketing", "telecom", "hardware_semiconductors",
	"manufacturing", "automotive", "aerospace_defense", "energy_utilities", "oil_gas", "construction",
	"real_estate", "logistics_transportation", "agriculture", "education", "nonprofit", "government",
	"professional_services", "consulting", "legal", "staffing_agency", "security", "crypto_web3",
	"climate_cleantech", "other",
] as const;
export const SIZE_BUCKETS = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10000+"] as const;

export interface CompanyLLM {
	name: string;
	website: string | null;
	careers_url: string | null;
	linkedin_url: string | null;
	hq_location: { city: string | null; region: string | null; country_code: string | null } | null;
	industry: (typeof INDUSTRIES)[number];
	industry_raw: string;
	description: string;
	is_staffing_agency: boolean;
	size_bucket: (typeof SIZE_BUCKETS)[number] | null;
	confidence: number;
	evidence: string;
}

export interface CompanyEnrichment extends CompanyLLM {
	schema_version: number;
	model: string;
	resolved_at: number;
	sources: string[];
	searches: string[];
	candidate_domains: string[];
	candidate_name: string;
	usage: { input: number; output: number };
}

const nullable = (t: string) => ({ type: [t, "null"] });
export const COMPANY_SCHEMA = {
	type: "object",
	additionalProperties: false,
	required: [
		"name", "website", "careers_url", "linkedin_url", "hq_location", "industry", "industry_raw",
		"description", "is_staffing_agency", "size_bucket", "confidence", "evidence",
	],
	properties: {
		name: { type: "string", description: "Canonical brand name as the company writes it (\"Stripe\", not \"Stripe, Inc.\")." },
		website: { ...nullable("string"), description: "Company homepage, scheme + host only, e.g. https://stripe.com. null if unknown." },
		careers_url: { ...nullable("string"), description: "Company-hosted careers page if one exists, else the ATS board URL." },
		linkedin_url: { ...nullable("string"), description: "https://www.linkedin.com/company/<slug> or null." },
		hq_location: {
			anyOf: [
				{
					type: "object",
					additionalProperties: false,
					required: ["city", "region", "country_code"],
					properties: { city: nullable("string"), region: nullable("string"), country_code: { ...nullable("string"), description: "ISO 3166-1 alpha-2" } },
				},
				{ type: "null" },
			],
		},
		industry: { type: "string", enum: [...INDUSTRIES] },
		industry_raw: { type: "string", description: "Free-text industry phrase." },
		description: { type: "string", description: "One sentence, <= 200 chars, what the company does." },
		is_staffing_agency: { type: "boolean", description: "True if this board posts jobs on behalf of client companies." },
		size_bucket: { anyOf: [{ type: "string", enum: [...SIZE_BUCKETS] }, { type: "null" }] },
		confidence: { type: "number", description: "0-1 confidence that `website` is this company's homepage." },
		evidence: { type: "string", description: "One sentence naming what confirmed the website (page title, SEC filing, LinkedIn, ...)." },
	},
} as const;

/** Hosts owned by ATS vendors: a job URL on one of these says nothing about the company's own domain. */
const ATS_HOST_RE =
	/(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|recruitee\.com|personio\.(de|com)|pinpointhq\.com|breezy\.hr|comeet\.(co|com)|crelate\.com|dayforcehcm\.com|eightfold\.ai|gohire\.io|icims\.com|jobscore\.com|jobvite\.com|oraclecloud\.com|paycomonline\.net|paylocity\.com|recruiterbox\.com|trakstar\.com|taleo\.net|myworkdayjobs\.com|myworkdaysite\.com|ultipro\.com|successfactors\.(com|eu)|linkedin\.com|indeed\.com|glassdoor\.com)$/i;

export function deriveCandidates(slug: string, jobs: Job[]): { candidate_domains: string[]; candidate_name: string } {
	const counts = new Map<string, number>();
	for (const j of jobs) {
		try {
			const host = new URL(j.url).hostname.replace(/^www\./, "");
			if (!ATS_HOST_RE.test(host)) counts.set(host, (counts.get(host) ?? 0) + 1);
		} catch {
			/* ignore bad urls */
		}
	}
	const candidate_domains = [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([h]) => h).slice(0, 3);
	// Slug → readable name: strip ids / hashes / hostnames, de-kebab, title-case.
	let name = slug.split("/")[0].replace(/\.(wd\d+\.)?myworkdayjobs\.com$/i, "").replace(/\.fa\..*oraclecloud\.com$/i, "");
	name = name.replace(/[-_]+(\d{3,}|[0-9a-f]{6,})$/i, "").replace(/[-_]+/g, " ").trim();
	const candidate_name = name.replace(/\b\w/g, (c) => c.toUpperCase());
	return { candidate_domains, candidate_name };
}

const INSTRUCTIONS = `You identify the company behind a job board and return strict JSON.
Rules:
- Prefer the evidence given (candidate domains from the board's own job links, provider metadata). Use web_search to confirm the homepage or when no candidate domain is given; do not search when a candidate domain is clearly the company's own site.
- \`website\` must be the company's own homepage (scheme + host, no path). Not the ATS board, not LinkedIn, not a parent conglomerate unless the board is literally the parent's.
- If the board belongs to a staffing/recruiting agency, set is_staffing_agency=true and describe the agency itself.
- null beats a guess. Keep description to one sentence.`;

export async function resolveCompany(env: Env, ctx: { ats: string; slug: string; jobs: Job[] }): Promise<CompanyEnrichment> {
	const { candidate_domains, candidate_name } = deriveCandidates(ctx.slug, ctx.jobs);
	const sample = ctx.jobs.slice(0, 3).map((j) => {
		const text = (j.content ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 1200);
		return `- ${j.title} (${j.location ?? "location n/a"}) ${j.url}\n  ${text}`;
	});
	const input = [
		`ATS: ${ctx.ats}`,
		`Board slug: ${ctx.slug}`,
		`Candidate company name (from slug/provider): ${candidate_name}`,
		`Candidate domains (from job links, ATS hosts removed): ${candidate_domains.length ? candidate_domains.join(", ") : "none"}`,
		`Open jobs on board: ${ctx.jobs.length}`,
		`Sample postings:`,
		...sample,
	].join("\n");

	const r = await structuredResponse<CompanyLLM>(env, {
		model: COMPANY_MODEL,
		instructions: INSTRUCTIONS,
		input,
		schemaName: "company_v1",
		schema: COMPANY_SCHEMA as unknown as Record<string, unknown>,
		tools: [{ type: "web_search" }],
		maxOutputTokens: 1500,
	});
	return {
		...r.data,
		schema_version: COMPANY_SCHEMA_VERSION,
		model: COMPANY_MODEL,
		resolved_at: Date.now(),
		sources: r.sources,
		searches: r.searches,
		candidate_domains,
		candidate_name,
		usage: r.usage,
	};
}

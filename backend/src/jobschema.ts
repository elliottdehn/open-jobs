/**
 * Structured-output schema for job enrichment (FIELDS.md §2), strict mode: every property is
 * required; optional values are `T | null`. Enum lists are closed; free text goes in *_raw.
 */
export const JOB_SCHEMA_VERSION = 1;

export const ROLE_FAMILIES = [
	"software_engineering", "data", "ml_ai", "product", "design", "devops_sre", "security", "it_support", "qa",
	"sales", "marketing", "customer_success", "support", "finance", "accounting", "legal", "hr_people", "recruiting",
	"operations", "supply_chain", "manufacturing", "healthcare_clinical", "nursing", "education", "research_science",
	"construction_trades", "hospitality", "retail", "logistics_driving", "admin", "executive", "other",
] as const;
export const SENIORITY = [
	"intern", "entry", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "senior_manager",
	"director", "vp", "c_level", "unspecified",
] as const;
export const EDUCATION = ["none", "high_school", "associate", "bachelor", "master", "phd", "md_jd_other_professional"] as const;
export const WORK_ARRANGEMENT = ["onsite", "hybrid", "remote", "unspecified"] as const;
export const YES_NO_UNSPEC = ["yes", "no", "unspecified"] as const;
export const EMPLOYMENT_TYPE = ["full_time", "part_time", "contract", "temporary", "internship", "apprenticeship", "volunteer", "unspecified"] as const;
export const SALARY_PERIOD = ["hour", "day", "week", "month", "year"] as const;
export const SHIFT = ["day", "night", "rotating", "weekend", "on_call"] as const;
export const CLEARANCE = ["none", "public_trust", "secret", "top_secret", "ts_sci", "other"] as const;
export const JD_QUALITY = ["full", "partial", "stub"] as const;
export const RED_FLAGS = ["commission_only", "mlm", "pay_to_apply", "unpaid_non_internship", "vague_employer"] as const;
export const LANG_LEVEL = ["basic", "professional", "fluent", "native"] as const;

const str = { type: "string" } as const;
const nstr = { type: ["string", "null"] } as const;
const nint = { type: ["integer", "null"] } as const;
const nnum = { type: ["number", "null"] } as const;
const nbool = { type: ["boolean", "null"] } as const;
const strs = { type: "array", items: str } as const;
const en = (vals: readonly string[]) => ({ type: "string", enum: [...vals] });
const nen = (vals: readonly string[]) => ({ anyOf: [{ type: "string", enum: [...vals] }, { type: "null" }] });
const obj = (props: Record<string, unknown>) => ({ type: "object", additionalProperties: false, required: Object.keys(props), properties: props });
const nobj = (props: Record<string, unknown>) => ({ anyOf: [obj(props), { type: "null" }] });

const LOCATION = obj({ city: nstr, region: nstr, country_code: { ...nstr, description: "ISO 3166-1 alpha-2" } });

export const JOB_SCHEMA = obj({
	// 2.1 title & role
	title_normalized: { ...str, description: "Original title cleaned: strip req ids, location suffixes, '(Remote)', level codes, ALL CAPS." },
	alt_titles: { ...strs, description: "3-6 alternative titles ordered MOST specific -> LEAST specific; the last is a broad canonical title." },
	role_family: en(ROLE_FAMILIES),
	specialization: { ...nstr, description: "Free-text specialization within the family, e.g. 'payments infrastructure'." },
	seniority: en(SENIORITY),
	is_people_manager: nbool,
	summary: { ...str, description: "One sentence, <= 240 chars, what the person will actually do. Not company boilerplate." },
	responsibilities: { ...strs, description: "<= 8 short imperative phrases." },
	// 2.2 requirements
	years_experience_min: nint,
	years_experience_max: nint,
	education_min: nen(EDUCATION),
	education_required: nbool,
	skills_required: { ...strs, description: "<= 15 canonical lowercase tokens, e.g. python, kubernetes, salesforce, gaap." },
	skills_preferred: { ...strs, description: "<= 10 tokens." },
	certifications: { ...strs, description: "e.g. cpa, pmp, rn, aws-sa-pro" },
	languages: { type: "array", items: obj({ language: str, level: nen(LANG_LEVEL) }), description: "Human languages required." },
	security_clearance: nen(CLEARANCE),
	// 2.3 location & arrangement
	locations: { type: "array", items: LOCATION },
	work_arrangement: en(WORK_ARRANGEMENT),
	remote_scope: { ...nstr, description: "e.g. 'US only', 'EMEA', 'anywhere'. Only when remote/hybrid." },
	hybrid_days_onsite: nint,
	travel_pct: nint,
	visa_sponsorship: en(YES_NO_UNSPEC),
	relocation_assistance: en(YES_NO_UNSPEC),
	// 2.4 compensation & terms — ONLY when stated
	salary_min: nnum,
	salary_max: nnum,
	salary_currency: { ...nstr, description: "ISO 4217" },
	salary_period: nen(SALARY_PERIOD),
	salary_raw: { ...nstr, description: "Exact text span the salary came from." },
	equity: en(YES_NO_UNSPEC),
	bonus: en(YES_NO_UNSPEC),
	employment_type: en(EMPLOYMENT_TYPE),
	shift: nen(SHIFT),
	benefits_highlights: { ...strs, description: "<= 6 short phrases; omit boilerplate." },
	union: nbool,
	// 2.5 hiring context
	hiring_company: { ...nstr, description: "End client when the poster is a staffing agency and the client is named." },
	team: nstr,
	reports_to: nstr,
	headcount_note: nstr,
	application_deadline: { ...nstr, description: "ISO date" },
	start_date: { ...nstr, description: "ISO date" },
	language_of_posting: { ...str, description: "ISO 639-1 of the posting text." },
	// 2.6 quality
	jd_quality: en(JD_QUALITY),
	is_evergreen: nbool,
	red_flags: { type: "array", items: en(RED_FLAGS) },
	extraction_confidence: { type: "number", description: "0-1 overall confidence." },
});

export const JOB_INSTRUCTIONS = `You extract structured data from a single job posting and return strict JSON.
Hard rules:
- null beats a guess. Never infer salary, visa sponsorship, relocation, or remote policy: only fill them when the posting states them; otherwise null / "unspecified".
- alt_titles: 3-6 entries ordered from most specific to least specific; the last entry is a broad canonical title (e.g. "Software Engineer", "Registered Nurse", "Account Executive").
- summary: one sentence, <= 240 chars, describing what the person will do — not the company.
- skills_*: canonical lowercase tokens (e.g. "python", "aws", "excel", "gaap", "salesforce"); no sentences.
- locations: parse from the location field and the text; multiple allowed. country_code is ISO 3166-1 alpha-2.
- If the posting is only a title or boilerplate, set jd_quality = "stub" and leave most fields null.
- If the company posting is a staffing agency and names the end client, put it in hiring_company.
Enum values must come from the schema.`;

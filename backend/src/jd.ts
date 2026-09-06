import { structuredResponse } from "./openai";
import { usdFor } from "./pricing";

/**
 * POST /jd — expand a title, a location, and a short blurb into the *ideal* job description: the
 * posting the person would write for themselves, in the shape of a real one (AGENTS.md §1), so it
 * embeds into the same space as real JDs. Structured output, then rendered here to plain text in
 * the section order real postings use. No tools, no web search.
 */
export const JD_MODELS = {
	/** gpt-5.6-luna: $0.20 / $1.20 per 1M. Default. */
	luna: "gpt-5.6-luna",
	/** gpt-6-astra: $10 / $50 per 1M. ~40x luna; opt-in via body.model = "astra". */
	astra: "gpt-6-astra",
} as const;
export type JdModel = keyof typeof JD_MODELS;

/** Pre-call budget hold per model (USD); settled to actual cost after the response. */
export const JD_ESTIMATE_USD: Record<JdModel, number> = { luna: 0.004, astra: 0.15 };

const str = { type: "string" } as const;
const nstr = { type: ["string", "null"] } as const;
const strs = { type: "array", items: str } as const;

export const JD_SCHEMA = {
	type: "object",
	additionalProperties: false,
	required: ["title", "location", "work_arrangement", "seniority", "employment_type", "about_the_role", "what_you_will_do", "must_have", "nice_to_have", "team_and_company", "compensation", "benefits"],
	properties: {
		title: { ...str, description: "The posting title as a real company would write it. Keep the person's title if they gave one; add a team or specialty suffix only if the blurb implies it." },
		location: { ...str, description: "As a real posting states it, e.g. 'Remote (US)', 'Denver, CO (hybrid, 2 days on site)', 'Berlin'." },
		work_arrangement: { type: "string", enum: ["onsite", "hybrid", "remote"] },
		seniority: { ...str, description: "e.g. 'Senior', 'Staff', 'Mid-level', 'Director'. From the blurb; infer from the title only if the blurb is silent." },
		employment_type: { ...str, description: "e.g. 'Full-time', 'Contract'." },
		about_the_role: { ...str, description: "One or two paragraphs, 90-160 words, in the voice of the hiring team: what the team owns, why the role exists, what the person will spend most of their time on. Built from the blurb, with the person's own phrases kept." },
		what_you_will_do: { ...strs, description: "5-8 concrete responsibility bullets, each a short imperative phrase. Specific to the blurb, not boilerplate." },
		must_have: { ...strs, description: "5-8 requirement bullets. Everything the person said they want to use or be good at goes here, in their words; add only what such a role always requires." },
		nice_to_have: { ...strs, description: "3-5 bullets. Adjacent skills or experiences the person would be glad to use." },
		team_and_company: { ...str, description: "One paragraph, 60-110 words: the kind of company and team the person described (stage, size, product, culture). Never a real company name; say 'we' as the company." },
		compensation: { ...nstr, description: "A line as a posting states it (range + currency + period, plus equity/bonus if the blurb mentions them). Null unless the blurb gives a number or a clear signal." },
		benefits: { ...strs, description: "3-6 short items the person cares about, from the blurb; typical ones for the role if the blurb is silent." },
	},
} as const;

export const JD_INSTRUCTIONS = `You write the ideal job description for one job seeker: the posting they would write for the job they actually want. It must read exactly like a real posting from a real company, because it will be embedded and compared against three million real postings.

Rules:
- The blurb is the source of truth. Keep the person's own words for what they would do and what they must have. Do not add constraints, technologies, industries, or seniority the blurb does not state or clearly imply.
- Ideal means ideal: describe the job at its best (the scope, autonomy, team, and tooling the person is asking for), not a compromise and not a generic version of the title.
- Shape it like a real posting: a specific title, a location line the way postings state it, an "about the role" in the hiring team's voice, concrete responsibility bullets, must-haves, nice-to-haves, team and company, compensation only if given, benefits.
- Concrete over vague. Name systems, scale, stack, patients, clients, or materials the way a real posting for this job would. No filler ("fast-paced", "rockstar", "wear many hats").
- Never a real company name. The company is "we".
- Match the register of the field: an engineering posting, a nursing posting, and a trades posting read differently.
- 350-650 words in total across the fields.`;

export interface JdSections {
	title: string;
	location: string;
	work_arrangement: "onsite" | "hybrid" | "remote";
	seniority: string;
	employment_type: string;
	about_the_role: string;
	what_you_will_do: string[];
	must_have: string[];
	nice_to_have: string[];
	team_and_company: string;
	compensation: string | null;
	benefits: string[];
}

export interface JdResult {
	jd: string;
	sections: JdSections;
	model: string;
	usage: { input: number; output: number };
	costUsd: number;
}

/** Plain-text posting in the order real JDs use; header lines mirror Board.embedText. */
export function renderJd(s: JdSections): string {
	const bullets = (xs: string[]) => xs.map((x) => `- ${x.replace(/^[-•*]\s*/, "")}`).join("\n");
	const arrangement = s.work_arrangement === "remote" ? "Remote" : s.work_arrangement === "hybrid" ? "Hybrid" : "On site";
	const out = [
		`# ${s.title}`,
		``,
		`Location: ${s.location}`,
		`Work arrangement: ${arrangement}`,
		`Seniority: ${s.seniority}`,
		`Employment type: ${s.employment_type}`,
		s.compensation ? `Compensation: ${s.compensation}` : "",
		``,
		`About the role`,
		s.about_the_role.trim(),
		``,
		`What you'll do`,
		bullets(s.what_you_will_do),
		``,
		`What we're looking for`,
		bullets(s.must_have),
		``,
		`Nice to have`,
		bullets(s.nice_to_have),
		``,
		`About the team`,
		s.team_and_company.trim(),
		``,
		`Benefits`,
		bullets(s.benefits),
	];
	return out.filter((l, i) => l !== "" || out[i - 1] !== "").join("\n").trim() + "\n";
}

export async function expandJd(env: Env, input: { title: string; location: string; blurb: string; model: JdModel }): Promise<JdResult> {
	const model = JD_MODELS[input.model];
	const r = await structuredResponse<JdSections>(env, {
		model,
		instructions: JD_INSTRUCTIONS,
		input: [`Title: ${input.title}`, `Location: ${input.location}`, ``, `What they want, in their words:`, input.blurb].join("\n"),
		schemaName: "ideal_jd_v1",
		schema: JD_SCHEMA as unknown as Record<string, unknown>,
		// luna: extraction-style, no reasoning. astra has no "none"; "low" is its floor.
		reasoningEffort: input.model === "astra" ? "low" : "none",
		maxOutputTokens: 2500,
		store: false,
		timeoutMs: 90_000,
	});
	return { jd: renderJd(r.data), sections: r.data, model, usage: r.usage, costUsd: usdFor(model, r.usage) };
}

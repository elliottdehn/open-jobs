/**
 * Minimal OpenAI Responses API client for Structured Outputs (strict JSON schema), optionally
 * with hosted tools such as web_search. No SDK: a single fetch keeps the Worker bundle small.
 */
/** The one model used for all enrichment (board + job). */
export const OPENAI_MODEL = "gpt-5.6-luna";

export interface StructuredRequest {
	/** Defaults to OPENAI_MODEL. */
	model?: string;
	/** System-style instructions. */
	instructions: string;
	/** User content. */
	input: string;
	schemaName: string;
	/** JSON schema; strict mode requires every property in `required` and additionalProperties:false. */
	schema: Record<string, unknown>;
	tools?: { type: "web_search" }[];
	/** Defaults to "none": enrichment is extraction, not reasoning. */
	reasoningEffort?: "none" | "minimal" | "low" | "medium" | "high";
	maxOutputTokens?: number;
}

export interface StructuredResult<T> {
	data: T;
	/** URLs cited via url_citation annotations, deduped. */
	sources: string[];
	/** Queries the model issued through web_search, if any. */
	searches: string[];
	usage: { input: number; output: number };
	responseId: string;
}

export async function structuredResponse<T>(env: Env, req: StructuredRequest): Promise<StructuredResult<T>> {
	if (!env.OPENAI_KEY) throw new Error("OPENAI_KEY secret not set");
	const body: Record<string, unknown> = {
		model: req.model ?? OPENAI_MODEL,
		instructions: req.instructions,
		input: req.input,
		text: { format: { type: "json_schema", name: req.schemaName, schema: req.schema, strict: true } },
	};
	if (req.tools?.length) body.tools = req.tools;
	body.reasoning = { effort: req.reasoningEffort ?? "none" };
	if (req.maxOutputTokens) body.max_output_tokens = req.maxOutputTokens;

	const res = await fetch("https://api.openai.com/v1/responses", {
		method: "POST",
		headers: { authorization: `Bearer ${env.OPENAI_KEY}`, "content-type": "application/json" },
		body: JSON.stringify(body),
	});
	const json = (await res.json()) as {
		id: string;
		status: string;
		error: { message: string } | null;
		incomplete_details?: { reason?: string } | null;
		output: {
			type: string;
			action?: { query?: string };
			content?: { type: string; text?: string; annotations?: { type: string; url?: string }[] }[];
		}[];
		usage?: { input_tokens: number; output_tokens: number };
	};
	if (!res.ok || json.error) throw new Error(`openai ${res.status}: ${json.error?.message ?? "request failed"}`);
	if (json.status !== "completed") throw new Error(`openai response ${json.status}: ${json.incomplete_details?.reason ?? ""}`);

	const searches: string[] = [];
	const sources = new Set<string>();
	let text: string | undefined;
	for (const o of json.output) {
		if (o.type === "web_search_call" && o.action?.query) searches.push(o.action.query);
		if (o.type === "message") {
			for (const c of o.content ?? []) {
				if (c.type === "refusal") throw new Error(`openai refusal: ${c.text ?? ""}`);
				if (c.type === "output_text") {
					text = c.text;
					for (const a of c.annotations ?? []) if (a.type === "url_citation" && a.url) sources.add(a.url.replace(/[?&]utm_source=openai$/, ""));
				}
			}
		}
	}
	if (text === undefined) throw new Error("openai: no output_text in response");
	return {
		data: JSON.parse(text) as T,
		sources: [...sources],
		searches,
		usage: { input: json.usage?.input_tokens ?? 0, output: json.usage?.output_tokens ?? 0 },
		responseId: json.id,
	};
}

/** Embedding model + dimensions used for every job (FIELDS.md §3). Changing either = re-embed. */
export const EMBED_MODEL = "text-embedding-3-small";
export const EMBED_DIMS = 1536;
/** Bump when the embedded text recipe changes; boards re-embed jobs tagged with an older recipe. */
export const EMBED_RECIPE = 3;
export const EMBED_TAG = `${EMBED_MODEL}:${EMBED_DIMS}:v${EMBED_RECIPE}`;

/** Embed up to 2048 texts in one call; returns float32 vectors in input order. */
export async function embedTexts(env: Env, texts: string[], retries = 0): Promise<{ vectors: Float32Array[]; tokens: number }> {
	if (!env.OPENAI_KEY) throw new Error("OPENAI_KEY secret not set");
	let res: Response;
	for (let attempt = 0; ; attempt++) {
		res = await fetch("https://api.openai.com/v1/embeddings", {
			method: "POST",
			headers: { authorization: `Bearer ${env.OPENAI_KEY}`, "content-type": "application/json" },
			body: JSON.stringify({ model: EMBED_MODEL, input: texts, dimensions: EMBED_DIMS, encoding_format: "float" }),
			signal: AbortSignal.timeout(60_000),
		});
		if (res.status !== 429 || attempt >= retries) break;
		// Org-wide TPM cap is shared with the fleet backfill; interactive callers retry briefly.
		await res.body?.cancel();
		await new Promise((r) => setTimeout(r, 1500 * (attempt + 1) + Math.random() * 1000));
	}
	const json = (await res.json()) as { error?: { message: string }; data?: { index: number; embedding: number[] }[]; usage?: { total_tokens: number } };
	if (!res.ok || json.error || !json.data) throw new Error(`openai embeddings ${res.status}: ${json.error?.message ?? "request failed"}`);
	const vectors: Float32Array[] = new Array(texts.length);
	for (const d of json.data) vectors[d.index] = Float32Array.from(d.embedding);
	return { vectors, tokens: json.usage?.total_tokens ?? 0 };
}

/**
 * Embed free text (a résumé, a "what I want" paragraph) so it lands in job-space: same recipe as
 * Board.embedText — labelled header lines, then the body, capped like a JD.
 */
export async function embedQueryText(env: Env, text: string, hint?: { title?: string; location?: string }): Promise<Float32Array> {
	const body = text.replace(/\s+/g, " ").trim().slice(0, 28_000);
	const lines = [
		hint?.title ? `Job title: ${hint.title}` : "",
		hint?.location ? `Location: ${hint.location}` : "",
		"",
		body,
	].filter((l, i) => l !== "" || i === 2);
	const { vectors } = await embedTexts(env, [lines.join("\n")], 8);
	return vectors[0];
}

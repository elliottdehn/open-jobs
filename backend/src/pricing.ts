/** USD cost of OpenAI usage, for per-IP metering. Update when the models/prices change. */
export const PRICES = {
	// gpt-5.6-luna standard, per 1M tokens (the fleet model; /enrich, /jd default)
	input: 0.2,
	output: 1.2,
	// web_search tool, per call
	webSearch: 0.01,
};
/** Per-model token prices, per 1M tokens. Models not listed fall back to PRICES (luna). */
export const MODEL_PRICES: Record<string, { input: number; output: number }> = {
	"gpt-5.6-luna": { input: 0.2, output: 1.2 },
	"gpt-6-astra": { input: 10, output: 50 },
};
export function usd(usage: { input: number; output: number } | undefined, searches = 0): number {
	if (!usage) return 0;
	return (usage.input * PRICES.input + usage.output * PRICES.output) / 1e6 + searches * PRICES.webSearch;
}
export function usdFor(model: string, usage: { input: number; output: number } | undefined): number {
	if (!usage) return 0;
	const p = MODEL_PRICES[model] ?? PRICES;
	return (usage.input * p.input + usage.output * p.output) / 1e6;
}

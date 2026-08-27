/** USD cost of OpenAI usage, for per-IP metering. Update when the model/prices change. */
export const PRICES = {
	// gpt-5.6-luna standard, per 1M tokens
	input: 0.2,
	output: 1.2,
	// web_search tool, per call
	webSearch: 0.01,
};
export function usd(usage: { input: number; output: number } | undefined, searches = 0): number {
	if (!usage) return 0;
	return (usage.input * PRICES.input + usage.output * PRICES.output) / 1e6 + searches * PRICES.webSearch;
}

/** Shared HTTP helper for fetchers: retries 429/502/503/504 with Retry-After or exponential backoff. */
/** Per-request timeout: Workers' fetch has none, and a hung upstream would otherwise stall a DO alarm for 15 min. */
export const REQUEST_TIMEOUT_MS = 30_000;

export async function fetchRetry(url: string, init: RequestInit = {}, tries = 4): Promise<Response> {
	let res: Response;
	for (let attempt = 0; ; attempt++) {
		res = await fetch(url, { ...init, signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
		if (!(res.status === 429 || (res.status >= 502 && res.status <= 504)) || attempt >= tries - 1) return res;
		const retryAfter = Number(res.headers.get("retry-after"));
		const delay = Number.isFinite(retryAfter) && retryAfter > 0 ? Math.min(retryAfter * 1000, 60_000) : 3000 * 2 ** attempt;
		await res.body?.cancel();
		await new Promise((r) => setTimeout(r, delay));
	}
}

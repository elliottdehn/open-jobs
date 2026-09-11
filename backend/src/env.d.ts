// Secrets aren't emitted by `wrangler types`; declare them here (interface merging with the generated Env).
interface Env {
	/** `wrangler secret put USAJOBS_KEY` — data.usajobs.gov API key (public federal jobs) */
	USAJOBS_KEY?: string;
	/** `wrangler secret put ADMIN_TOKEN` — required; admin endpoints return 401 without it (fail closed). Never a var. */
	ADMIN_TOKEN?: string;
	/** `wrangler secret put OPENAI_KEY` */
	OPENAI_KEY?: string;
	/** `wrangler secret put SLACK_IDEAS_WEBHOOK` — Slack incoming webhook for the #multipenny-ideas relay (POST /ideas) */
	SLACK_IDEAS_WEBHOOK?: string;
	/** `wrangler secret put SLACK_STATS_WEBHOOK` — where the daily use line goes (falls back to SLACK_IDEAS_WEBHOOK) */
	SLACK_STATS_WEBHOOK?: string;
	/** who to @-mention on every idea: a Slack member ID (U…) or "@name" (default "@egd") */
	IDEAS_MENTION?: string;
	/** `wrangler secret put R2_ACCOUNT_ID|R2_ACCESS_KEY_ID|R2_SECRET_ACCESS_KEY` — S3 credentials the consolidation container hands to its scripts (the Worker itself uses the DATA binding) */
	R2_ACCOUNT_ID?: string;
	R2_ACCESS_KEY_ID?: string;
	R2_SECRET_ACCESS_KEY?: string;
	/** `wrangler secret put SLACK_RUN_WEBHOOK` — where the run report goes (the report falls back to the /ideas relay) */
	SLACK_RUN_WEBHOOK?: string;
	/** the consolidation container's Durable Object (wrangler.jsonc containers + durable_objects) */
	CONSOLIDATE: DurableObjectNamespace<import("./consolidate").Consolidate>;
}

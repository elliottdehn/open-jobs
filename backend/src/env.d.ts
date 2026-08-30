// Secrets aren't emitted by `wrangler types`; declare them here (interface merging with the generated Env).
interface Env {
	/** `wrangler secret put ADMIN_TOKEN` — required; admin endpoints return 401 without it (fail closed). Never a var. */
	ADMIN_TOKEN?: string;
	/** `wrangler secret put OPENAI_KEY` */
	OPENAI_KEY?: string;
	/** `wrangler secret put SLACK_IDEAS_WEBHOOK` — Slack incoming webhook for the #multipenny-ideas relay (POST /ideas) */
	SLACK_IDEAS_WEBHOOK?: string;
	/** who to @-mention on every idea: a Slack member ID (U…) or "@name" (default "@egd") */
	IDEAS_MENTION?: string;
}

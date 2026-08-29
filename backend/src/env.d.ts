// Secrets aren't emitted by `wrangler types`; declare them here (interface merging with the generated Env).
interface Env {
	/** `wrangler secret put OPENAI_KEY` */
	OPENAI_KEY?: string;
	/** `wrangler secret put SLACK_IDEAS_WEBHOOK` — Slack incoming webhook for the #multipenny-ideas relay (POST /ideas) */
	SLACK_IDEAS_WEBHOOK?: string;
}

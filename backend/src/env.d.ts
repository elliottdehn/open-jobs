// Secrets aren't emitted by `wrangler types`; declare them here (interface merging with the generated Env).
interface Env {
	/** `wrangler secret put OPENAI_KEY` */
	OPENAI_KEY?: string;
}

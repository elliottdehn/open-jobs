/**
 * The consolidation container on Cloudflare (CONTAINER.md). One Durable Object ("consolidate") controls one
 * standard-4 container that runs the nightly chain (scripts/container-chain.sh) or a single stage, with the same
 * image as the laptop. No ports: the process runs to completion and onStop records the exit. The object keeps a
 * journal (started / stopped with exit code / errors) that GET /run returns: the first half of the run tap.
 */
import { Container } from "@cloudflare/containers";

type Journal = { t: number; ev: "start" | "stop" | "error"; label?: string; exitCode?: number; reason?: string; elapsedMs?: number; message?: string };
type Current = { label: string; startedAt: number; args: string[] };

export class Consolidate extends Container<Env> {
	sleepAfter = "14h"; // a batch run has no activity in the Container sense; the process ends on its own well before this
	enableInternet = true;

	private baseEnv(): Record<string, string> {
		const e = this.env;
		return {
			ADMIN_TOKEN: e.ADMIN_TOKEN ?? "", OPENAI_KEY: e.OPENAI_KEY ?? "",
			R2_ACCOUNT_ID: e.R2_ACCOUNT_ID ?? "", R2_ACCESS_KEY_ID: e.R2_ACCESS_KEY_ID ?? "", R2_SECRET_ACCESS_KEY: e.R2_SECRET_ACCESS_KEY ?? "",
			SLACK_RUN_WEBHOOK: e.SLACK_RUN_WEBHOOK ?? "", WORKER_URL: "https://backend.dehnbostele.workers.dev",
		};
	}
	private async journal(entry: Journal): Promise<void> {
		const j = ((await this.ctx.storage.get<Journal[]>("journal")) ?? []).concat(entry).slice(-500);
		await this.ctx.storage.put("journal", j);
	}
	/** Start the process unless one is running. `args` is the full command; `extra` adds env (ESTIMATORS_ONLY, ...). */
	async run(args: string[], extra: Record<string, string>, label: string): Promise<{ started: boolean; reason?: string }> {
		const cur = await this.ctx.storage.get<Current>("current");
		const st = await this.getState();
		if (cur && (st.status === "running" || st.status === "healthy")) return { started: false, reason: `busy: ${cur.label} since ${new Date(cur.startedAt).toISOString()}` };
		await this.ctx.storage.put("current", { label, startedAt: Date.now(), args } satisfies Current);
		await this.journal({ t: Date.now(), ev: "start", label });
		await this.start({ entrypoint: args, envVars: { ...this.baseEnv(), ...extra }, enableInternet: true });
		return { started: true };
	}
	override async onStop(params: { exitCode: number; reason: string }): Promise<void> {
		const cur = await this.ctx.storage.get<Current>("current");
		await this.journal({ t: Date.now(), ev: "stop", label: cur?.label, exitCode: params.exitCode, reason: params.reason, elapsedMs: cur ? Date.now() - cur.startedAt : undefined });
		await this.ctx.storage.delete("current");
		if (params.exitCode !== 0 && cur) {
			// The chain posts its own report line; a non-zero exit without one (killed, crashed before report) still surfaces.
			const hook = this.env.SLACK_RUN_WEBHOOK; const text = `❌ cloud consolidation: ${cur.label} exited ${params.exitCode} (${params.reason}) after ${Math.round((Date.now() - cur.startedAt) / 60000)} min`;
			try { if (hook) await fetch(hook, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) }); } catch { /* best effort */ }
		}
	}
	override async onError(error: unknown): Promise<void> {
		await this.journal({ t: Date.now(), ev: "error", message: error instanceof Error ? error.message : String(error) });
	}
	async status(): Promise<{ state: unknown; current: Current | null; journal: Journal[] }> {
		return { state: await this.getState(), current: (await this.ctx.storage.get<Current>("current")) ?? null, journal: ((await this.ctx.storage.get<Journal[]>("journal")) ?? []).slice(-50) };
	}
	async halt(): Promise<void> { await this.stop("SIGTERM"); }
}

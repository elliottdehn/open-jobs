/**
 * The consolidation container on Cloudflare (CONTAINER.md). One Durable Object ("consolidate") controls one
 * standard-4 container that runs the nightly chain (scripts/container-chain.sh) or a single stage, with the same
 * image as the laptop. No ports: the process runs to completion and onStop records the exit. The object keeps a
 * journal (started / stopped with exit code / errors) that GET /run returns: the first half of the run tap.
 */
import { Container, type OutboundHandler } from "@cloudflare/containers";

type Journal = { t: number; ev: "start" | "stop" | "error" | "output"; label?: string; exitCode?: number; reason?: string; elapsedMs?: number; message?: string };
type Current = { label: string; startedAt: number; args: string[] };

/** The hostname the container uses for our own API. Containers cannot reach *.workers.dev; requests to this name run
 *  as an outbound handler in the Workers runtime and are forwarded to this Worker over the SELF service binding. */
export const WORKER_INTERNAL = "http://worker.internal";

export class Consolidate extends Container<Env> {
	sleepAfter = "14h"; // a batch run has no activity in the Container sense; the process ends on its own well before this
	enableInternet = true;

	private baseEnv(): Record<string, string> {
		const e = this.env;
		return {
			ADMIN_TOKEN: e.ADMIN_TOKEN ?? "", OPENAI_KEY: e.OPENAI_KEY ?? "",
			R2_ACCOUNT_ID: e.R2_ACCOUNT_ID ?? "", R2_ACCESS_KEY_ID: e.R2_ACCESS_KEY_ID ?? "", R2_SECRET_ACCESS_KEY: e.R2_SECRET_ACCESS_KEY ?? "",
			SLACK_RUN_WEBHOOK: e.SLACK_RUN_WEBHOOK ?? "", WORKER_URL: WORKER_INTERNAL,
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
		// Wrapped so the process's own output comes back to this object (POST /run/output) with its exit code: the platform's
		// log pipeline is not something a stage can depend on, and a failed run must carry its traceback.
		// The process runs in the background of the wrapper; its output tail is posted every two minutes while it runs
		// (code -1) and once more with the real exit code when it ends, so a three-hour chain is watchable in GET /run.
		const wrap = String.raw`post() { /usr/local/bin/python3 - "$1" <<'PY'
import sys, os, urllib.request, shutil, time
code = sys.argv[1]; data = open('/tmp/run.out', 'rb').read()[-200000:]
try:
    la = os.getloadavg(); mi = {l.split(':')[0]: int(l.split()[1]) for l in open('/proc/meminfo') if l.startswith(('MemTotal', 'MemAvailable'))}
    du = shutil.disk_usage(os.environ.get('WORK_ROOT', '/work'))
    data += ('\n[host %s] load %.1f %.1f %.1f (%d cpus) | mem %.1f of %.1f GiB free | disk %s: %.1f GB free\n' % (time.strftime('%H:%M:%S', time.gmtime()), la[0], la[1], la[2], os.cpu_count() or 0, mi.get('MemAvailable', 0) / 2**20, mi.get('MemTotal', 0) / 2**20, os.environ.get('WORK_ROOT', '/work'), du.free / 1e9)).encode()
except Exception as e: data += ('\n[host stats unavailable: %s]\n' % e).encode()
req = urllib.request.Request(os.environ['WORKER_URL'] + '/run/output?code=' + code + '&who=' + os.environ.get('RUN_OBJECT', 'consolidate'), data=data, headers={'authorization': 'Bearer ' + os.environ['ADMIN_TOKEN'], 'content-type': 'text/plain'})
try: urllib.request.urlopen(req, timeout=30)
except Exception as e: print('output post failed', e)
PY
}
: > /tmp/run.out; ( "$@" 2>&1 | tee -a /tmp/run.out; echo $PIPESTATUS > /tmp/run.code ) &
while kill -0 $! 2>/dev/null; do sleep 120; kill -0 $! 2>/dev/null && post -1; done
code=$(cat /tmp/run.code 2>/dev/null || echo 1); post "$code"; exit "$code"`;
		await this.start({ entrypoint: ["/bin/bash", "-c", wrap, "run", ...args], envVars: { ...this.baseEnv(), RUN_OBJECT: this.ctx.id.name ?? "consolidate", ...extra }, enableInternet: true });
		return { started: true };
	}
	/** The process's captured output (last 200 KB) and exit code, posted by the wrapper above. */
	async output(code: number, text: string): Promise<void> {
		await this.ctx.storage.put("lastOutput", { t: Date.now(), code, text });
		if (code === -1) return;  // an interim tail while the process runs: kept in lastOutput, not in the journal
		const tail = text.trim().split("\n").slice(-3).join(" | ");
		await this.journal({ t: Date.now(), ev: "output" as Journal["ev"], exitCode: code, message: tail.slice(0, 300) });
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
	async status(): Promise<{ state: unknown; current: Current | null; journal: Journal[]; lastOutput: { t: number; code: number; text: string } | null }> {
		const lo = (await this.ctx.storage.get<{ t: number; code: number; text: string }>("lastOutput")) ?? null;
		return { state: await this.getState(), current: (await this.ctx.storage.get<Current>("current")) ?? null, journal: ((await this.ctx.storage.get<Journal[]>("journal")) ?? []).slice(-50), lastOutput: lo && { ...lo, text: lo.text.slice(-20000) } };
	}
	async halt(signal: "SIGTERM" | "SIGKILL" = "SIGTERM"): Promise<void> { await this.stop(signal); }
}

// The documented form: assigned through the base class's static setter (a static field on the subclass shadows the accessor).
Consolidate.outboundByHost = {
	"worker.internal": (async (req: Request, env: unknown) => {
		const u = new URL(req.url); u.protocol = "https:"; u.host = "backend.dehnbostele.workers.dev";
		return (env as Env).SELF.fetch(new Request(u.toString(), req));
	}) as OutboundHandler,
};

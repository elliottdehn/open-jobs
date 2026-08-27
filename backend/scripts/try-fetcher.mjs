// Quick local harness: node scripts/try-fetcher.mjs <ats> <slug> [<slug>...]
// Runs the fetcher directly in Node (no wrangler) and prints a summary.
const [ats, ...slugs] = process.argv.slice(2);
const mod = await import(`../src/ats/${ats}.ts`);
const fetcher = mod[ats] ?? Object.values(mod)[0];
for (const slug of slugs) {
	const t = Date.now();
	try {
		const r = await fetcher.fetchJobs(slug);
		if (r.status === "gone") console.log(`${slug}: gone (${Date.now() - t}ms)`);
		else {
			const j = r.jobs[0];
			console.log(`${slug}: ${r.jobs.length} jobs (${Date.now() - t}ms)`, j && { ...j, raw: undefined, content: j.content?.slice(0, 60) });
		}
	} catch (e) {
		console.log(`${slug}: ERROR ${e.message}`);
	}
}

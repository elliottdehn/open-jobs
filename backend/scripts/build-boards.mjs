// Generates src/boards.json ({ ats: slug[] }) from ../slugs.json (schema 2: {ats, gone}; see scripts/build-slugs.py).
// Default: live + gone boards, i.e. the fleet exactly as it runs. --live-only leaves the dead boards out (a fresh deploy).
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = JSON.parse(readFileSync(join(here, "../../slugs.json"), "utf8"));
const liveOnly = process.argv.includes("--live-only");
const out = {};
const merged = {};
for (const [ats, slugs] of Object.entries(src.ats)) merged[ats] = [...slugs];
if (!liveOnly) for (const [ats, slugs] of Object.entries(src.gone ?? {})) merged[ats] = [...(merged[ats] ?? []), ...slugs];
for (const [ats, slugs] of Object.entries(merged)) {
	let list = [...new Set(slugs)];
	if (ats === "dayforce") {
		// `x` and `x/CANDIDATEPORTAL` are the same board (the fetcher defaults bare slugs to CANDIDATEPORTAL).
		const bare = new Set(list.filter((s) => !s.includes("/")));
		list = list.filter((s) => !(s.endsWith("/CANDIDATEPORTAL") && bare.has(s.slice(0, -"/CANDIDATEPORTAL".length))));
	}
	out[ats] = list.sort();
}
writeFileSync(join(here, "../src/boards.json"), JSON.stringify(out));
console.log(Object.entries(out).map(([k, v]) => `${k}: ${v.length}`).join("\n"));

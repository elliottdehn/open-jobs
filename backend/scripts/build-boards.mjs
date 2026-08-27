// Generates src/boards.json ({ ats: slug[] }) from ../slugs.json, stripping crawl metadata.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = JSON.parse(readFileSync(join(here, "../../slugs.json"), "utf8"));
const out = {};
for (const [ats, slugs] of Object.entries(src.ats)) {
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

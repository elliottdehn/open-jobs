import type { AtsFetcher, FetchResult, Job } from "./types";

const UA = "open-jobs/0.1";

function decodeEntities(s: string): string {
	return s
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&apos;/g, "'")
		.replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
		.replace(/&amp;/g, "&");
}

/** Text content of the first <tag> child (CDATA unwrapped, entities decoded). */
function tag(xml: string, name: string): string | null {
	const m = new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`).exec(xml);
	if (!m) return null;
	let v = m[1].trim();
	const cd = /^<!\[CDATA\[([\s\S]*?)\]\]>$/.exec(v);
	v = cd ? cd[1].trim() : decodeEntities(v);
	return v === "" ? null : v;
}

function tags(xml: string, name: string): string[] {
	const out: string[] = [];
	const re = new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "g");
	let m: RegExpExecArray | null;
	while ((m = re.exec(xml))) {
		const v = tag(m[0], name);
		if (v) out.push(v);
	}
	return out;
}

function toIso(s: string | null): string | null {
	if (!s) return null;
	const d = new Date(s);
	return isNaN(d.getTime()) ? null : d.toISOString();
}

export const personio: AtsFetcher = {
	async fetchJobs(slug: string): Promise<FetchResult> {
		const host = slug.includes(".") ? slug : `${slug}.jobs.personio.de`;
		// Single XML feed with every published position; no pagination.
		const res = await fetch(`https://${host}/xml`, { redirect: "manual", headers: { "user-agent": UA } });
		if (res.status === 404 || res.status === 410) return { status: "gone" };
		if (res.status >= 300 && res.status < 400) {
			// Unknown subdomains 307 to https://personio.com/
			const loc = res.headers.get("location") ?? "";
			if (/personio\.(com|de)\/?$/.test(loc) || loc === "") return { status: "gone" };
			throw new Error(`personio ${slug}: unexpected redirect to ${loc}`);
		}
		if (!res.ok) throw new Error(`personio ${slug}: HTTP ${res.status}`);
		const xml = await res.text();
		if (!/<workzag-jobs/.test(xml)) throw new Error(`personio ${slug}: unexpected response (not a workzag-jobs feed)`);

		const jobs: Job[] = [];
		const re = /<position>([\s\S]*?)<\/position>/g;
		let m: RegExpExecArray | null;
		while ((m = re.exec(xml))) {
			const p = m[1];
			const id = tag(p, "id");
			const title = tag(p, "name");
			if (!id || !title) continue;
			const office = tag(p, "office");
			const extra = (() => {
				const block = /<additionalOffices>([\s\S]*?)<\/additionalOffices>/.exec(p);
				return block ? tags(block[1], "office") : [];
			})();
			const offices = [office, ...extra].filter((x): x is string => !!x);
			const dept = tag(p, "department");
			const descBlock = /<jobDescriptions>([\s\S]*?)<\/jobDescriptions>/.exec(p)?.[1] ?? "";
			const sections: string[] = [];
			const dre = /<jobDescription>([\s\S]*?)<\/jobDescription>/g;
			let dm: RegExpExecArray | null;
			while ((dm = dre.exec(descBlock))) {
				const n = tag(dm[1], "name");
				const v = tag(dm[1], "value");
				if (v) sections.push(n ? `<h3>${n}</h3>\n${v}` : v);
			}
			const raw: Record<string, unknown> = {};
			for (const k of [
				"id", "subcompany", "office", "department", "recruitingCategory", "name", "employmentType",
				"seniority", "schedule", "yearsOfExperience", "keywords", "occupation", "occupationCategory", "createdAt",
			]) raw[k] = tag(p, k);
			raw.additionalOffices = extra;
			raw.jobDescriptions = sections;
			jobs.push({
				id,
				title,
				location: offices.length ? offices.join("; ") : null,
				url: `https://${host}/job/${id}`,
				departments: dept ? [dept] : [],
				publishedAt: toIso(tag(p, "createdAt")),
				updatedAt: null,
				content: sections.length ? sections.join("\n") : null,
				raw,
			});
		}
		return { status: "ok", jobs };
	},
};

// "Why isn't this posting in my list?" — resolve a job URL to (ats, slug, id-or-url) so /probe can ask
// the board's Durable Object about it. Slugs are matched against slugs.json where the URL doesn't
// carry them verbatim. Unresolvable boards return slug=null with a hint.
import { slugsFor } from "./ats";

export interface Resolved {
	ats: string | null;
	slug: string | null;
	/** Provider job id when the URL carries it; otherwise the job is matched by URL. */
	id: string | null;
	hint?: string;
}

const norm = (s: string) => s.toLowerCase().replace(/\/+$/, "");

export function resolveJobUrl(raw: string): Resolved {
	let u: URL;
	try { u = new URL(raw.trim()); } catch { return { ats: null, slug: null, id: null, hint: "not a URL" }; }
	const host = u.hostname.toLowerCase(), path = u.pathname, seg = path.split("/").filter(Boolean);
	const sub = host.split(".")[0];
	const pick = (ats: string, cands: (s: string) => boolean, id: string | null, hint?: string): Resolved => {
		const hits = slugsFor(ats).filter(cands);
		return { ats, slug: hits.length === 1 ? hits[0] : null, id, hint: hits.length === 1 ? undefined : hits.length ? `ambiguous board (${hits.slice(0, 3).join(", ")}…)` : hint ?? "board not in slugs.json" };
	};
	const exact = (ats: string, slug: string, id: string | null): Resolved =>
		slugsFor(ats).includes(slug) ? { ats, slug, id } : slugsFor(ats).map(norm).includes(norm(slug)) ? { ats, slug: slugsFor(ats).find((s) => norm(s) === norm(slug))!, id } : { ats, slug: null, id, hint: `board ${ats}/${slug} not in slugs.json` };

	if (/greenhouse\.io$/.test(host) && seg.length >= 3 && seg[1] === "jobs") return exact("greenhouse", seg[0], seg[2]);
	if (host === "jobs.lever.co" && seg.length >= 2) return exact("lever", seg[0], seg[1]);
	if (host === "jobs.ashbyhq.com" && seg.length >= 2) return exact("ashby", seg[0], seg[1]);
	if (host === "jobs.smartrecruiters.com" && seg.length >= 2) return exact("smartrecruiters", seg[0], seg[1]);
	if (/myworkdayjobs\.com$/.test(host)) { const i = seg.indexOf("job"); return exact("workday", host, i >= 0 ? "/" + seg.slice(i).join("/") : null); }
	if (/oraclecloud\.com$/.test(host)) { const i = seg.indexOf("job"); return exact("oraclecloud", host, i >= 0 ? seg[i + 1] ?? null : null); }
	if (/taleo\.net$/.test(host)) return exact("taleo", sub, u.searchParams.get("job"));
	if (/\.jobs\.personio\.(de|com)$/.test(host)) return exact("personio", sub, seg[0] === "job" ? seg[1] ?? null : null);
	if (/recruitee\.com$/.test(host)) return exact("recruitee", sub, null);
	if (/breezy\.hr$/.test(host)) return exact("breezy", sub, seg[0] === "p" && seg[1] ? seg[1].split("-")[0] : null);
	if (/eightfold\.ai$/.test(host)) { const i = seg.indexOf("job"); return exact("eightfold", sub, i >= 0 ? seg[i + 1] ?? null : null); }
	if (host === "jobs.jobvite.com" && seg.length >= 3) return exact("jobvite", seg[0], seg[2]);
	if (host === "jobs.dayforcehcm.com") { const i = seg.findIndex((s) => s.toUpperCase() === "CANDIDATEPORTAL"); return exact("dayforce", seg[i - 1] ?? "", seg[i + 2] ?? null); }
	if (host === "www.paycomonline.net") { const i = seg.indexOf("portal"); return exact("paycom", seg[i + 1] ?? "", seg[i + 3] ?? null); }
	if (host === "careers.jobscore.com" && seg[0] === "careers") return exact("jobscore", seg[1], null);
	if (/pinpointhq\.com$/.test(host)) return exact("pinpoint", sub, null);
	if (/hire\.trakstar\.com$/.test(host)) return exact("recruiterbox", sub, seg[0] === "jobs" ? seg[1] ?? null : null);
	if (host === "jobs.crelate.com" && seg[0] === "portal") return exact("crelate", seg[1], null);
	if (host === "jobs.gohire.io" && seg[0]) { const prefix = seg[0].replace(/-[^-]+$/, "-"); const id = /-(\d+)\/?$/.exec(seg[1] ?? "")?.[1] ?? null; return pick("gohire", (s) => s.startsWith(prefix), id); }
	if (/icims\.com$/.test(host)) { const id = seg[0] === "jobs" ? seg[1] ?? null : null; return pick("icims", (s) => s === sub || s.endsWith(sub) || sub.endsWith(s), id); }
	if (host === "recruiting.paylocity.com") { const i = seg.indexOf("Details"); return { ats: "paylocity", slug: null, id: seg[i + 1] ?? null, hint: "paylocity boards are keyed by a GUID that isn't in the job URL; pass --board paylocity/<guid>" }; }
	if (host === "apply.workable.com") return { ats: "workable", slug: null, id: seg[0] === "j" ? seg[1] ?? null : null, hint: "workable job URLs don't name the company; pass --board workable/<slug>" };
	// Greenhouse embedded on a company site: https://acme.com/careers?gh_jid=123 — the board slug isn't in the URL
	const gh = u.searchParams.get("gh_jid");
	if (gh) return { ats: "greenhouse", slug: null, id: gh, hint: "embedded greenhouse board (gh_jid); pass --board greenhouse/<slug>" };
	return { ats: null, slug: null, id: null, hint: "unrecognized ATS host" };
}

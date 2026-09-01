import type { AtsFetcher } from "./types";
import boards from "../boards.json";
import { ashby } from "./ashby";
import { breezy } from "./breezy";
import { comeet } from "./comeet";
import { crelate } from "./crelate";
import { dayforce } from "./dayforce";
import { eightfold } from "./eightfold";
import { gohire } from "./gohire";
import { greenhouse } from "./greenhouse";
import { icims } from "./icims";
import { jobscore } from "./jobscore";
import { join } from "./join";
import { jobvite } from "./jobvite";
import { lever } from "./lever";
import { oraclecloud } from "./oraclecloud";
import { paycom } from "./paycom";
import { paylocity } from "./paylocity";
import { personio } from "./personio";
import { pinpoint } from "./pinpoint";
import { recruitee } from "./recruitee";
import { recruiterbox } from "./recruiterbox";
import { smartrecruiters } from "./smartrecruiters";
import { softgarden } from "./softgarden";
import { taleo } from "./taleo";
import { teamtailor } from "./teamtailor";
import { workable } from "./workable";
import { workday } from "./workday";
import { phenom } from "./phenom";
import { jibe } from "./jibe";
import { snowflake } from "../snowflake";

/**
 * Providers with a working fetcher. Boards for other ATSes are ignored (no DOs are created).
 * Not enabled: `ukg` and `successfactors` — their slugs are datacenter/CDN hostnames with no
 * company identifier, so there is nothing to fetch (see src/ats/ukg.ts, successfactors.ts).
 * `comeet` is disabled until its slug->uid map is built (scripts/build-comeet.mjs --via=<worker-url>).
 */
export const fetchers: Record<string, AtsFetcher> = {
	ashby,
	breezy,
	// comeet, // disabled: slug->uid map (src/comeet-uids.json) not built yet; see scripts/build-comeet.mjs
	crelate,
	dayforce,
	eightfold,
	gohire,
	greenhouse,
	jibe,
	icims,
	jobscore,
	join,
	jobvite,
	lever,
	oraclecloud,
	paycom,
	paylocity,
	personio,
	phenom,
	pinpoint,
	recruitee,
	recruiterbox,
	smartrecruiters,
	snowflake,
	softgarden,
	taleo,
	teamtailor,
	workable,
	workday,
};

/**
 * Providers whose fetcher works but which block Cloudflare's IP ranges (403 from Workers, 200 from
 * a laptop). They are excluded from the Worker fleet and fetched locally by scripts/fetch-local.mjs
 * during consolidation instead.
 */
export const localOnlyAts = ["jobscore"];

export const enabledAts = Object.keys(fetchers).filter((a) => !localOnlyAts.includes(a));

export function slugsFor(ats: string): string[] {
	return (boards as Record<string, string[]>)[ats] ?? [];
}

export function boardName(ats: string, slug: string): string {
	return `${ats}/${slug}`;
}

export function parseBoardName(name: string): { ats: string; slug: string } {
	const i = name.indexOf("/");
	if (i < 0) throw new Error(`bad board name: ${name}`);
	return { ats: name.slice(0, i), slug: name.slice(i + 1) };
}

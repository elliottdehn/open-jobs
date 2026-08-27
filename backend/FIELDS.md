# FIELDS.md — enrichment field spec

What we extract from crawled job data with the OpenAI **Responses API + Structured Outputs**
(`text.format = { type: "json_schema", strict: true }`), and where each field comes from.
Two levels: **board** (company; one-shot per board, may use `web_search`) and **job** (one-shot
per job, no tools, cheap model, Batch API where possible).

Conventions
- Every LLM field has a sibling `confidence` (0–1) where the value is inferred, and `null` is
  allowed wherever the JD genuinely doesn't say. Never guess salary, remote policy, or visa.
- Schemas are `strict: true` → every property is `required`; optional values are `T | null`.
- Enums are closed sets; free-text goes in `*_raw` fields so we can re-map later without re-running.
- Derived (non-LLM) fields are computed in code from the crawl and are always present.
- Nothing is re-extracted once `done` (see DOCS.md). A `schema_version` on each payload lets a
  future field be backfilled by only re-running jobs below that version.

---

## 1. Board / company (`Board.meta.company`)

Resolved once per board (64k boards, not per job). Inputs: `ats`, `slug`, job URL hostnames,
2–3 job titles + first ~1,500 chars of JD text, provider metadata when the fetcher exposes a
company name.

### Derived first (free, no LLM)
| Field | Source |
|---|---|
| `candidate_domains[]` | hostnames of job `url`s minus known ATS domains (custom careers domains: `stripe.com`, `careers.acme.com`) |
| `candidate_name` | provider metadata (ashby `organizationName`, smartrecruiters `company.identifier`, comeet company, workday tenant) else slug de-kebabed |

### LLM (Responses API, `web_search` tool allowed, `gpt-5.6-luna`)
| Field | Type | Notes |
|---|---|---|
| `name` | string | canonical legal/brand name as the company writes it ("Stripe", not "Stripe, Inc.") |
| `website` | string \| null | homepage URL, scheme + host only (`https://stripe.com`) |
| `careers_url` | string \| null | company-hosted careers page if it exists, else the ATS board URL |
| `linkedin_url` | string \| null | `linkedin.com/company/...` |
| `hq_location` | `{city, region, country_code}` \| null | ISO-3166-1 alpha-2 country |
| `industry` | enum | closed list (~40: software, fintech, healthcare, biotech, retail, manufacturing, staffing_agency, …) |
| `industry_raw` | string | model's free-text phrase |
| `description` | string | one sentence, ≤ 200 chars, what the company does |
| `is_staffing_agency` | bool | recruiters posting on behalf of clients — affects how job-level `company` is read |
| `size_bucket` | enum \| null | `1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5001-10000, 10000+` |
| `confidence` | 0–1 | for the `website` identification |
| `evidence` | string | one sentence + the URL(s) it relied on |
| `sources[]` | string[] | citation URLs from `web_search` |

Stored with `{ schema_version, model, resolved_at, source: "derived" | "llm" }`. Cached forever
unless forced (`POST /boards/:ats/:slug/enrich-board?force=1`).

---

## 2. Job (`jobs.enrichment`)

Inputs: `title`, `location`, `departments`, `content` (HTML → text, capped ~12k chars), plus
board `company.name` / `is_staffing_agency` as context. No tools. `gpt-5.6-luna` via Batch API.

### 2.1 Title & role
| Field | Type | Notes |
|---|---|---|
| `title_normalized` | string | cleaned original: strip req IDs, location suffixes, "(Remote)", level codes, all-caps |
| `alt_titles[]` | string[3–6] | **most specific → least specific**, e.g. `["Staff Backend Engineer, Payments", "Staff Backend Engineer", "Backend Engineer", "Software Engineer"]`. Last item should be a broad canonical title. |
| `role_family` | enum | ~30 buckets: `software_engineering, data, ml_ai, product, design, devops_sre, security, it_support, qa, sales, marketing, customer_success, support, finance, accounting, legal, hr_people, recruiting, operations, supply_chain, manufacturing, healthcare_clinical, nursing, education, research_science, construction_trades, hospitality, retail, logistics_driving, admin, executive, other` |
| `specialization` | string \| null | free text within family: "payments infrastructure", "pediatric nursing" |
| `seniority` | enum | `intern, entry, junior, mid, senior, staff, principal, lead, manager, senior_manager, director, vp, c_level, unspecified` |
| `is_people_manager` | bool \| null | manages direct reports |
| `summary` | string | **one sentence**, ≤ 240 chars, what the person will actually do — not company boilerplate |
| `responsibilities[]` | string[≤8] | short imperative phrases |

### 2.2 Requirements
| Field | Type | Notes |
|---|---|---|
| `years_experience_min` | int \| null | only if stated |
| `years_experience_max` | int \| null | |
| `education_min` | enum \| null | `none, high_school, associate, bachelor, master, phd, md_jd_other_professional` |
| `education_required` | bool \| null | required vs preferred |
| `skills_required[]` | string[≤15] | canonical lowercase tokens: `python`, `kubernetes`, `salesforce`, `gaap` |
| `skills_preferred[]` | string[≤10] | |
| `certifications[]` | string[] | `cpa, pmp, rn, aws-sa-pro` |
| `languages[]` | `{language, level}`[] | human languages, level enum `basic, professional, fluent, native` \| null |
| `security_clearance` | enum \| null | `none, public_trust, secret, top_secret, ts_sci, other` |

### 2.3 Location & work arrangement
| Field | Type | Notes |
|---|---|---|
| `locations[]` | `{city, region, country_code}`[] | parsed from `location` + JD; multiple allowed |
| `work_arrangement` | enum | `onsite, hybrid, remote, unspecified` |
| `remote_scope` | string \| null | "US only", "EMEA", "anywhere" — only when remote/hybrid |
| `hybrid_days_onsite` | int \| null | |
| `travel_pct` | int \| null | |
| `visa_sponsorship` | enum | `yes, no, unspecified` |
| `relocation_assistance` | enum | `yes, no, unspecified` |

### 2.4 Compensation & terms
| Field | Type | Notes |
|---|---|---|
| `salary_min` / `salary_max` | number \| null | **only when stated in the JD** (pay-transparency ranges). Never inferred. |
| `salary_currency` | ISO-4217 \| null | |
| `salary_period` | enum \| null | `hour, day, week, month, year` |
| `salary_raw` | string \| null | the exact text span |
| `equity` | enum | `yes, no, unspecified` |
| `bonus` | enum | `yes, no, unspecified` |
| `employment_type` | enum | `full_time, part_time, contract, temporary, internship, apprenticeship, volunteer, unspecified` |
| `shift` | enum \| null | `day, night, rotating, weekend, on_call` — mostly for clinical/industrial |
| `benefits_highlights[]` | string[≤6] | short phrases; omit boilerplate |
| `union` | bool \| null | |

### 2.5 Hiring context
| Field | Type | Notes |
|---|---|---|
| `hiring_company` | string \| null | when the poster is a staffing agency and the end client is named |
| `team` | string \| null | team/org name if stated |
| `reports_to` | string \| null | |
| `headcount_note` | string \| null | "multiple openings", "backfill" |
| `application_deadline` | ISO date \| null | |
| `start_date` | ISO date \| null | |
| `language_of_posting` | ISO-639-1 | from the JD text |

### 2.6 Quality flags
| Field | Type | Notes |
|---|---|---|
| `jd_quality` | enum | `full, partial, stub` — stub = title only / boilerplate |
| `is_evergreen` | bool \| null | "always accepting applications" style posting |
| `red_flags[]` | enum[] | `commission_only, mlm, pay_to_apply, unpaid_non_internship, vague_employer` |
| `extraction_confidence` | 0–1 | overall |

### 2.7 Derived (code, not LLM)
| Field | Source |
|---|---|
| `content_text` | HTML-stripped JD, whitespace-normalized (input to the model; keep for search) |
| `content_lang_hint` | cheap detector before the call (lets us route non-English) |
| `posted_age_days` | from `publishedAt` at extraction time |
| `salary_min_usd` / `salary_max_usd` / `salary_annualized_usd` | FX + period normalization, computed from the LLM fields |
| `location_geo[]` | geocoded `locations[]` (lat/lon, admin codes) via a gazetteer, not the LLM |
| `title_hash` | for dedup across boards (`alt_titles[-1] + company + city`) |

---

## 3. Embeddings (separate step, `text-embedding-3-small` or `-large`)
| Vector | Text | Purpose |
|---|---|---|
| `emb_job` | `title_normalized + summary + responsibilities + skills` | job ↔ job similarity, "roles like this" |
| `emb_query` | same model | user queries; stored nowhere |
| `emb_company` | `company.name + description + industry` | company clustering |

Embeddings live outside the DO (parquet / vector store); the DO only stores the structured payload.

---

## 4. Schema mechanics
- One JSON schema file per level: `schemas/job.v1.json`, `schemas/company.v1.json`
  (also imported by the Worker for `strict: true` calls). `schema_version` embedded in the payload.
- Prompt includes: the field table's *Notes* column verbatim as instructions, the enum lists,
  and the hard rules (never infer salary/visa/remote; `null` beats a guess).
- Model: `gpt-5.6-luna` for everything (`OPENAI_MODEL` in `src/openai.ts`). Cost control via the
  Batch API (~50% off) for job extraction.
- Validation after the call: enums checked against the closed lists, salary sanity
  (`min ≤ max`, plausible range for period), country codes valid. Failures → `enrich_status = error`.

## Open questions
- Which enum lists to lock first (`role_family`, `industry`) — everything else re-maps from `*_raw`.
- Do we want `alt_titles` capped at 4 or allowed up to 6?
- Should `summary` be written for a job seeker (2nd person) or neutral (3rd person)?
- Multi-location jobs: one row per location in parquet, or keep the array?

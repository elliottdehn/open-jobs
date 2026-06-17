# AGENTS.md — How to find a job with this dataset

You are an agent helping a person find a job. This file tells you exactly how to use the
**open-jobs** dataset to do that well. Read it fully before you start.

The dataset is a single Parquet file at **https://download.jobscream.com/open-jobs.parquet**,
refreshed in place daily. Grab it with `python3 download.py` (resumable), or read it straight from
the URL with `stream.py`. It is a deduplicated snapshot of every currently-open role pulled from
16 applicant tracking systems (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable,
Breezy, Personio, Paylocity, Dayforce, Recruitee, Pinpoint, Recruiterbox, JobScore, Crelate,
Eightfold). Roughly a million rows. Each row carries metadata, the full job description as
Markdown, precomputed embeddings, and ~34 structured fields extracted from the JD.

It is a snapshot, not a history. Yesterday's file is gone. Work with what is here now.

## How you should work

Write **small Python scripts** to filter and rank, and produce a **self-contained HTML file** the
person opens in their browser to explore the results. No database, no server, no special tooling.
Everything here runs with:

```
pip install pandas pyarrow numpy openai
```

The canonical pipeline is **hull -> learn -> rank** (section 4): draw the convex hull of eligible
roles, gather the LLM's pairwise judgments inside it, aggregate those into a ranking, then write an
HTML report (section 5). Keep the scripts readable so the person can rerun and tweak them.

---

## 1. Load it

```python
import pandas as pd, numpy as np
# either download first (python3 download.py) then read the local file,
# or read straight from the URL:
df = pd.read_parquet("https://download.jobscream.com/open-jobs.parquet")
df = df[df["jd_markdown"].str.len() > 0]      # drop the few rows with no description
print(len(df), "jobs")
```

The two embedding columns come back as arrays of 1536 floats. Do NOT stack all million into one
matrix (that is ~6 GB). You filter first, then build the embedding matrix only on the survivors
(section 4).

---

## 2. Schema

**Identity & apply**
`id, ats, company, url` — `url` is where the person applies. Always surface it.

**Content**
`title, jd_markdown` — the full description, cleaned to Markdown.

**Where & when**
`location, posted_at, remote` (bool), plus structured geography below.

**Embeddings** (OpenAI `text-embedding-3-small`, 1536-dim, float32) — see section 4.
- `title_embedding`, `jd_embedding`: one vector each (the posted title; the JD). JD similarity is the
  strongest signal for "what is this role."
- `alt_titles_embedding`: a **list of vectors**, one per entry in `alt_titles` (aligned 1:1), NOT a
  single vector. Each alt title was embedded separately so a query matches the closest *variant*, not
  a blurred average. Match a title query by taking the **max** cosine over a row's alt vectors.

**Structured facets** (extracted by an LLM from each JD; use for exact filtering)

- Role: `level` (Intern…C-Level/Unknown), `function` (engineering, data, design, product, sales,
  marketing, ops, security, finance, hr, legal, research, support, healthcare, education,
  skilled-trade, other), `sub_function`, `role_summary`, `employment_type`,
  `years_experience_min`, `years_experience_max`, `education_min`, `management` (bool),
  `key_responsibilities` (list)
- Titles: `alt_titles` (list, MOST specific first then broadening — your lexical recall handle)
- Location & eligibility: `is_remote` (bool), `work_mode` (fully_remote/remote_first/hybrid/
  onsite/unknown), `city`, `region`, `country_code` (ISO alpha-2), `country_required` (bool),
  `remote_scope` (e.g. us-only, us-canada, emea, latam, apac, global), `relocation`,
  `timezone_requirement`, `travel_required`
- Comp & legal: `salary_min_k`, `salary_max_k` (thousands), `salary_currency` (ISO 4217),
  `equity` (bool), `visa_sponsorship` (yes/no/unknown), `security_clearance` (bool)
- Company: `company_does`, `industry`, `company_stage`
- Skills & tags: `skills` (required/core), `nice_to_have`, `tags`

**Unknown sentinels.** Do not mistake these for real values:
`""` (string), `"unknown"` (enum), `-1` (number), `[]` (list), `false` (boolean).
Critically: `salary_min_k = -1` means "not stated," not "free." Filter accordingly.

---

## 3. First, build the candidate profile

Before querying, extract these from the person's resume and what they tell you. Ask if unclear.

- target `function` and `level` (and acceptable adjacent levels)
- core `skills` they have, and which they want to use
- location: which `country_code`(s) they can legally work in, and whether they need remote
- if remote: which `remote_scope` works for them (a `us-only` role is useless to an EU resident)
- comp floor (in a currency), and whether `equity` matters
- `visa_sponsorship` need (true/false)
- deal-breakers: `employment_type`, `security_clearance`, `travel_required`, relocation

Write this down explicitly. Every step below references it.

---

## 4. The search: hull -> learn -> rank

Three small tools ship alongside this file and run as a pipeline. The philosophy: use cheap exact
filters to draw the smallest set that still CONTAINS every relevant role (the "convex hull"), then
spend LLM judgment only inside it, comparing roles head-to-head and aggregating the comparisons into
a ranking.

Why pairwise, not 0-100 scoring: asking a model "how good is this role, 0-100?" gives mushy, bunched
numbers (everything lands near 85). Asking "which of these two fits better?" is a far easier and
better-calibrated call. So we collect pairwise judgments and let the math turn them into an order.

```
python3 hull.py     --function engineering --level Senior,Staff --country US --remote \
                    --title "software engineer,backend,platform" --out hull.json
python3 langsort.py --resume resume.txt --candidates hull.json --mode sample --per-item 12
python3 btrank.py   --candidates hull.json --decisions langsort_decisions.jsonl \
                    --parquet open-jobs.parquet --out ranked.json
```

### Step 1 — the convex hull (hull.py)

Filter ONLY on hard eligibility (function, level, location, work authorization, comp floor) and broad
role recall. Never filter on soft fit. Fit is what steps 2-3 decide; excluding on it here silently
drops relevant roles. The hull should be as specific as possible while still containing everything
relevant: too loose wastes comparisons, too tight loses real matches.

- Eligibility is binary: country, remote scope, level, sponsorship. An EU resident drops a `us-only`
  role; someone needing sponsorship adds `--require-visa`.
- Recall is generous: `--title` matches the posted `title` OR any `alt_titles` entry (lowercase
  substring). People name a role differently than the posting does. Measured on this corpus for
  "software engineer", `alt_titles` alone finds **+56%** more roles than the literal title, yet still
  **misses ~24%** of jobs whose title contains it, so hull.py unions both. Pass several phrasings.
- Respect unknown sentinels: `--min-comp` keeps `salary_max_k = -1` (not stated) rows. Unknown is not
  "low."

hull.py streams the parquet on structured fields only (no embeddings), so it is fast and memory-light
even over ~1M rows, and dedups cross-posted roles by (company, title). If it comes back empty, loosen
a filter with the person; the hull must contain every relevant role.

### Step 2 — learn the LLM's discrimination (langsort.py)

`--mode sample` gathers pairwise judgments toward `--per-item` comparisons per role. Each call asks one
question, "which fits the resume better, A or B?", capped to a tiny decision-only output, and appends
the verdict to `langsort_decisions.jsonl` (the product of this step). It is memoized and replayed on
restart, so a killed run resumes without losing or repeating a decision.

By default it **gates**: it only asks pairs still incomparable under the gold partial order built so
far, so every call buys a new constraint instead of re-deriving one transitivity already implies. This
matters more the more you gather: random pair selection wastes a fast-growing fraction on already-
implied pairs (about a quarter by ~8k decisions, more beyond), and gating reclaims that budget, worth
several points of final ranking quality. It runs in parallel rounds (`--batch` pairs per round, then
the closure updates). `--no-gate` reverts to one fully-parallel sweep of random pairs (fine at small
budgets, where almost nothing is implied yet). Note: gate WHICH pairs are eligible, but pick among them
at RANDOM. Cleverly choosing the "most informative" eligible pair (by model uncertainty or order
adjacency) measurably underperforms random, because those pairs are the noisy near-ties.

How many comparisons you need depends on the goal. To directly rank THIS hull, ~8-12 per role gives
each one enough head-to-heads. To train the distilled model in step 3 for corpus-wide reuse, far
fewer: measured on a ~2,600-role hull, held-out accuracy is within 1% of its ceiling by ~2,000 total
decisions (about 1.5 per role) and fully saturated by ~4,000, so ~3 per role is plenty. Bound cost
with `--max-comparisons`.

There is also `--mode sort`: a contradiction-free merge sort that emits an exact total order over a
small shortlist. It only compares the heads of two already-sorted runs, so it never asks a
transitively-implied comparison and a noisy comparator cannot make it self-contradict, but its final
merge is a sequential ~n tail. Use it to get a guaranteed-consistent order over a few dozen finalists,
not to learn over thousands.

### Step 3 — rank (btrank.py)

The LLM's decisions are treated as GOLD. Each is an edge (winner ranked above loser), and together
they define a partial order; btrank topologically sorts it, so the final ranking honors every decision
it can. A model distilled from the same decisions (logistic regression over the job embeddings) only
DISAMBIGUATES: it orders roles the decisions leave incomparable, places roles never compared (the
small uncompared tail, or the whole corpus), and breaks cycles if any exist (strongly-connected
components are condensed first). On a real ~2,600-role hull this fits held-out decisions ~90%, versus
~68% for the model alone and ~70% for a soft blend, because the decisions' transitive structure carries
far more than the embeddings do. In practice the decision graph is acyclic, so the gold order honors
100% of decisions and the model only fills the gaps.

So the two goals from step 2 have different appetites for decisions: the distilled model saturates at
~4K, but this hull ranking keeps sharpening with every decision you add, since each one pins down more
of the order.

`--method` picks the ranker: `gold` (default, above; needs `--parquet` for the embeddings), `fuse` (a
softer blend that lets the model override sparse decisions, more robust to a very noisy comparator but
it bottlenecks at the embedding ceiling), or `bt` (plain Bradley-Terry, no embeddings needed).
`--distill-out taste.npz` also saves the model as a corpus-wide ranker in score_distill.py's format, so
the taste generalizes to every future snapshot with no further LLM calls.

### Add-ons and hand-rolling

- `rank.py` — pure-embedding RECALL (lexical seed -> learn a ridge ranker in embedding space -> score
  the corpus). Use it to build a hull semantically when titles and facets do not capture intent, or to
  rank all ~1M rows directly. `python3 rank.py --function engineering --seed "backend,rust,kubernetes,api,crypto"`.
- `match.py` — per-role LLM EXPLANATION on a shortlist: pulls each finalist's full JD and returns
  strengths, gaps, and a verdict. Good for the final "why" once btrank has the order.
  `python3 match.py --resume resume.txt --candidates ranked.json --top 40`.

To hand-roll any stage: load the parquet (section 1), filter with pandas masks, rank in numpy. For a
single anchor query, embed a 2-4 sentence description of the ideal role with `text-embedding-3-small`
at 1536 dims and take cosine against `jd_embedding`. For a title query, max-pool cosine over a row's
`alt_titles_embedding` (a list of per-variant vectors, so you match the closest variant, not a blurred
average). Embeddings only compare if you embed the query with the same model at 1536 dims.

---

## 5. Present results as an HTML report

Write the shortlist to a self-contained `report.html` the person double-clicks to open. Embed the
data as JSON and add a search box + sortable columns with a few lines of vanilla JS. No server.

```python
import json, html
cols = ["company", "title", "level", "salary_min_k", "salary_max_k", "remote_scope",
        "country_code", "score", "url", "role_summary"]
data = json.dumps(top[cols].to_dict("records"))

doc = """<!doctype html><meta charset=utf-8><title>Job matches</title>
<style>
 body{font:14px/1.5 system-ui;margin:2rem;max-width:1100px}
 input{padding:.5rem;width:100%;margin-bottom:1rem;font-size:1rem}
 table{border-collapse:collapse;width:100%} th,td{padding:.4rem .6rem;border-bottom:1px solid #ddd;text-align:left}
 th{cursor:pointer;background:#fafafa;position:sticky;top:0} tr:hover{background:#f6f9ff}
 a{color:#1558d6;text-decoration:none} .sub{color:#666;font-size:12px}
</style>
<input id=q placeholder="filter by company, title, summary...">
<table id=t><thead><tr>
 <th>Company</th><th>Title</th><th>Level</th><th>Salary (k)</th><th>Remote</th><th>Loc</th>
 <th data-sort=num>Match</th><th>Apply</th></tr></thead><tbody></tbody></table>
<script>
const D=__DATA__; const tb=document.querySelector('#t tbody'); let asc=false;
function sal(r){return r.salary_min_k>0?`${r.salary_min_k}-${r.salary_max_k>0?r.salary_max_k:'?'}`:''}
function draw(rows){tb.innerHTML=rows.map(r=>`<tr>
 <td>${r.company||''}</td>
 <td>${r.title||''}<div class=sub>${(r.role_summary||'').slice(0,120)}</div></td>
 <td>${r.level||''}</td><td>${sal(r)}</td><td>${r.remote_scope||''}</td><td>${r.country_code||''}</td>
 <td>${(r.score||0).toFixed(3)}</td>
 <td>${r.url?`<a href="${r.url}" target=_blank>apply</a>`:''}</td></tr>`).join('')}
draw(D);
q.oninput=e=>{const s=e.target.value.toLowerCase();
 draw(D.filter(r=>JSON.stringify(r).toLowerCase().includes(s)))};
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{asc=!asc;
 const k=['company','title','level','salary_min_k','remote_scope','country_code','score'][i];
 if(!k)return; draw([...D].sort((a,b)=>(a[k]>b[k]?1:-1)*(asc?1:-1)))});
</script>"""
open("report.html","w").write(doc.replace("__DATA__", data))
```

For each role the person should see: company, title, a one-line why-it-fits, comp if known,
remote/location reality, match score, and the apply link. Then offer to go deeper on any one: read
its full `jd_markdown` and draft a tailored application.

---

## 6. Rules

- The `url` is the application link. Never invent one; only use what is in the row.
- Treat the structured fields as strong hints, not gospel. They are LLM extractions: mostly right,
  occasionally wrong. For anything that decides whether the person applies (comp, work
  authorization, location), confirm against `jd_markdown` before telling them it is true.
- Respect the unknown sentinels. Never present `-1` as a salary or `""` as a location.
- This is open, real data about real openings. Do not fabricate roles, companies, or details. If the
  filtered set is empty, say so and loosen a constraint with the person, do not invent matches.
- Embeddings are only comparable if you embed the query with `text-embedding-3-small` at 1536 dims.
  A different model gives garbage rankings.

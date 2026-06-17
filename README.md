# open-jobs

A single Parquet file containing roughly **967,000 currently-open jobs**, pulled from **16 applicant
tracking systems**, enriched with LLM-extracted structured fields and precomputed embeddings, plus a
small set of scripts that find the roles a given resume should actually apply to. Released **CC0** (public
domain, no attribution required).

Everyone who sets out to build a job board eventually discovers the hard part isn't the product, it's
that the data is the same data everyone else is fighting over. So instead of monetizing it, here it is.
Point your own agent at it.

```
https://download.jobscream.com/open-jobs.parquet      # ~21 GB, refreshed in place daily
```

## What's here

| File | What it does |
|------|--------------|
| `AGENTS.md` | The manual. Hand this to an LLM agent and it can drive the whole pipeline. |
| `download.py` | Resumable streaming download of the dataset. |
| `stream.py` | Iterate the dataset row by row, memory-bounded, local file or straight off the URL. |
| `hull.py` | **Step 1.** Filter to the "convex hull" of a search: the smallest set that still contains every relevant role. |
| `langsort.py` | **Step 2.** Gather the LLM's pairwise judgments ("which of these two fits better?") over the hull. |
| `btrank.py` | **Step 3.** Aggregate those judgments into one ranking (Bradley-Terry), and optionally distill them into a reusable model. |
| `rank.py` | An embedding-only recall ranker (lexical seed -> learned ridge ranker -> score the corpus). |
| `match.py` | Per-role LLM explanation: strengths, gaps, and a verdict for a shortlist. |

## The dataset

One row per open role. ~967K rows, snapshot (not history): the file is overwritten daily, so yesterday's
is gone. Each row carries:

- **Identity & apply**: `id`, `ats`, `company`, `url` (the real application link).
- **Content**: `title`, and the full job description cleaned to Markdown (`jd_markdown`).
- **Embeddings** (`text-embedding-3-small`, 1536-dim): one vector for the title, one for the JD, and a
  *list* of vectors for the alternate titles (each embedded separately, so a query matches the closest
  variant rather than a blurred average).
- **~34 structured fields** extracted from each JD by an LLM: `level`, `function`, `sub_function`,
  `salary_min_k`/`salary_max_k`, `work_mode`, `remote_scope`, `country_code`, `visa_sponsorship`,
  `skills`, `alt_titles`, `years_experience_min`, and more. Unknown values use explicit sentinels
  (`-1`, `""`, `"unknown"`), so "salary not stated" is never confused with "salary is zero."

Full schema and field semantics are in `AGENTS.md`.

## Using it: hull -> learn -> rank

The pipeline is three steps. Draw the smallest eligible set that still contains everything relevant,
spend LLM judgment only inside it, and aggregate that judgment into an order.

```bash
pip install pyarrow numpy            # + an OPENAI_API_KEY for steps 2-3

python3 hull.py     --function engineering --level Senior,Staff --country US --remote \
                    --title "software engineer,backend,platform" --out hull.json
python3 langsort.py --resume resume.txt --candidates hull.json --mode sample --per-item 12
python3 btrank.py   --candidates hull.json --decisions langsort_decisions.jsonl --out ranked.json
```

That's it: `ranked.json` is your shortlist, best first.

## Why it's built this way

A few decisions worth calling out, because the design is the point.

- **Two-stage retrieval.** Cheap structured filtering shrinks a million rows to a few thousand;
  expensive LLM judgment is then spent only on what survives. You never pay model cost to reject the
  obviously irrelevant.

- **Pairwise judgment, not absolute scores.** Asking a model "rate this 0-100" produces mushy, bunched
  numbers (everything lands near 85). Asking "which of these two fits better?" is far easier and
  better-calibrated. `langsort.py` collects those comparisons; `btrank.py` turns them into a ranking.

- **The decisions are gold; the model only disambiguates.** Each comparison is an edge (winner above
  loser), and together they form a partial order that `btrank.py` topologically sorts, honoring every
  decision it can. A model distilled from the same decisions (logistic regression over the embeddings)
  only orders what the decisions leave incomparable, places never-compared roles, and condenses cycles
  if any appear. On a real ~2,600-role hull this predicts held-out decisions **~90%**, versus ~68% for
  the model alone, because the decisions' transitive structure carries far more than the embeddings do.
  In practice the graph comes out acyclic, so the gold order is honored 100% and the model just fills
  gaps. (`langsort.py --mode sort` can instead produce a guaranteed contradiction-free total order over
  a small shortlist, via a merge sort that never asks a transitively-implied comparison.)

- **Gather only what isn't already implied.** Collecting comparisons, `langsort.py` gates to pairs still
  incomparable under the partial order, so every LLM call buys a new constraint. Random pairing wastes a
  fast-growing share re-deriving implied orderings (a quarter-plus by ~8k decisions); gating reclaims it,
  worth several points of ranking quality. But it picks *among* the eligible pairs at random: choosing
  the "most informative" one (by model uncertainty or order-adjacency) measurably loses, because those
  are the noisy near-ties. Gate which pairs are eligible; choose among them by coin flip.

- **Lexical and semantic recall, unioned.** Measured on this corpus for "software engineer": matching the
  alternate titles finds **+56%** more roles than embeddings alone, yet still misses **~24%** of jobs
  whose literal title contains the term. Neither alone is enough, so `hull.py` unions them.

- **Distillation.** `btrank.py --distill-out` turns thousands of pairwise judgments into a linear model
  over the embeddings (logistic regression on embedding differences). It then scores the *entire* corpus
  with no further model calls: the taste learned on one hull generalizes to every future snapshot, for
  free. It saturates fast, held-out accuracy is within 1% of its ceiling by ~2,000 decisions.

- **Memory-bounded throughout.** A 21 GB dataset was built and is consumed on a 38 GB laptop. Everything
  streams (chunked Parquet, memory-mapped embedding caches); nothing assumes the corpus fits in RAM.

- **Cost-aware and resumable.** Comparisons use a nano model with token-capped, decision-only outputs,
  and every decision is appended to a log that replays on restart, so a killed run resumes without losing
  or repeating work.

## License

CC0 1.0 (public domain). Use it for anything, commercial or otherwise, no attribution required. The data
describes real, currently-open roles; don't fabricate, and confirm anything load-bearing (comp, work
authorization) against `jd_markdown` before relying on it.

## Provenance

Built by Elliott Dehnbostel. The productized version, which ranks new listings against a resume and emails
a daily digest of the strongest matches, runs at [JobScream.com](https://jobscream.com). More at
[github.com/elliottdehn](https://github.com/elliottdehn); a resume sits alongside this file as
`egd-resume.txt`.

# 0005: Pick models by benchmark, not by default

**Status:** Accepted: ER adjudication, name embeddings, document extraction and agent tool-calling (benchmarks below, 2026-11-13/14)

## Context

Databricks Wizard uses models for four jobs: agent tool-calling, heavier reasoning, bulk row-level `ai_query()`, and document understanding. Free Edition offers these endpoints (checked 2026-11-13):

`databricks-gpt-oss-120b`, `databricks-gpt-oss-20b`, `databricks-qwen3-next-80b-a3b-instruct`, `databricks-qwen35-122b-a10b`, `databricks-gemma-3-12b`, `databricks-meta-llama-3-1-8b-instruct`, `databricks-meta-llama-3-3-70b-instruct`, `databricks-llama-4-maverick`, `databricks-gte-large-en`, `databricks-bge-large-en`, `databricks-qwen3-embedding-0-6b`

## Decision

Starting assignments:

| Role | Primary | Candidates to benchmark |
|---|---|---|
| Agent / MCP tool-calling | `qwen35-122b-a10b` | `llama-4-maverick`, `llama-3-3-70b-instruct` |
| Reasoning (risk narratives, ER explanations) | `gpt-oss-120b` | `qwen3-next-80b-a3b-instruct` |
| Bulk `ai_query()` (classify, normalize, extract) | `llama-3-1-8b-instruct` | `gpt-oss-20b`, `gemma-3-12b` |
| ER adjudication | `qwen35-122b-a10b` | `gpt-oss-120b`, `llama-3-3-70b-instruct` |
| Document images | `llama-4-maverick` | `gemma-3-12b` |
| Embeddings | `gte-large-en` | `bge-large-en`, `qwen3-embedding-0-6b` |

Final choices come from a benchmark in M7:

- **Tool-calling:** a fixed set of Supplier 360 questions with expected tool calls and answers. Scored on tool-selection accuracy, argument correctness, answer correctness and p50/p95 latency.
- **ER adjudication:** labelled middle-band pairs. Scored on accuracy and agreement with ground truth.
- **Embeddings:** recall@k on supplier-name variants and on document queries.

## Consequences

- Model choice is backed by evidence, and the benchmark becomes the seed of the Project 3 agent-evaluation framework.
- Reasoning models (gpt-oss) spend tokens on thinking. `max_tokens` must allow for this; the capability check showed an empty reply at `max_tokens=20`.
- Model IDs live in config, never in code, so changing a model is a config change plus a benchmark rerun.

## Benchmark 1: ER adjudication (M4)

90 pairs sampled from the uncertain band (0.50 ≤ p < 0.95), scored against the generator's answer key
(7 true matches, 83 non-matches). Same prompt (`er-adjudicate-v1`), temperature 0. Code: `er/wizard_er/bench.py`.

| Model | Accuracy on decided | Unsure | False match | False non-match |
|---|---|---|---|---|
| `meta-llama-3-3-70b-instruct` | 0.57 | 2% | 37 | 1 |
| `llama-4-maverick` | 0.87 | 31% | 7 | 1 |
| **`qwen3-next-80b-a3b-instruct`** | **0.92** | **2%** | 7 | **0** |

**Decision:** Qwen3-Next 80B Instruct adjudicates ER pairs. On the full band its "no_match" verdicts were 342/342
correct; its wrong "match" verdicts cluster on one-record-to-many patterns, which the clustering guard refuses.

`qwen35-122b-a10b` (the original primary) could not be used: it is a reasoning model whose output is a list of
blocks starting with a long reasoning block, and 2,000 tokens were not enough to reach the answer. `/no_think`
is ignored and the endpoint rejects chat-template parameters.

## Benchmark 2: name embeddings (2026-11-13)

All 5,374 supplier names embedded with each model; for every record with a true partner (answer key), is a
partner its nearest neighbour (recall@1) or in the top 5 (recall@5)? Code: `er/wizard_er/bench_embeddings.py`.

| Scope | `qwen3-embedding-0-6b` | `gte-large-en` | `bge-large-en` |
|---|---|---|---|
| Overall (n=2,817) | **0.888 / 0.931** | 0.846 / 0.865 | 0.844 / 0.875 |
| Cyrillic–Latin (n=536) | **0.271 / 0.465** | 0.007 / 0.024 | 0.041 / 0.132 |
| Latin–Latin (n=2,149) | 0.966 / 0.990 | 0.976 / 0.994 | 0.967 / 0.993 |
| Polish–Polish (n=231) | 0.952 / 0.996 | 0.961 / 0.991 | 0.948 / 0.996 |

**Decision:** keep `qwen3-embedding-0-6b`. The models are equal on Latin names; across scripts the English models
fail almost completely. **Even the multilingual model finds a cross-script partner in the top 5 only 47% of the
time**, so embeddings are a backup candidate source; the transliteration skeleton (ADR 0004) is what makes
Cyrillic–Latin matching work (F1 0.972).

## Benchmark 3: document field extraction (M6)

80 documents (40 certificates incl. scanned, 40 contracts), parsed with `ai_parse_document`, fields extracted with
`ai_query` and a JSON schema per type; every field compared with the document answer key (`gt_document`).
Code: `scripts/m6_extraction_bench.py`.

| Model | All fields | Text certs | Scanned certs | Contracts | Time (80 docs) |
|---|---|---|---|---|---|
| **`llama-4-maverick`** | **0.999** | 0.997 | **1.000** | 1.000 | 33 s |
| `meta-llama-3-3-70b-instruct` | 0.999 | 1.000 | 0.992 | 1.000 | 80 s |
| `qwen3-next-80b-a3b-instruct` | 0.989 | 0.991 | 0.983 | 0.989 | 14 s |
| `meta-llama-3-1-8b-instruct` | 0.986 | 0.978 | 0.992 | 0.991 | 14 s |

**Decision:** Llama 4 Maverick. All four reach ~99% once the schema is right; the first, looser schema let models
drop fields (Qwen omitted the expiry date) and swap contract parties (both Llamas named the customer as supplier).
Schema and prompt design mattered more than model choice.

## Benchmark 4: agent tool-calling over the MCP servers (M7)

12 business questions, each answered by an agent loop (`agents/wizard_agent.py bench`) with the 15 Wizard MCP tools.
Expected answers are computed live from gold. Scored: right tool used, answer correct, tool errors, latency.
One question tests policy: an unconfirmed sanctions alert must not be reported as "sanctioned".

| Model | Right tool | Correct answer | Tool errors | Avg time |
|---|---|---|---|---|
| **`qwen3-next-80b-a3b-instruct`** (with date context) | **12/12** | **12/12** | 1 | **6.1 s** |
| `gpt-oss-120b` | 12/12 | 12/12 | 1 | 7.4 s |
| `meta-llama-3-3-70b-instruct` | 12/12 | 11/12 | 3 | 8.9 s |
| `llama-4-maverick` | 10/12 | 7/12 | 2 | 7.6 s |

**Decision:** Qwen3-Next is the default agent model (perfect and fastest); GPT-OSS 120B is the alternative.

What the benchmark changed, beyond picking a model:
- **A missing tool, not weak models:** in the first run every model failed "which category is most concentrated?"
  by writing broken exploratory SQL. Adding a governed `get_spend_concentration` tool fixed it for all of them.
- **Agents need the date:** Qwen refused "since May 2026" as "in the future". The harness now states today's date
  and the data's as-of date.
- **Format reliability differs from task quality:** Maverick, the best document extractor (benchmark 3), often writes
  tool calls as text (`[get_document(...)]`) instead of structured calls, so it is not used for agents.
- **Scoring needs care:** a "wrong" answer used non-breaking hyphens in dates (`2026‑06‑19`); the checker now
  normalizes dash characters.

## Benchmark 5: clause extraction from real contracts (CUAD)

Benchmark 3 scores extraction against documents this project generated. CUAD tests the same approach (one `ai_query`
call with a strict JSON schema) on real commercial contracts filed with the SEC, converted from PDF to text, against
the Atticus Project's expert answers. Code: `scripts/cuad_bench.py` (I/O) and `scripts/cuad_score.py` (scoring, tested).

- **Sample:** 100 contracts, picked deterministically (smallest SHA-1 of the file name) from the 410 labelled contracts
  under 80,000 characters (~20k tokens), so every model sees the whole contract.
- **Fields a procurement team acts on:** agreement date, governing law, renewal term, notice to stop renewal, and
  8 yes/no clauses: anti-assignment, change of control, termination for convenience, cap on liability, audit rights,
  insurance, minimum commitment, warranty duration. Each field's definition is CUAD's own category description.
- **Scoring:** dates compare only the parts CUAD knows (`[]/[]/2020` checks the year); governing law ignores
  "State of", "laws of" and similar; renewal and notice compare days (a month is 30, a year 365, ±3%); answers CUAD
  gives in another form (e.g. "perpetual" renewal) are not scored. For the yes/no clauses, precision and recall of
  "Yes" are reported too, because a missed clause costs a reviewer more than a false flag.
- **Coverage in the sample:** 94–100 scored answers per field; "Yes" rates range from 8 (warranty duration) to 71
  (anti-assignment).

Results (run 2026-11-15, 100 contracts, no failed responses for any model):

| Model | All fields | "Yes" precision / recall | Governing law | Agreement date | Termination for convenience | Change of control | Time |
|---|---|---|---|---|---|---|---|
| `gpt-oss-120b` | **0.912** | **0.90** / 0.78 | **0.97** | 0.84 | **0.89** | **0.91** | 150 s |
| **`llama-4-maverick`** | 0.894 | 0.79 / 0.88 | 0.95 | 0.88 | 0.85 | 0.82 | 70 s |
| `qwen3-next-80b-a3b-instruct` | 0.872 | 0.70 / **0.93** | 0.92 | **0.89** | 0.61 | 0.81 | **46 s** |
| `meta-llama-3-3-70b-instruct` | 0.831 | 0.65 / 0.91 | 0.95 | 0.88 | 0.72 | 0.47 | 79 s |

**Decision:** keep Llama 4 Maverick for document extraction. On real contracts it is second overall, 0.02 behind
GPT-OSS 120B, with the same "Yes" F1 (0.83 vs 0.84). The two trade off differently: GPT-OSS misses more
clauses (recall 0.78) and Maverick flags more that aren't there (precision 0.79). For review triage a miss costs
more than a false flag, and Maverick is twice as fast. **The generated documents overstate accuracy:** the same
approach that scores 0.999 on this project's own documents (benchmark 3) scores 0.89 on real filings. Judgement clauses
(termination for convenience, change of control) are the weak spot, so extracted clauses are suggestions for a
reviewer, not decisions.


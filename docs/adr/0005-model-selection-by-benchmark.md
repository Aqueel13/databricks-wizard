# 0005: Pick models by benchmark, not by default

**Status:** Accepted for ER adjudication (benchmark below, 2026-11-13); tool-calling and embeddings still to benchmark in M7

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

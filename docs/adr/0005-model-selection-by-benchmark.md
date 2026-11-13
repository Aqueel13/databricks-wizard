# 0005: Pick models by benchmark, not by default

**Status:** Proposed · 2026-11-13 (decided in M7 once the benchmark has run)

## Context

ATLAS uses models for four jobs: agent tool-calling, heavier reasoning, bulk row-level `ai_query()`, and document understanding. Free Edition offers these endpoints (checked 2026-11-13):

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

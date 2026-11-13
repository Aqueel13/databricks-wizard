# 0004: Layered entity resolution

**Status:** Accepted · 2026-11-13

## Context

Supplier records from five sources have to be resolved into one golden record. Name matching alone fails: legal suffixes vary, abbreviations are common, and different companies can have nearly identical names. The method has to be explainable to data stewards and auditors, and cheap enough for Free Edition.

## Decision

Resolve in layers, cheapest and most certain first:

0. **Multilingual normalization:** every name keeps its original form and also gets
   - a **transliterated** form (ICU `Any-Latin; Latin-ASCII`, so Cyrillic and Polish diacritics become ASCII),
   - a **legal-form-stripped** form using a per-country dictionary (`ТОВ`, `TOV`, `Sp. z o.o.`, `S.A.`, `GmbH`, `LLC`, `Ltd`, …) that also records the legal form as a separate, comparable attribute,
   - a **language/script tag**.
   National IDs (EDRPOU, NIP, REGON, EIN, UEI) are validated with country-specific checksums before they are used for matching.
1. **Deterministic:** exact match on a validated national ID, LEI or UEI links records immediately.
2. **Probabilistic:** **Splink** (Fellegi–Sunter) on Spark, with blocking rules (national ID, LEI, transliterated name token, country + postcode) and comparisons on transliterated name (Jaro-Winkler), legal form, address, national ID, bank-account hash, and embedding cosine similarity of the **original-script** name. Embeddings: `databricks-qwen3-embedding-0-6b` (multilingual) for cross-script pairs and `databricks-gte-large-en` for English-only pairs, with the choice benchmarked (ADR 0005).
3. **LLM adjudication:** pairs with probability 0.60–0.95 go to `ai_query()`. The model returns a JSON verdict (`match` / `no_match` / `unsure`) with a reason. The prompt version and model are logged.
4. **Steward review:** `unsure` verdicts go to a review queue. Steward decisions are stored and override every other layer.
5. **Clustering and survivorship:** connected components form clusters. Field-level survivorship rules build `gold.dim_supplier`.

Thresholds are tuned against ground truth and stored in `ops.er_config` (versioned).

## Alternatives considered

- **LLM for all pairs.** Expensive, slow, not repeatable, and a poor fit for Free Edition quotas.
- **Commercial MDM (Informatica, Reltio, Tamr).** The usual enterprise answer, and noted as such in the write-up, but it costs money and hides the logic the project is meant to show.
- **Pure embedding similarity.** Fails on short names and on look-alike companies.

## Consequences

- Every link has evidence (method, score, comparison levels, LLM reason) in `gold.supplier_match_evidence`.
- The LLM runs on only a small share of pairs, keeping cost and latency low.
- Splink must install on serverless compute (pip). This is verified in M4. Fallback: run Splink with its DuckDB backend in a job, then write the results back to Delta.
- Precision, recall and F1 are tracked per run in `ops.er_eval`, **broken down by script pair** (Latin–Latin, Cyrillic–Latin, Polish–ASCII), so weak spots in cross-language matching show up instead of being averaged away.
- Transliteration alone is not enough for translated names (`Укрзалізниця` vs `Ukrainian Railways`). Those cases depend on multilingual embeddings and LLM adjudication, and are expected to make up much of the steward review queue.
- The same normalization and matching code is reused for sanctions screening against OFAC aliases.

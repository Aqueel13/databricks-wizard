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

## Implementation notes (M4, 2026-11-13)

What the built system does, and why it differs from the plan above:

| Layer | Implementation |
|---|---|
| Normalization | `er/wizard_er/normalize.py`: `unidecode` transliteration (independent of the data generator), legal forms stripped in 4 languages incl. full Cyrillic phrases, abbreviation folding, a **phonetic skeleton** that collapses transliteration variants (`Юг-Агрос`, `Yuh-Ahros`, `Yug-Agros` → `uhahros`), legal **jurisdiction** (GmbH=DE, B.V.=NL, TOV=UA, …; generic forms carry none), numbers in the name |
| Candidates | Splink blocking (valid ID, ID minus check digit, country + skeleton prefix, first token, sorted tokens, local-script skeleton) **plus embedding neighbours** (`qwen3-embedding-0-6b`, top-6 at cosine ≥ 0.80), which is how translated names get compared at all |
| Scoring | Splink 4 on **DuckDB inside the job** (the Spark backend needs a JAR; serverless can't load one). One hierarchical name comparison (exact core → exact skeleton → local-script skeleton → skeleton JW → sorted-token JW → embedding neighbours), national ID with a "one character apart" level, country, city, legal form, name numbers. Unsupervised EM, except one **expert-fixed** parameter: differing name numbers (EM can't learn it because SAP training copies dominate the sample) |
| Deterministic tiers | equal valid national ID; **exact normalized name** + same country + nothing contradicting (city, IDs, jurisdiction). Exact duplicates otherwise sat at p≈0.6–0.8 because SAP training copies inflate the exact-name `u` |
| LLM | `databricks-qwen3-next-80b-a3b-instruct` (chosen by benchmark, ADR 0005) for 0.50 ≤ p < 0.95; verdicts cached in `silver.er_llm_adjudications` by pair, model and prompt version |
| Clustering | union-find, strongest evidence first, with **cluster-level constraints**: no merge across different valid national IDs, different jurisdictions or clearly different cities. LLM links are applied last and refused when the LLM matches one record to several clusters (ambiguity, not identity). Refused links → `silver.er_review_queue` |
| Survivorship | `gold.dim_supplier`: legal name HICX > SAP > Coupa; national ID from validated sources only; local-script name kept; source system ids listed |

### Results (Databricks job, answer key from the generator)

| Metric | Precision | Recall | F1 | Target |
|---|---|---|---|---|
| Pairwise, all records | 0.974 | 0.961 | **0.968** | ≥ 0.95 |
| Pairwise, Cyrillic–Latin | 1.000 | 0.946 | **0.972** | ≥ 0.85 |
| B-cubed | 0.991 | 0.990 | 0.990 | |
| Within-system duplicates found | | 40/42 | | |
| Conflicting addresses flagged | | 29/30 | | |
| Sanctions near-matches alerted | | 6/6 | | |

Evolution that mattered, kept for the write-up: baseline F1 0.47 → exact-name tier → constraints → answer-key fix (below) → name-number prior → guarded LLM. The unguarded LLM *lowered* pairwise F1 (0.961) by chaining clusters; the ambiguity guard fixed it.

### Answer-key correction

The generator first grouped SAP demo vendors by `NAME1` + city, so training copies ("… Gr. 01") counted as one supplier when the group label overflowed into `NAME2` and as separate suppliers otherwise. No matcher could satisfy both. `wizard-sources gt-refine` re-groups by full name + city without touching any generated data (system files verified identical by checksum).

### Known limitations

- Sanctions screening is name-only: 82 alerts for 6 planted near-matches. Alerts are for review, never findings. Next step: OFAC addresses (`add.csv`) for country disambiguation.
- External benchmark (Amazon ML Challenge sample) not yet run.

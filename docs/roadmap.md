# Roadmap

Each milestone ends with something that can be demonstrated. Exit criteria are pass/fail.

| # | Milestone | Scope | Exit criteria |
|---|---|---|---|
| M0 | Platform check | Free Edition workspace, capability notebook, docs | ✅ All capability checks pass (2026-11-13) |
| M1 ✅ | Sources & data | Docker Compose: Postgres (SAP schema), Debezium, Redpanda, Coupa mock API, HICX export job, PDF generator. Synthetic generator with injected defects and ground truth. Public-data snapshots. | `docker compose up` produces CDC events, API pages, exports and PDFs; ground-truth tables exist |
| M2 | Landing & bronze | Shipper uploads to the UC Volume with manifests. Lakeflow pipeline with Auto Loader bronze tables. | A change in Postgres reaches a bronze table without manual steps; manifest reconciliation passes |
| M3 | Silver & DQ | `AUTO CDC` (SCD1/SCD2), canonical supplier model, expectations, quarantine, DQ scorecard | Late and out-of-order CDC handled correctly; every injected DQ defect appears in quarantine or the scorecard |
| M4 | Entity resolution | Multilingual normalization (transliteration, legal forms, national-ID checks), blocking, Splink, embeddings, LLM adjudication, review queue, survivorship, `dim_supplier` | F1 ≥ 0.95 overall and ≥ 0.85 on Cyrillic–Latin pairs; every link has evidence |
| M5 | Gold & semantic layer | Facts, KPIs, risk score, spend concentration, metric views or governed views, Genie space, AI/BI dashboard | The 4 core business questions are answered correctly in Genie and on the dashboard |
| M6 | Knowledge layer | Document parsing, extraction, chunks, Vector Search index; extracted fields feed risk | Cited answers to document questions; expired certificates raise the compliance score |
| M7 | Agent interface | MCP server on Databricks Apps, model benchmark (ADR 0005), WatchDog integration | WatchDog pulls supplier context through MCP; benchmark results published |
| M8 | Governance & delivery | Tags, masks, row filters, service principal, Asset Bundles, GitHub Actions, `trace_metric` lineage tool | Clean deploy from CI; agent can't read masked columns; lineage traced end to end |
| M9 | Story | Architecture write-up, business case, 3-minute demo video, case-study page | Published |

## Stretch goals

- `simulate_consolidation` what-if tool
- Steward review UI (second Databricks App) for the entity-resolution queue
- Supplier hierarchy rollups via GLEIF parent relationships
- Drift alerts: a Lakeflow job that flags newly deteriorating suppliers and notifies WatchDog

## Links to other portfolio projects

- **Intelligence WatchDog** consumes Databricks Wizard through MCP (M7).
- **Agent evaluation framework (Project 3)** builds on the model benchmark from M7 and the entity-resolution eval from M4.

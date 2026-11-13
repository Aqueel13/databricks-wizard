# Roadmap

Each milestone ends with something that can be demonstrated. Exit criteria are pass/fail.

| # | Milestone | Scope | Exit criteria |
|---|---|---|---|
| M0 | Platform check | Free Edition workspace, capability notebook, docs | ✅ All capability checks pass (2026-11-13) |
| M1 ✅ | Sources & data | Docker Compose: Postgres (SAP schema), Debezium, Redpanda, Coupa mock API, HICX export job, PDF generator. Synthetic generator with injected defects and ground truth. Public-data snapshots. | `docker compose up` produces CDC events, API pages, exports and PDFs; ground-truth tables exist |
| M2 ✅ | Landing & bronze | Shipper uploads to the UC Volume with manifests. Lakeflow pipeline with Auto Loader bronze tables. | A change in Postgres reaches a bronze table without manual steps; manifest reconciliation passes |
| M3 ✅ | Silver & DQ | `AUTO CDC` (SCD1/SCD2), canonical supplier model, expectations, quarantine, DQ findings | Late and out-of-order CDC handled correctly (`scripts/m3_out_of_order_test.sh`); every injected defect that a single-source rule can detect is found (`scripts/sql/m3_dq_eval.sql`). Duplicates and conflicting addresses are scored in M4, sanctions in M4, delivery drift in M5, document mismatches in M6 |
| M4 ✅ | Entity resolution | Multilingual normalization (transliteration, legal forms, jurisdictions, phonetic skeleton), Splink (DuckDB) + embedding neighbours, exact-name and ID tiers, LLM adjudication (benchmarked), cluster constraints, review queue, survivorship, `dim_supplier`, sanctions screening | Pairwise F1 **0.968** overall, **0.972** Cyrillic–Latin (targets 0.95 / 0.85); every link has evidence in `gold.supplier_match_evidence`. External benchmark still to run |
| M5 ✅ | Gold & semantic layer | Facts (PO lines, deliveries, invoices) on the golden supplier, market-adjusted deterioration KPI, risk score, spend concentration, DQ scorecard, Supplier 360; UC metric views (spend, delivery, supplier); Genie space; AI/BI dashboard (bundle) | Genie answers the 4 core questions matching gold (4/4 pass, `scripts/m5_genie_eval.py`); dashboard datasets verified |
| M6 ✅ | Knowledge layer | `ai_parse_document` (text + OCR), schema-based extraction (benchmarked), reconciliation vs HICX/Coupa, page-level chunks, Vector Search (hybrid + identifier lookup), cited Q&A; document expiry feeds risk | 40/40 cited answers correct; 28/28 planted document mismatches found (0 false positives); document-expired certificates raise compliance risk |
| M7 ✅ | Agent interface | Three MCP servers on one Databricks App (analytics, knowledge, stewardship) mirroring WatchDog; audit log; agent proposals approved by people in the steward app; agent harness; tool-calling benchmark (ADR 0005); combined agent with WatchDog (read-only) | Combined agent answers cross-system questions with attribution; benchmark published; every call audited |
| M8 ✅ | Governance & delivery | Tags, masks, row filters, service principal, Asset Bundles, GitHub Actions, `trace_metric` lineage tool | ✅ Clean deploy from CI (`wizard-ci` service principal, OAuth M2M); ✅ agent can't read masked columns (masks + health check); ✅ company-code row filter verified as a scoped SP; ✅ lineage traced end to end, table and row level (`scripts/lineage_e2e_check.py`) |
| M9 | Story | Architecture write-up, business case, 3-minute demo video, case-study page | Published |

## Stretch goals

- `simulate_consolidation` what-if tool
- Steward review UI (second Databricks App) for the entity-resolution queue
- Supplier hierarchy rollups via GLEIF parent relationships
- Drift alerts: a Lakeflow job that flags newly deteriorating suppliers and notifies WatchDog

## Links to other portfolio projects

- **Intelligence WatchDog** consumes Databricks Wizard through MCP (M7).
- **Agent evaluation framework (Project 3)** builds on the model benchmark from M7 and the entity-resolution eval from M4.

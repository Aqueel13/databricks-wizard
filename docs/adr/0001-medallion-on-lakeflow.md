# 0001: Medallion architecture on Lakeflow Declarative Pipelines

**Status:** Accepted · 2026-11-13

## Context

ATLAS combines CDC streams, API pages, file exports and documents. Each needs to be kept raw for audit, cleaned and conformed, then shaped for business use. Data quality has to be enforced and visible, not hidden in notebook code.

## Decision

Use a bronze/silver/gold medallion layout in the `atlas` catalog, built with Lakeflow Declarative Pipelines:

- Bronze: Auto Loader streaming tables, append-only, with ingest metadata
- Silver: `AUTO CDC` for change data, expectations for quality, quarantine tables for rejected rows
- Gold: materialized views, plus jobs for entity resolution (iterative logic that doesn't fit a declarative flow)

## Alternatives considered

- **Hand-written Structured Streaming + Jobs.** More control, but checkpoints, CDC ordering and DQ metrics all have to be built by hand. More code with no portfolio benefit.
- **dbt on a SQL warehouse.** Strong for gold modelling, but weak for streaming CDC ingestion, and it adds a second tool.

## Consequences

- Pipeline DQ metrics and the event log come built in and can feed the DQ dashboard.
- Entity resolution runs as a separate job between silver and gold, so the DAG is pipeline → ER job → gold pipeline.
- Lakeflow-specific syntax ties ATLAS to Databricks, which is acceptable because Databricks is the platform being demonstrated.

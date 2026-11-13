# 0002: Local source simulators with push-based landing into UC Volumes

**Status:** Accepted · 2026-11-13

## Context

The budget is ₹0. Real SAP, Coupa and HICX systems aren't available. Databricks Free Edition has no inbound network path to a local machine, and Lakeflow Connect's managed connectors for those systems aren't usable here.

## Decision

Run source simulators locally in Docker Compose:

- Postgres with SAP-shaped tables, captured by **Debezium** into **Redpanda**
- A FastAPI **Coupa mock** with pagination and `updated_at` filtering
- A scheduled **HICX export** that writes Excel/CSV
- A **document generator** for PDFs

A Python **landing shipper** consumes these, writes micro-batches with a manifest (row count, checksum, watermark), and uploads them to `/Volumes/wizard/bronze/landing/` through the Databricks Files API. Auto Loader picks them up from there.

## Alternatives considered

- **Upload static CSVs once.** Simple, but it shows no incremental or CDC handling, which is a core skill this project is meant to demonstrate.
- **Kafka source in Databricks reading local Redpanda.** Needs a public tunnel to the laptop: fragile, and a security smell.
- **Generate data inside Databricks.** No real ingestion boundary, so less realistic.

## Consequences

- It matches a common enterprise pattern: on-prem agents pushing to a cloud landing zone.
- Manifests give a reconciliation check (sent vs. landed vs. loaded).
- The shipper is code Databricks Wizard has to maintain, so it stays small and stateless, with its watermarks kept in a local SQLite file.
- In production, the shipper would be replaced by Lakeflow Connect or a vendor CDC tool. The architecture doc notes this mapping.

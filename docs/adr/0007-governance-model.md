# 0007: Unity Catalog as the only governance layer

**Status:** Accepted · 2026-11-13

## Context

Supplier data includes PII and sensitive financial fields (bank accounts, tax IDs, contact details). People, BI tools and agents all read the same data. If access rules live in application code, they drift apart and can't be audited.

## Decision

All access control, masking, classification and lineage is in Unity Catalog:

- **Groups:** `wizard_engineers` (all layers), `wizard_stewards` (silver + gold, unmasked, review queue), `wizard_analysts` (gold, masked), and the `wizard_agent` service principal (gold + vector index, masked, read-only).
- **Column masks** on `tax_id`, `bank_account`, contact email and phone, using SQL UDFs that check `is_account_group_member('wizard_stewards')`.
- **Row filters** (demo): category managers see only their categories in `fact_po_line`.
- **Tags:** `pii`, `source_system`, `data_owner`, `certified`, applied by the pipelines.
- **Lineage:** from system tables, exposed to agents via `trace_metric`.
- **Audit:** the MCP server logs every tool call to `wizard.ops.agent_tool_calls`.

## Alternatives considered

- **Masking in the MCP server.** Easy to bypass through other access paths, and not visible to auditors.
- **Separate masked copies of tables.** Doubles storage and drifts out of sync.

## Consequences

- One policy applies to Genie, dashboards, notebooks and agents alike.
- Free Edition is a single-user workspace, so group-based behaviour is demonstrated with groups and service principals inside it. The write-up explains how this maps to a multi-workspace production setup.

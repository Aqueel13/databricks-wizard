# 0006: MCP as the agent interface

**Status:** Accepted · 2026-11-13

## Context

Several agents need governed supplier data: Intelligence WatchDog, general assistants such as Claude, and future agents. Each one integrating through its own REST client or SQL connection would copy query logic around and widen data access.

## Decision

Expose Databricks Wizard to agents through an **MCP server** hosted as a Databricks App. Its tools are read-only and typed (`search_suppliers`, `get_supplier_360`, `get_kpi`, `trace_metric`, `search_documents`, `explain_supplier_match`). It runs as the `wizard_agent` service principal, which has `SELECT` on gold tables and the vector index only.

Tools return data **plus provenance**: the SQL run, the tables read, and document citations.

## Alternatives considered

- **REST API (FastAPI).** Fine for apps, but every agent framework then needs a custom client and tool descriptions.
- **Agents with direct SQL access (text-to-SQL).** Flexible, but harder to govern and to evaluate. Genie covers this for people.
- **Databricks managed MCP servers (Genie, Vector Search, UC functions).** Worth using next to the custom server. The custom server is still needed for composite tools (`get_supplier_360`, `trace_metric`) and for consistent provenance.

## Consequences

- Any MCP client can use Databricks Wizard. The WatchDog integration is configuration, not code.
- The tool surface is small and testable, so the eval framework can cover it.
- Free Edition Apps stop after 24 hours, so the server is restarted before demos. A local run mode against the workspace is kept for development.

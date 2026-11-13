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

## Implementation (M7, 2026-11-13)

One Databricks App (`wizard-mcp`, Free Edition allows three) hosts **three MCP servers**, mirroring Intelligence
WatchDog's split by capability so one agent can use both systems the same way:

| Server | Tools |
|---|---|
| `/analytics/mcp` (read-only) | `list_metrics`, `query_metric` (definitions read live from the UC metric views; returns the SQL and `low_sample` flags), `find_suppliers` (governed listing), `search_suppliers` (any spelling or script, via the ER skeleton), `get_supplier_360`, `explain_supplier_match`, `trace_metric` (lineage snapshot), `simulate_consolidation` (what-if), `run_exploratory_sql` (labelled fallback, gold only) |
| `/knowledge/mcp` | `search_documents`, `answer_document_question` (cites `[file, p. N]`, says when not found), `get_document` (document vs system of record) |
| `/stewardship/mcp` | `list_review_items`, `propose_decision`, `list_proposals`: **agents propose, a person approves** in the steward app ("Agent proposals" tab); approval writes the decision under the person's name, citing the proposal |

- Every call is audited to `ops.agent_tool_calls` with the caller's identity (from the Apps proxy headers).
- The app's service principal has least-privilege grants (`scripts/grant_mcp_app.sh`): read gold and a few silver tables, write only audit and proposals.
- Clients authenticate with a Databricks OAuth token. For WatchDog in production: a service principal with an M2M OAuth secret and `CAN_USE` on the app.

Gotchas fixed on the way: the MCP SDK redirects `/x/mcp` to `/x/mcp/`, and behind the Apps proxy the redirect pointed at the container's internal address (`https://localhost:8000/...`), so the app serves both paths without a redirect; tool `Context` is not resolvable with `from __future__ import annotations` behind a decorator; the tool Context doesn't expose HTTP headers in this SDK version, so the caller identity is set per request in a ContextVar.

### Combined agent (`agents/wizard_agent.py`)
One agent loop over any set of MCP servers on a Databricks model endpoint. With WatchDog (via the SSH tunnel) it
has 32 read-only tools from 6 servers; tools that change state in another system (`propose_action`,
`execute_action`, `propose_decision`) are excluded unless explicitly allowed. The two projects model different
fictional companies, so the honest join point is the spend category: e.g. WatchDog finds the category with the worst
invoice-to-pay breach rate, Wizard lists the riskiest suppliers in it, and the answer attributes each fact to its system.

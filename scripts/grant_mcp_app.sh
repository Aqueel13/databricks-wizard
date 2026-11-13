#!/usr/bin/env bash
# Least-privilege Unity Catalog grants for the MCP app's service principal (idempotent).
# Read: gold (schema), the silver tables the tools need, the document index, lineage snapshot.
# Write: only the audit log and the proposals table. Agents can never write decisions directly.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABRICKS_CONFIG_PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}
CAT=${WIZARD_CATALOG:-wizard}
SP=$(databricks apps get wizard-mcp -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['service_principal_client_id'])")
SQL="uv run --project shipper -q python scripts/dbx_sql.py"
run() { $SQL "$1" >/dev/null && echo "  $1"; }
echo "granting to app service principal $SP"
run "GRANT USE CATALOG ON CATALOG $CAT TO \`$SP\`"
for s in silver gold ops; do run "GRANT USE SCHEMA ON SCHEMA $CAT.$s TO \`$SP\`"; done
run "GRANT SELECT ON SCHEMA $CAT.gold TO \`$SP\`"
for t in silver.supplier_records silver.er_clusters silver.er_review_queue silver.er_steward_decisions \
         silver.doc_parsed silver.doc_chunks_index ops.lineage_edges; do
  run "GRANT SELECT ON TABLE $CAT.$t TO \`$SP\`"
done
for t in ops.agent_tool_calls ops.agent_proposals; do run "GRANT SELECT, MODIFY ON TABLE $CAT.$t TO \`$SP\`"; done

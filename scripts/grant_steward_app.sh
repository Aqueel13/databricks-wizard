#!/usr/bin/env bash
# Least-privilege Unity Catalog grants for the steward app's service principal (idempotent).
# Read: review queue, scored pairs, supplier records, golden records, sanctions alerts. Write: the two decision tables.
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABRICKS_CONFIG_PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}
CAT=${WIZARD_CATALOG:-wizard}
SP=$(databricks apps get wizard-steward -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['service_principal_client_id'])")
SQL="uv run --project shipper -q python scripts/dbx_sql.py"
run() { $SQL "$1" >/dev/null && echo "  $1"; }
echo "granting to app service principal $SP"
run "GRANT USE CATALOG ON CATALOG $CAT TO \`$SP\`"
for s in silver gold; do run "GRANT USE SCHEMA ON SCHEMA $CAT.$s TO \`$SP\`"; done
for t in silver.er_review_queue silver.er_scored_pairs silver.supplier_records gold.dim_supplier gold.supplier_sanctions_hits; do
  run "GRANT SELECT ON TABLE $CAT.$t TO \`$SP\`"
done
for t in silver.er_steward_decisions silver.sanctions_alert_decisions; do
  run "GRANT SELECT, MODIFY ON TABLE $CAT.$t TO \`$SP\`"
done

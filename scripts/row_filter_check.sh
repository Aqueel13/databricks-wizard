#!/usr/bin/env bash
# M8 check: query gold.fact_invoice as a company-code-scoped service principal and print what it can see.
# Creates a short-lived OAuth secret for the SP, exchanges it for a token, runs one query, and deletes the secret.
# The secret and token are never printed.
#   scripts/row_filter_check.sh <sp_workspace_id> <sp_application_id>
set -euo pipefail
SPID=$1; APPID=$2; PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}
HOST=$(databricks auth describe --profile "$PROFILE" -o json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('details',{}).get('host') or d.get('host'))")
WH=$(databricks warehouses list --profile "$PROFILE" -o json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
J=$(databricks service-principal-secrets-proxy create "$SPID" --profile "$PROFILE" -o json)
SECID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "$J")
trap 'databricks service-principal-secrets-proxy delete "$SPID" "$SECID" --profile "$PROFILE" && echo "secret deleted"' EXIT
TOK=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['secret'])" "$J" | \
  { read -r SEC; curl -s -u "$APPID:$SEC" "$HOST/oidc/v1/token" -d grant_type=client_credentials -d scope=all-apis; } | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
unset J
curl -s -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' "$HOST/api/2.0/sql/statements" \
  -d "{\"warehouse_id\":\"$WH\",\"wait_timeout\":\"50s\",\"statement\":\"select company_code, count(*) n from wizard.gold.fact_invoice group by 1 order by 1\"}" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status']['state'], d.get('result',{}).get('data_array'), d['status'].get('error',{}).get('message',''))"

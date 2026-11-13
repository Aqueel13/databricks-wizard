#!/usr/bin/env bash
# Give the wizard-ci service principal CAN_MANAGE on every resource the bundle deploys, so GitHub Actions can
# update them. Additive (permissions update, not set): owners, run-as identities and other grants are unchanged.
# Run once, by an admin, after the first deploy (and again if a resource is recreated).
#   scripts/grant_ci_sp.sh [ci_sp_application_id]
set -euo pipefail
SP=${1:-84c149b5-1d4a-4c35-bef0-434059df4e8c}; PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}
cd "$(dirname "$0")/.."
ACL="{\"access_control_list\":[{\"service_principal_name\":\"$SP\",\"permission_level\":\"CAN_MANAGE\"}]}"
databricks bundle summary -o json --profile "$PROFILE" | python3 -c "
import json,sys
kinds={'jobs':'jobs','pipelines':'pipelines','apps':'apps','dashboards':'dashboards'}
for k,v in json.load(sys.stdin)['resources'].items():
  for r in v.values():
    if k in kinds and r.get('id'): print(kinds[k], r['id'])" | while read -r kind id; do
  databricks permissions update "$kind" "$id" --json "$ACL" --profile "$PROFILE" -o json >/dev/null && echo "CAN_MANAGE on $kind/$id"
done
WH=$(databricks warehouses list --profile "$PROFILE" -o json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
databricks permissions update warehouses "$WH" --json "${ACL/CAN_MANAGE/CAN_USE}" --profile "$PROFILE" -o json >/dev/null && echo "CAN_USE on warehouses/$WH"

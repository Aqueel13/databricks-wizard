#!/usr/bin/env bash
# Workspace groups behind the Unity Catalog policies in pipelines/governance.sql (idempotent).
#   wizard_stewards  - unmasked PII, all rows: data stewards and the steward app
#   wizard_analysts  - masked PII, all rows: analysts and the MCP app (agents)
#   wizard_cc_<code> - masked PII, one company code's invoices (row filter); wizard_cc_pl01 holds a test SP
# Policies check is_member(), i.e. workspace groups. Table grants stay per principal: Unity Catalog grants need
# account groups, which Free Edition does not offer.
#   scripts/groups_setup.sh [steward_user_email]
set -euo pipefail
PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}; cd /tmp
db() { databricks "$@" --profile "$PROFILE"; }
group_id() { db groups list -o json | python3 -c "import json,sys; print(next((g['id'] for g in json.load(sys.stdin) if g['displayName']=='$1'), ''))"; }
ensure_group() { local id; id=$(group_id "$1"); [ -n "$id" ] || id=$(db groups create --display-name "$1" -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"); echo "$id"; }
sp_id() { db service-principals list -o json | python3 -c "import json,sys; print(next((s['id'] for s in json.load(sys.stdin) if s['displayName'] in ('$1', 'app-2cg4i4 $1') or s['displayName'].endswith(' $1')), ''))"; }
add() {  # add <group> <member id> <label>
  [ -n "$2" ] || { echo "  skip $3 (not found)"; return; }
  db groups patch "$1" --json "{\"schemas\":[\"urn:ietf:params:scim:api:messages:2.0:PatchOp\"],\"Operations\":[{\"op\":\"add\",\"path\":\"members\",\"value\":[{\"value\":\"$2\"}]}]}"
  echo "  $3"
}
S=$(ensure_group wizard_stewards); A=$(ensure_group wizard_analysts); P=$(ensure_group wizard_cc_pl01)
echo "wizard_stewards:"; add "$S" "$(sp_id wizard-steward)" "steward app"
if [ -n "${1:-}" ]; then add "$S" "$(db users list --filter "userName eq \"$1\"" -o json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")" "$1"; fi
echo "wizard_analysts:"; add "$A" "$(sp_id wizard-mcp)" "MCP app (agents)"
echo "wizard_cc_pl01:"; add "$P" "$(sp_id wizard-test-pl01)" "wizard-test-pl01 (row-filter test)"

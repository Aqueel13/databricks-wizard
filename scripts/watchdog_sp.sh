#!/usr/bin/env bash
# Service principal for Intelligence WatchDog to call the Wizard MCP servers as itself (idempotent).
# It gets CAN_USE on the wizard-mcp app and nothing else: the app reads data with its own least-privilege identity
# (grant_mcp_app.sh) and records the caller in ops.agent_tool_calls, so WatchDog's calls are attributable.
#   scripts/watchdog_sp.sh                       # create + grant
#   scripts/watchdog_sp.sh --write-env <file>    # also mint an OAuth secret into <file> (chmod 600, never printed)
set -euo pipefail
PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}; NAME=wizard-watchdog
cd /tmp   # outside the bundle folder so the CLI uses the profile, not bundle auth
db() { databricks "$@" --profile "$PROFILE"; }
read -r ID APPID < <(db service-principals list -o json | python3 -c "
import json,sys; print(*next(([s['id'], s['applicationId']] for s in json.load(sys.stdin) if s['displayName']=='$NAME'), ['', '']))")
if [ -z "$ID" ]; then
  read -r ID APPID < <(db service-principals create --display-name "$NAME" -o json | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['id'], s['applicationId'])")
  db service-principals patch "$ID" --json '{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"entitlements","value":[{"value":"workspace-access"}]}]}'
  echo "created $NAME ($APPID)"
fi
db permissions update apps wizard-mcp --json "{\"access_control_list\":[{\"service_principal_name\":\"$APPID\",\"permission_level\":\"CAN_USE\"}]}" -o json >/dev/null
echo "$NAME ($APPID): CAN_USE on app wizard-mcp"
if [ "${1:-}" = "--write-env" ]; then
  OUT=${2:?file}; umask 077
  HOST=$(db auth describe -o json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('details',{}).get('host') or d.get('host'))")
  URL=$(db apps get wizard-mcp -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")
  db service-principal-secrets-proxy create "$ID" --lifetime 31536000s -o json | python3 -c "
import json,sys; s=json.load(sys.stdin)
print(f'DATABRICKS_HOST=$HOST\nWIZARD_MCP_URL=$URL\nDATABRICKS_CLIENT_ID=$APPID\nDATABRICKS_CLIENT_SECRET={s[\"secret\"]}')" > "$OUT"
  echo "credentials written to $OUT (secret expires in 1 year)"
fi

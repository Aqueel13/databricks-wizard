#!/usr/bin/env bash
# M3 exit test: out-of-order and late CDC events are applied in sequence order, not arrival order.
# Ships three phases for test vendor ZZOOO00001 and checks silver after each pipeline run.
set -euo pipefail
cd "$(dirname "$0")/.."
SQL="uv run --project shipper -q python scripts/dbx_sql.py"
CHECK="select coalesce(name1,'-') name1, coalesce(cast(__START_AT.source_lsn - 9000000000000000 as string),'-') start_seq, coalesce(cast(__END_AT.source_lsn - 9000000000000000 as string),'open') end_seq from wizard.silver.sap_lfa1 where lifnr = 'ZZOOO00001' order by __START_AT"

for phase in 1 2 3; do
  uv run --project shipper -q wizard-shipper scenario out-of-order --phase "$phase"
  databricks bundle run wizard_ingest >/dev/null 2>&1   # queued behind any file-triggered run
  echo "--- silver.sap_lfa1 history after phase $phase"
  $SQL "$CHECK"
done

#!/usr/bin/env bash
# Create the Unity Catalog objects the bundle relies on. Idempotent.
#   catalog wizard; schemas bronze, silver, gold, ops; volumes bronze.landing, ops.ground_truth
set -euo pipefail
CAT=${WIZARD_CATALOG:-wizard}
export DATABRICKS_CONFIG_PROFILE=${DATABRICKS_CONFIG_PROFILE:-wizard}

exists() { "$@" >/dev/null 2>&1; }

exists databricks catalogs get "$CAT" || { echo "create catalog $CAT"; databricks catalogs create "$CAT" >/dev/null; }
for s in bronze silver gold ops; do
  exists databricks schemas get "$CAT.$s" || { echo "create schema $CAT.$s"; databricks schemas create "$s" "$CAT" >/dev/null; }
done
exists databricks volumes read "$CAT.bronze.landing" || { echo "create volume $CAT.bronze.landing"; databricks volumes create "$CAT" bronze landing MANAGED >/dev/null; }
exists databricks volumes read "$CAT.ops.ground_truth" || { echo "create volume $CAT.ops.ground_truth"; databricks volumes create "$CAT" ops ground_truth MANAGED >/dev/null; }
echo "Unity Catalog objects ready in $CAT"

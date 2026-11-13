"""Run SQL on the workspace's SQL warehouse and print the result as a table.

  uv run --project shipper python scripts/dbx_sql.py "select 1"
  uv run --project shipper python scripts/dbx_sql.py -f query.sql
"""
from __future__ import annotations

import argparse
import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("sql", nargs="?")
    p.add_argument("-f", "--file")
    a = p.parse_args()
    sql = open(a.file).read() if a.file else a.sql
    w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
    wh = os.environ.get("WIZARD_WAREHOUSE_ID") or next(iter(w.warehouses.list())).id
    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=wh, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        sys.exit(f"{r.status.state}: {r.status.error.message if r.status.error else ''}")
    cols = [c.name for c in r.manifest.schema.columns]
    rows = (r.result.data_array or []) if r.result else []
    widths = [max(len(str(x)) for x in [c] + [row[i] for row in rows]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w_) for c, w_ in zip(cols, widths)))
    for row in rows:
        print("  ".join(str(x).ljust(w_) for x, w_ in zip(row, widths)))


if __name__ == "__main__":
    main()

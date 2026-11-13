"""Walk Unity Catalog lineage upstream from a table to its sources (landing files included).

  uv run --project shipper python scripts/lineage_trace.py wizard.gold.supplier_360
  uv run --project shipper python scripts/lineage_trace.py wizard.gold.supplier_360 --check   # exit 1 if broken

--check verifies the chain is complete: every table reached must have at least one recorded upstream, except the
roots by design: bronze (fed from landing files), static reference tables and human decision tables written by
the steward app. Any other table without an upstream edge means lineage is broken there.
"""
from __future__ import annotations

import argparse
import os
import sys

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
WH = os.environ.get("WIZARD_WAREHOUSE_ID") or next(iter(w.warehouses.list())).id


def upstream(tables: list[str]) -> dict[str, set[str]]:
    names = ", ".join(f"'{t}'" for t in tables)
    r = w.statement_execution.execute_statement(warehouse_id=WH, wait_timeout="50s", statement=f"""
        SELECT target_table_full_name, coalesce(source_table_full_name, concat('path:', source_path))
        FROM system.access.table_lineage
        WHERE target_table_full_name IN ({names})
          AND coalesce(source_table_full_name, source_path) IS NOT NULL
        GROUP BY ALL""")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        r = w.statement_execution.get_statement(r.statement_id)
    out: dict[str, set[str]] = {t: set() for t in tables}
    for tgt, src in (r.result.data_array or []) if r.result else []:
        if src != tgt:
            out[tgt].add(src)
    return out


def trace(start: str, show=print) -> tuple[set[str], list[str]]:
    """Walk upstream from start; returns (tables reached, tables with no recorded upstream that should have one)."""
    seen, frontier, depth, broken = {start}, [start], 0, []
    while frontier:
        ups = upstream(frontier)
        nxt = []
        for t in frontier:
            srcs = sorted(ups.get(t, set()))
            label = ", ".join(s if not s.startswith("path:") else s[5:].split("/landing/")[-1].split("/")[0] + " (landing files)"
                              for s in srcs[:8]) + (f" (+{len(srcs) - 8} more)" if len(srcs) > 8 else "")
            show(f"{'  ' * depth}{t}  <-  {label or 'NO UPSTREAM RECORDED'}")
            tables = [s for s in srcs if not s.startswith("path:")]
            # roots by design: bronze (fed from landing files), static reference MVs, human decisions (steward app)
            root = ".bronze." in t or t.endswith(("dim_fx", "dim_category", "_decisions"))
            if not srcs and not root:
                broken.append(t)
            for s in tables:
                if s not in seen:
                    seen.add(s)
                    nxt.append(s)
        frontier, depth = nxt, depth + 1
    return seen, broken


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("table")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    seen, broken = trace(a.table)
    print(f"\n{len(seen)} tables reached; broken links: {broken or 'none'}")
    if a.check and broken:
        sys.exit(1)


if __name__ == "__main__":
    main()

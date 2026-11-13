"""M8 exit test: lineage traced end to end, at two levels.

1. Table level (Unity Catalog lineage): from every metric view and Supplier 360, walk upstream to bronze. Every
   table reached must have a recorded upstream except roots by design (see lineage_trace.py), and every walk
   must reach bronze tables of all three source systems (SAP, Coupa, HICX).
2. Row level (lineage columns): Unity Catalog does not record Auto Loader's landing files as upstream of bronze,
   so the last hop is proven per row. For sampled invoices in gold.fact_invoice from each source, follow the row to
   its silver row (_batch_id), its bronze row, the landing file it was read from (_file), and the shipper
   manifest that declared that file (landing_reconciliation, status OK). Every sampled row must resolve.

  uv run --project shipper python scripts/lineage_e2e_check.py      # exit 1 on any failure
"""
from __future__ import annotations

import os
import sys

from databricks.sdk.service.sql import StatementState

sys.path.insert(0, os.path.dirname(__file__))
from lineage_trace import WH, trace, w  # noqa: E402

STARTS = ["wizard.gold.metrics_spend", "wizard.gold.metrics_delivery", "wizard.gold.metrics_supplier",
          "wizard.gold.supplier_360"]
SAMPLE = 25

ROW_SQL = {
    "sap": f"""
    WITH g AS (SELECT invoice_key, amount_local FROM wizard.gold.fact_invoice WHERE source = 'sap'
               ORDER BY xxhash64(invoice_key) LIMIT {SAMPLE}),
    s AS (SELECT g.invoice_key, r.source_lsn, r._batch_id FROM g LEFT JOIN wizard.silver.sap_rbkp r
          ON concat(r.belnr, '/', r.gjahr) = g.invoice_key),
    b AS (SELECT s.*, min(c._file) AS _file FROM s LEFT JOIN wizard.bronze.sap_rbkp_cdc c
          ON c.source_lsn = s.source_lsn AND c._batch_id = s._batch_id GROUP BY ALL)
    SELECT b.invoice_key, b._batch_id, b._file, m.source, m.status
    FROM b LEFT JOIN wizard.bronze.landing_reconciliation m ON m._file = b._file AND m.batch_id = b._batch_id""",
    "coupa": f"""
    WITH g AS (SELECT invoice_key FROM wizard.gold.fact_invoice WHERE source = 'coupa'
               ORDER BY xxhash64(invoice_key) LIMIT {SAMPLE}),
    s AS (SELECT g.invoice_key, i.id, i._batch_id FROM g LEFT JOIN wizard.silver.coupa_invoices i
          ON concat(i.invoice_number, '#', i.id) = g.invoice_key),
    b AS (SELECT s.*, min(c._file) AS _file FROM s LEFT JOIN wizard.bronze.coupa_invoices c
          ON c.id = s.id AND c._batch_id = s._batch_id GROUP BY ALL)
    SELECT b.invoice_key, b._batch_id, b._file, m.source, m.status
    FROM b LEFT JOIN wizard.bronze.landing_reconciliation m ON m._file = b._file AND m.batch_id = b._batch_id""",
}


def rows(sql: str) -> list[list]:
    r = w.statement_execution.execute_statement(warehouse_id=WH, wait_timeout="50s", statement=sql)
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else r.status.state)
    return (r.result.data_array or []) if r.result else []


def main() -> None:
    failures = []
    print("== table level (Unity Catalog lineage)")
    for start in STARTS:
        seen, broken = trace(start, show=lambda _: None)
        systems = {s for s in ("sap", "coupa", "hicx") if any(t.startswith(f"wizard.bronze.{s}") for t in seen)}
        ok = not broken and systems == {"sap", "coupa", "hicx"}
        print(f"  {'PASS' if ok else 'FAIL'} {start}: {len(seen)} tables, source systems {sorted(systems)}, "
              f"broken {broken or 'none'}")
        if not ok:
            failures.append(start)
    print("== row level (gold row -> silver -> bronze -> landing file -> shipper manifest)")
    for source, sql in ROW_SQL.items():
        rs = rows(sql)
        bad = [r for r in rs if not (r[2] and r[3] == source and r[4] == "OK")]
        print(f"  {'PASS' if rs and not bad else 'FAIL'} {source}: {len(rs) - len(bad)}/{len(rs)} sampled invoices "
              f"traced to a manifest-declared landing file")
        if rs:
            k, batch, f, _, _ = rs[0]
            print(f"    e.g. invoice {k} <- batch {batch} <- {(f or '?').split('/landing/')[-1]}")
        for r in bad[:3]:
            print(f"    unresolved: {r}")
        if not rs or bad:
            failures.append(source)
    print(f"\n{'PASS' if not failures else 'FAIL: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

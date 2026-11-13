"""M5 exit test: ask the Genie space the four core business questions and score the answers against gold.

For each question: Genie's generated SQL is executed on the warehouse, the answer's suppliers (or categories)
are compared with the expected set computed directly from the gold tables, and precision / recall are reported.
Results go to ops.genie_eval and are printed with Genie's SQL for review.

  uv run --project shipper python scripts/m5_genie_eval.py
"""
from __future__ import annotations

import json
import os
import re
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

TITLE = "Supplier 360 (Databricks Wizard)"
CASES = [
    ("Which suppliers have deteriorating delivery performance?",
     "select supplier_id, legal_name from wizard.gold.supplier_360 where deteriorating", "supplier"),
    ("Which suppliers have elevated compliance risk?",
     "select supplier_id, legal_name from wizard.gold.supplier_360 where compliance_risk >= 50", "supplier"),
    ("What is our spend concentration by category?",
     "select category_id, category_name from wizard.gold.spend_concentration", "category"),
    ("Which suppliers have incomplete or inconsistent master data?",
     "select supplier_id, legal_name from wizard.gold.supplier_dq_scorecard q join wizard.gold.supplier_360 s "
     "using (supplier_id) where q.has_issues", "supplier"),
]

w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
WH = os.environ.get("WIZARD_WAREHOUSE_ID") or next(iter(w.warehouses.list())).id


def sql(q: str) -> tuple[list[str], list[list]]:
    r = w.statement_execution.execute_statement(statement=q, warehouse_id=WH, wait_timeout="50s",
                                                row_limit=100000)
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else r.status.state)
    cols = [c.name for c in r.manifest.schema.columns]
    return cols, (r.result.data_array or []) if r.result else []


def keys(cols, rows, kind):
    """Pull supplier ids / names (or category ids / names) out of Genie's result, whatever it named them."""
    out = set()
    for row in rows:
        for c, v in zip(cols, row):
            if v is None:
                continue
            c = c.lower()
            if kind == "supplier" and (c in ("supplier_id", "legal_name", "supplier") or c.endswith("supplier_id")):
                out.add(str(v).strip())
            if kind == "category" and (c in ("category_id", "category_name", "category") or "category" in c):
                out.add(str(v).strip())
    return out


def main() -> None:
    sid = next(s["space_id"] for s in w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces", [])
               if s.get("title") == TITLE)
    results = []
    for question, expected_sql, kind in CASES:
        ecols, erows = sql(expected_sql)
        expected_ids = {str(r[0]) for r in erows}
        expected_any = expected_ids | {str(r[1]) for r in erows}
        t = time.time()
        msg = w.genie.start_conversation_and_wait(space_id=sid, content=question)
        gsql = next((a.query.query for a in (msg.attachments or []) if a.query and a.query.query), None)
        text = " ".join(a.text.content for a in (msg.attachments or []) if a.text and a.text.content)
        got, n_rows = set(), 0
        if gsql:
            gcols, grows = sql(gsql)
            n_rows = len(grows)
            got = keys(gcols, grows, kind)
        # a returned row counts if its id or its name is in the expected set
        hits = {g for g in got if g in expected_any}
        found = {r[0] for r in erows if str(r[0]) in got or str(r[1]) in got}
        precision = len(hits) / len(got) if got else 0.0
        recall = len(found) / len(expected_ids) if expected_ids else 0.0
        res = dict(question=question, expected=len(expected_ids), genie_rows=n_rows, precision=round(precision, 3),
                   recall=round(recall, 3), passed=precision >= 0.9 and recall >= 0.9, seconds=round(time.time() - t),
                   genie_sql=gsql, genie_text=text[:500])
        results.append(res)
        print(f"\n=== {question}\n  expected {len(expected_ids)} | genie rows {n_rows} | precision {precision:.2f} "
              f"| recall {recall:.2f} | {'PASS' if res['passed'] else 'FAIL'} ({res['seconds']}s)")
        print("  SQL:", re.sub(r"\s+", " ", gsql or "(none)")[:600])
        if text:
            print("  text:", re.sub(r"\s+", " ", text)[:300])
    payload = json.dumps(results).replace("'", "''")
    sql(f"""create table if not exists wizard.ops.genie_eval (run_at timestamp, results string)""")
    sql(f"""insert into wizard.ops.genie_eval values (current_timestamp(), '{payload}')""")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} questions passed")


if __name__ == "__main__":
    main()

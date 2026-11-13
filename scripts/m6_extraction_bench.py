"""ADR 0005 benchmark 3: document field extraction, scored per field against the document answer key.

  uv run --project shipper python scripts/m6_extraction_bench.py [--n 40]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipelines"))
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.sql import StatementState  # noqa: E402
from doc_extraction import ai_query_sql  # noqa: E402

MODELS = ["databricks-qwen3-next-80b-a3b-instruct", "databricks-meta-llama-3-3-70b-instruct",
          "databricks-meta-llama-3-1-8b-instruct", "databricks-llama-4-maverick"]
GT = "parquet.`/Volumes/wizard/ops/ground_truth/gt_document.parquet`"
w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
WH = next(iter(w.warehouses.list())).id


def sql(q: str):
    r = w.statement_execution.execute_statement(statement=q, warehouse_id=WH, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else r.status.state)
    return [c.name for c in r.manifest.schema.columns], (r.result.data_array or []) if r.result else []


def norm(c: str) -> str:  # normalized text comparison
    return f"lower(regexp_replace(coalesce({c}, ''), '[^\\\\p{{L}}\\\\p{{N}}]', ''))"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--score-only", action="store_true", help="re-score existing extractions without model calls")
    a = ap.parse_args()
    if not a.score_only:
        extract(a.n)
    score()


def extract(n: int) -> None:
    sql(f"""create or replace table wizard.ops.bench_doc_sample as
            (select * from wizard.silver.doc_parsed where doc_type = 'certificate' order by doc_id limit {n})
            union all
            (select * from wizard.silver.doc_parsed where doc_type = 'contract' order by doc_id limit {n})""")
    sql("create or replace table wizard.ops.bench_doc_extraction (model string, doc_id string, doc_type string, "
        "extracted string, seconds double)")
    for m in MODELS:
        for dt in ("certificate", "contract"):
            t = time.time()
            sql(f"""insert into wizard.ops.bench_doc_extraction
                    select '{m}', doc_id, doc_type, {ai_query_sql(m, dt)}, 0 from wizard.ops.bench_doc_sample
                    where doc_type = '{dt}'""")
            print(f"{m:45s} {dt:12s} {time.time() - t:5.0f}s", flush=True)


def score() -> None:
    cols, rows = sql(f"""
      with g as (select regexp_extract(path, '([^/]+)$', 1) file_name, * from {GT}),
      x as (select e.model, s.doc_type, s.file_name, try_parse_json(e.extracted) j, g.* except (doc_type, file_name)
            from wizard.ops.bench_doc_extraction e join wizard.ops.bench_doc_sample s using (doc_id)
            join g on g.file_name = s.file_name),
      f as (
        select model, doc_type, scanned, field, ok from x lateral view explode(map(
          'holder/supplier', {norm("coalesce(j:holder::string, j:supplier::string)")} = {norm("holder")},
          'number', {norm("coalesce(j:certificate_number::string, j:agreement_number::string)")} = {norm("doc_key")},
          'type', doc_type = 'contract' or {norm("j:certificate_type::string")} = {norm("cert_type")},
          'issuer', doc_type = 'contract' or {norm("j:issuer::string")} = {norm("issuer")},
          'start/issue date', try_cast(coalesce(j:issue_date::string, j:start_date::string) as date) = issue_date,
          'end/expiry date', try_cast(coalesce(j:expiry_date::string, j:end_date::string) as date) = expiry_date,
          'contract value', doc_type = 'certificate' or abs(j:contract_value::double - contract_value) < 0.01,
          'currency', doc_type = 'certificate' or {norm("j:currency::string")} = {norm("currency")},
          'payment terms', doc_type = 'certificate' or j:payment_terms_days::int = payment_terms_days,
          'notice days', doc_type = 'certificate' or j:notice_days::int = notice_days,
          'governing law', doc_type = 'certificate' or {norm("j:governing_law::string")} like concat('%', {norm("governing_law")}, '%')
        )) t as field, ok)
      select regexp_replace(model, 'databricks-', '') model,
             round(avg(cast(coalesce(ok, false) as int)), 3) all_fields,
             round(avg(case when doc_type='certificate' and not scanned then cast(coalesce(ok,false) as int) end), 3) cert_text,
             round(avg(case when doc_type='certificate' and scanned then cast(coalesce(ok,false) as int) end), 3) cert_scanned,
             round(avg(case when doc_type='contract' then cast(coalesce(ok,false) as int) end), 3) contract,
             round(avg(case when field='end/expiry date' then cast(coalesce(ok,false) as int) end), 3) expiry_date,
             round(avg(case when field='holder/supplier' then cast(coalesce(ok,false) as int) end), 3) holder_supplier
      from f group by 1 order by all_fields desc""")
    print("\n" + "  ".join(f"{c:>15s}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(v):>15s}" for v in r))


if __name__ == "__main__":
    main()

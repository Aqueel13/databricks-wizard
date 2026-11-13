"""ADR 0005 benchmark 5: clause extraction from real contracts (CUAD), scored against expert answers.

The supplier contracts in this project are generated, so benchmark 3 shows extraction works when the answer key is
ours. CUAD tests the same approach on 100 real, messy commercial contracts (EDGAR filings, converted from PDF).

  uv run --project shipper python scripts/cuad_bench.py prepare        # pick the sample, upload texts (no compute)
  uv run --project shipper python scripts/cuad_bench.py extract [--models a,b]
  uv run --project shipper python scripts/cuad_bench.py score          # -> data/cuad_benchmark.json

Needs data/raw/cuad (make data). CUAD: The Atticus Project, CC BY 4.0. Contract text is never committed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

sys.path.insert(0, os.path.dirname(__file__))
from cuad_score import FIELDS, PROMPT, VALUES, YES_NO, response_format, score_field, summarize  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CUAD = ROOT / "data/raw/cuad/CUAD_v1"
VOL = "/Volumes/wizard/ops/ground_truth/cuad"
N, MAX_CHARS = 100, 80_000  # 410 of 510 contracts are under 80k characters (~20k tokens)
MODELS = ["databricks-llama-4-maverick", "databricks-qwen3-next-80b-a3b-instruct",
          "databricks-meta-llama-3-3-70b-instruct", "databricks-gpt-oss-120b"]
OUT = ROOT / "data/cuad_benchmark.json"

w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))


def sql(q: str):
    wh = os.environ.get("WIZARD_WAREHOUSE_ID") or next(iter(w.warehouses.list())).id
    r = w.statement_execution.execute_statement(statement=q, warehouse_id=wh, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(5)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else r.status.state)
    return (r.result.data_array or []) if r.result else []


def key(name: str) -> str:
    """CSV names end in .pdf/.PDF (one with a stray quote); text files in .txt."""
    return re.sub(r"\.(pdf|txt)'?$", "", name, flags=re.I)


def labels() -> dict[str, dict]:
    with open(CUAD / "master_clauses.csv", encoding="utf-8") as f:
        return {key(r["Filename"]): r for r in csv.DictReader(f)}


def sample() -> list[Path]:
    """Deterministic: the N labelled contracts under MAX_CHARS with the smallest SHA-1 of their name."""
    lab = labels()
    files = [p for p in (CUAD / "full_contract_txt").glob("*.txt")
             if key(p.name) in lab and len(p.read_text(encoding="utf-8", errors="replace")) <= MAX_CHARS]
    return sorted(files, key=lambda p: hashlib.sha1(p.name.encode()).hexdigest())[:N]


def prepare() -> None:
    files = sample()
    for i, p in enumerate(files):
        # ASCII object names: some CUAD names have quotes, ampersands and brackets
        name = hashlib.sha1(p.name.encode()).hexdigest()[:16] + ".txt"
        w.files.upload(f"{VOL}/txt/{name}", io.BytesIO(p.read_bytes()), overwrite=True)
        print(f"\r{i + 1}/{len(files)}", end="", flush=True)
    index = "\n".join(f"{hashlib.sha1(p.name.encode()).hexdigest()[:16]}.txt\t{key(p.name)}" for p in files)
    w.files.upload(f"{VOL}/index.tsv", io.BytesIO(index.encode()), overwrite=True)
    print(f"\nuploaded {len(files)} contracts to {VOL}")


def extract(models: list[str]) -> None:
    sql(f"""create or replace table wizard.ops.cuad_docs as
            select regexp_extract(_metadata.file_path, '([^/]+)$', 1) object_name, value as text
            from read_files('{VOL}/txt', format => 'text', wholeText => true)""")
    sql("create table if not exists wizard.ops.cuad_extraction (model string, object_name string, response string, "
        "error string, seconds double)")
    rf, prompt = response_format().replace("'", "''"), PROMPT.replace("'", "''")
    for m in models:
        sql(f"delete from wizard.ops.cuad_extraction where model = '{m}'")
        t = time.time()
        sql(f"""insert into wizard.ops.cuad_extraction
                select '{m}', object_name, r.result, r.errorMessage, null from (
                  select object_name, ai_query('{m}', concat('{prompt}\\n\\nCONTRACT:\\n', text),
                         responseFormat => '{rf}', failOnError => false) r
                  from wizard.ops.cuad_docs)""")
        print(f"{m:45s} {time.time() - t:5.0f}s", flush=True)


def score() -> None:
    idx = dict(line.split("\t") for line in
               w.files.download(f"{VOL}/index.tsv").contents.read().decode().splitlines())
    lab = labels()
    rows, errors = [], {}
    for model, obj, resp in sql("select model, object_name, response from wizard.ops.cuad_extraction"):
        try:
            pred = json.loads(resp) if resp else None
        except json.JSONDecodeError:
            pred = None
        if pred is None:
            errors[model] = errors.get(model, 0) + 1
            pred = {}
        lr = lab[idx[obj]]
        for f in FIELDS:
            col = (YES_NO.get(f) or VALUES[f])[0]
            ok = score_field(f, lr.get(col), pred.get(f))
            rows.append(dict(model=model, field=f, ok=ok, label_yes=str(lr.get(col)).strip().lower() == "yes",
                             pred_yes=bool(pred.get(f))))
    res = summarize(rows)
    for m in res:
        res[m]["failed_responses"] = errors.get(m, 0)
    OUT.write_text(json.dumps(res, indent=2))
    print(f"{'model':42s} {'all':>5s} {'yes P':>6s} {'yes R':>6s} {'fail':>4s}  " + " ".join(f[:8] for f in FIELDS))
    for m, r in sorted(res.items(), key=lambda kv: -(kv[1]["accuracy"] or 0)):
        fmt = lambda x: f"{x:.2f}" if x is not None else "  - "  # noqa: E731
        print(f"{m:42s} {fmt(r['accuracy']):>5s} {fmt(r['yes_precision']):>6s} {fmt(r['yes_recall']):>6s} "
              f"{r['failed_responses']:>4d}  " + " ".join(f"{fmt(r['fields'].get(f)):>8s}" for f in FIELDS))
    print(f"-> {OUT.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["prepare", "extract", "score"])
    ap.add_argument("--models", default=",".join(MODELS))
    a = ap.parse_args()
    {"prepare": prepare, "extract": lambda: extract(a.models.split(",")), "score": score}[a.step]()


if __name__ == "__main__":
    main()

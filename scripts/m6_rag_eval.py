"""M6 exit test: cited answers to document questions, scored against the document answer key.

Questions are generated from gt_document (the true field values printed in each PDF). 30 name the document
number; 10 don't ("when does <holder>'s <type> certificate expire?"), so retrieval must work semantically.
Scores: retrieval hit@5 (right document retrieved), answer correct (true value in the answer), citation
correct (the right document cited). Results go to ops.rag_eval.

  uv run --project shipper python scripts/m6_rag_eval.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from doc_qa import answer, client  # noqa: E402

GT = "/Volumes/wizard/ops/ground_truth/gt_document.parquet"


def questions() -> list[dict]:
    g = pd.read_parquet(io.BytesIO(client().files.download(GT).contents.read()))
    g["file_name"] = g["path"].str.extract(r"([^/]+)$")[0]
    g["h"] = g["file_name"].map(lambda s: hashlib.sha1(s.encode()).hexdigest())
    g = g.sort_values("h")
    certs, cons = g[g.doc_type == "certificate"], g[g.doc_type == "contract"]
    qs = []
    for _, r in certs.head(15).iterrows():
        qs.append(dict(q=f"When does certificate {r.doc_key} held by {r.holder} expire?", file=r.file_name,
                       expect=[str(r.expiry_date)[:10]], kind="certificate expiry (by number)"))
    # no document number: semantic retrieval; only holders with a single certificate of that type
    uniq = certs.groupby(["holder", "cert_type"]).filter(lambda x: len(x) == 1).iloc[15:25]
    for _, r in uniq.iterrows():
        qs.append(dict(q=f"When does the {r.cert_type} certificate of {r.holder} expire?", file=r.file_name,
                       expect=[str(r.expiry_date)[:10]], kind="certificate expiry (no number)"))
    for i, (_, r) in enumerate(cons.head(15).iterrows()):
        if i % 3 == 0:
            qs.append(dict(q=f"What are the payment terms in agreement {r.doc_key} with {r.holder}?",
                           file=r.file_name, expect=[f"{int(r.payment_terms_days)} days"], kind="contract payment terms"))
        elif i % 3 == 1:
            qs.append(dict(q=f"Which law governs agreement {r.doc_key}?", file=r.file_name,
                           expect=[r.governing_law], kind="contract governing law"))
        else:
            qs.append(dict(q=f"How many days' notice are needed to terminate agreement {r.doc_key} for convenience?",
                           file=r.file_name, expect=[f"{int(r.notice_days)} days"], kind="contract notice period"))
    return qs


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main() -> None:
    qs = questions()
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda q: answer(q["q"]), qs))
    rows = []
    for q, r in zip(qs, res, strict=True):
        rows.append(dict(kind=q["kind"], question=q["q"], expected=q["expect"][0], answer=r["answer"],
                         retrieved=q["file"] in r["retrieved"],
                         correct=any(norm(e) in norm(r["answer"]) for e in q["expect"]),
                         cited=q["file"] in r["citations"]))
    df = pd.DataFrame(rows)
    summary = df.groupby("kind")[["retrieved", "correct", "cited"]].mean().round(3)
    summary.loc["ALL"] = df[["retrieved", "correct", "cited"]].mean().round(3)
    print(summary.to_string())
    for _, r in df[~(df.correct & df.cited)].head(6).iterrows():
        print(f"\nMISS [{r.kind}] {r.question}\n  expected {r.expected} | retrieved={r.retrieved}\n  answer: {r.answer[:240]}")
    w = client()
    wh = next(iter(w.warehouses.list())).id
    payload = json.dumps(rows).replace("'", "''")
    for q in ("create table if not exists wizard.ops.rag_eval (run_at timestamp, results string)",
              f"insert into wizard.ops.rag_eval values (current_timestamp(), '{payload}')"):
        w.statement_execution.execute_statement(statement=q, warehouse_id=wh, wait_timeout="50s")


if __name__ == "__main__":
    main()

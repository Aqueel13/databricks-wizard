"""Local development runner: run the matcher on an export of silver.supplier_records and score it.

  wizard-er eval [--llm]    (inputs in data/er_dev/, answer key in data/stage/)
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import pandas as pd

from .cluster import cluster, decide
from .evaluate import pairwise
from .matching import prepare, score

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "data" / "er_dev"
STAGE = ROOT / "data" / "stage"


def load() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    rec = duckdb.sql(f"select * from '{DEV}/supplier_records/*.parquet'").df()
    emb = duckdb.sql(f"select * from '{DEV}/er_name_embeddings/*.parquet'").df()
    gt = duckdb.sql(f"select source_system || ':' || source_id u, true_supplier_id t from '{STAGE}/gt_source_record.parquet'").df()
    return rec, emb, dict(zip(gt["u"], gt["t"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["eval"])
    ap.add_argument("--llm", action="store_true", help="adjudicate the uncertain band with the LLM")
    a = ap.parse_args()
    t0 = time.time()
    rec, emb, truth = load()
    df = prepare(rec, emb)
    pairs, model = score(df)
    print(f"scored {len(pairs)} candidate pairs in {time.time() - t0:.0f}s")
    llm = None
    if a.llm:
        from .adjudicate import adjudicate
        band = decide(pairs, df)
        llm = adjudicate(band[band["method"] == "llm_pending"], df, cache=DEV / "llm_cache.parquet")
    d = decide(pairs, df, llm=llm)
    print(d.groupby("method")["linked"].agg(["count", "sum"]).to_string())
    pred, refused = cluster(df["unique_id"].tolist(), d[d["linked"]], df)
    print(f"links refused by cluster constraints: {len(refused)}",
          refused["reason"].value_counts().to_dict() if len(refused) else "")
    names = dict(zip(df["unique_id"], df["name"]))
    res, fps, fns = pairwise(pred, truth, names)
    (DEV / "metrics.json").write_text(json.dumps(res, indent=1))
    print(f"\n{'scope':18s} {'tp':>6} {'fp':>5} {'fn':>6} {'prec':>7} {'recall':>7} {'f1':>7}")
    for k, v in res.items():
        if isinstance(v, dict) and "tp" in v:
            print(f"{k:18s} {v['tp']:>6} {v['fp']:>5} {v['fn']:>6} {v['precision']:>7} {v['recall']:>7} {v['f1']:>7}")
    b = res["bcubed"]
    print(f"{'B-cubed':18s} {'':>6} {'':>5} {'':>6} {b['precision']:>7} {b['recall']:>7} {b['f1']:>7}")
    d.drop(columns=[c for c in d.columns if c.startswith(("gamma_", "bf_"))]).to_parquet(DEV / "decisions.parquet")
    lookup = df.set_index("unique_id")[["name", "country", "city", "national_id"]]
    for label, rows in (("FALSE POSITIVES", fps), ("FALSE NEGATIVES", fns)):
        print(f"\n{label} (first 15 of {len(rows)})")
        for x, y in rows[:15]:
            a_, b_ = lookup.loc[x], lookup.loc[y]
            print(f"  {x:18s} {str(a_['name'])[:38]:38s} | {y:18s} {str(b_['name'])[:38]}")
    (DEV / "model.json").write_text(json.dumps(model, indent=1))


if __name__ == "__main__":
    main()

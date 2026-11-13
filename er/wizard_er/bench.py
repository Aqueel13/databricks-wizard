"""ADR 0005 benchmark: which model adjudicates uncertain pairs best? Scored against the answer key."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from databricks.sdk import WorkspaceClient

from .adjudicate import _ask
from .dev import DEV, load
from .matching import prepare

MODELS = ["databricks-meta-llama-3-3-70b-instruct", "databricks-llama-4-maverick",
          "databricks-qwen3-next-80b-a3b-instruct"]


def main(n: int = 90) -> None:
    rec, emb, truth = load()
    df = prepare(rec, emb)
    band = pd.read_parquet(DEV / "decisions.parquet")
    band = band[band["method"].isin(["llm_pending", "review"])].sample(n=n, random_state=7)
    band["truth"] = [truth.get(a) is not None and truth.get(a) == truth.get(b)
                     for a, b in zip(band["unique_id_l"], band["unique_id_r"])]
    recs = df.set_index("unique_id").to_dict("index")
    for u, r in recs.items():
        r["source"] = u.split(":", 1)[0]
    w = WorkspaceClient(profile="wizard")
    print(f"{n} pairs from the uncertain band; {int(band['truth'].sum())} are true matches\n")
    rows = []
    for model in MODELS:
        t = time.time()
        with ThreadPoolExecutor(8) as ex:
            out = list(ex.map(lambda p, m=model: _ask(w, recs[p[0]], recs[p[1]], m),
                              zip(band["unique_id_l"], band["unique_id_r"])))
        v = pd.Series([o["verdict"] for o in out], index=band.index)
        decided = v != "unsure"
        correct = ((v == "match") & band["truth"]) | ((v == "no_match") & ~band["truth"])
        fp = int(((v == "match") & ~band["truth"]).sum())
        fn = int(((v == "no_match") & band["truth"]).sum())
        rows.append(dict(model=model.replace("databricks-", ""), accuracy_on_decided=round(correct[decided].mean(), 3),
                         unsure_rate=round(1 - decided.mean(), 3), false_match=fp, false_no_match=fn,
                         errors=sum("model error" in o["reason"] for o in out), seconds=round(time.time() - t)))
        print(rows[-1], flush=True)
    pd.DataFrame(rows).to_csv(DEV / "llm_benchmark.csv", index=False)
    (DEV / "llm_benchmark.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()

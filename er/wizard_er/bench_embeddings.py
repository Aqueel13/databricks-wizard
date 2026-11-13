"""ADR 0005 benchmark: which embedding model puts the same supplier's names closest together?

For every record with a true partner (answer key), rank all other records by cosine similarity of the name
embedding and check whether a true partner is the nearest neighbour (recall@1) or among the top 5 (recall@5).
Reported overall and per script pair (a pair counts as cyrillic-latin when the query and its partner differ).
"""
from __future__ import annotations

import json
from collections import defaultdict

import duckdb
import numpy as np

from .dev import DEV, load
from .evaluate import script

MODELS = {"qwen3-embedding-0-6b": "er_name_embeddings", "gte-large-en": "bench_emb_gte_large_en",
          "bge-large-en": "bench_emb_bge_large_en"}


def main() -> None:
    rec, _, truth = load()
    rec["u"] = rec["source"] + ":" + rec["source_id"].astype(str)
    names = dict(zip(rec["u"], rec["name"]))
    rows = []
    for model, folder in MODELS.items():
        e = duckdb.sql(f"select source || ':' || source_id u, name, embedding from '{DEV}/{folder}/*.parquet'").df()
        e = e[e["name"] == e["u"].map(names)].drop_duplicates("u")      # the record's own name
        ids = e["u"].tolist()
        m = np.vstack(e["embedding"].map(np.asarray).to_numpy()).astype(np.float32)
        m /= np.linalg.norm(m, axis=1, keepdims=True)
        sims = m @ m.T
        np.fill_diagonal(sims, -1)
        top5 = np.argsort(-sims, axis=1)[:, :5]
        hits = defaultdict(lambda: [0, 0, 0])   # category -> [queries, hit@1, hit@5]
        for i, u in enumerate(ids):
            t = truth.get(u)
            partners = [j for j, v in enumerate(ids) if j != i and truth.get(v) == t] if t else []
            if not partners:
                continue
            cats = {"-".join(sorted((script(names[u]), script(names[ids[j]])))) for j in partners}
            for cat in cats | {"overall"}:
                ok = [j for j in partners if cat == "overall" or
                      "-".join(sorted((script(names[u]), script(names[ids[j]])))) == cat]
                h = hits[cat]
                h[0] += 1
                h[1] += int(top5[i, 0] in ok)
                h[2] += int(any(j in ok for j in top5[i]))
        for cat, (n, h1, h5) in sorted(hits.items()):
            rows.append(dict(model=model, scope=cat, queries=n, recall_at_1=round(h1 / n, 3), recall_at_5=round(h5 / n, 3)))
    (DEV / "embedding_benchmark.json").write_text(json.dumps(rows, indent=1))
    print(f"{'scope':18s} " + " ".join(f"{m[:20]:>22s}" for m in MODELS))
    for scope in ["overall", "cyrillic-latin", "latin-latin", "latin-polish", "polish-polish"]:
        cells = []
        for m in MODELS:
            r = next((x for x in rows if x["model"] == m and x["scope"] == scope), None)
            cells.append(f"{r['recall_at_1']:.3f} / {r['recall_at_5']:.3f} (n={r['queries']})" if r else "-")
        print(f"{scope:18s} " + " ".join(f"{c:>22s}" for c in cells))
    print("\ncells: recall@1 / recall@5")


if __name__ == "__main__":
    main()

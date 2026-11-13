"""External benchmark: the matcher on Amazon ML Challenge 2026 business-entity data (internal use only).

A different domain from the supplier data: small US and Indian businesses, three sources with different address
layouts and OCR-style name noise, no national IDs. The matcher runs with exactly the supplier configuration
(no tuning, no LLM adjudication), so the score is an honest out-of-the-box number.

  uv run --project er python -m wizard_er.bench_amazon [--s1 4000] [--distractors 3000]
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import time

import duckdb
import pandas as pd

from .cluster import cluster, decide
from .dev import DEV, ROOT
from .evaluate import pairwise
from .matching import prepare, score

RAW = ROOT / "data" / "raw" / "er_benchmark"
OUT = DEV / "amazon"
VOLUME = "/Volumes/wizard/ops/ground_truth/_bench"
TSV = "read_csv('{f}', delim='\t', header=true, quote='', all_varchar=true)"

US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la",
             "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
             "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}
STATE_NAMES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
               "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
               "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
               "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
               "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
               "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
               "west virginia", "wisconsin", "wyoming", "district of columbia",
               # Indian states / UTs and common abbreviations
               "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana",
               "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur",
               "meghalaya", "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
               "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal", "delhi", "chandigarh",
               "jammu and kashmir", "puducherry", "mh", "ka", "tn", "dl", "up", "wb", "gj", "ts", "ap", "hr", "rj"}


def city_of(address: str | None) -> str | None:
    """Documented heuristic: drop parts with digits and state names; the city is the part nearest the state."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    keep = [p for p in parts if not re.search(r"\d", p) and p.lower() not in US_STATES | STATE_NAMES]
    return keep[-1] if keep else None


def build_sample(n_s1: int, n_distract: int, seed: int) -> tuple[pd.DataFrame, dict]:
    con = duckdb.connect()
    gt = con.sql(f"select * from {TSV.format(f=RAW / 'train_ground_truth.tsv')}").df()
    gt["m"] = gt["matched_entity_ids"].fillna("").map(lambda s: [x for x in s.split(",") if x])
    truth = {s1: s1 for s1 in gt["source1_entity_id"]}
    for s1, ms in zip(gt["source1_entity_id"], gt["m"], strict=True):
        for x in ms:
            truth[x] = s1
    rng = random.Random(seed)
    s1 = rng.sample(sorted(gt["source1_entity_id"]), n_s1)
    chosen = set(s1)
    matched = {x for ms in gt.set_index("source1_entity_id").loc[s1, "m"] for x in ms}
    ids = chosen | matched
    frames = []
    for src in ("1", "2", "3"):
        con.execute(f"create or replace table s as select * from {TSV.format(f=RAW / f'train_source{src}.tsv')}")
        want = pd.DataFrame({"entity_id": sorted(x for x in ids if x.startswith(f"S{src}-"))})
        con.register("want", want)
        part = con.sql("select s.* from s join want using (entity_id)").df()
        if src in ("2", "3"):
            # deterministic sample (DuckDB's reservoir sampling varies with parallelism)
            pool = con.sql(f"select * from s order by hash(entity_id || '{seed}') limit {n_distract * 3}").df()
            pool = pool[~pool["entity_id"].isin(ids)].head(n_distract // 2)
            part = pd.concat([part, pool])
        frames.append(part.assign(source=f"amz{src}"))
    df = pd.concat(frames, ignore_index=True)
    rec = pd.DataFrame({
        "source": df["source"], "source_id": df["entity_id"], "name": df["business_name"], "name_local": None,
        "country": df["country"].map({"US": "US", "India": "IN"}).fillna(df["country"]),
        "city": df["business_address"].map(city_of), "national_id": None, "national_id_type": None,
        "national_id_valid": None, "address": df["business_address"]})
    truth_u = {f"{s}:{i}": truth.get(i, i) for s, i in zip(rec["source"], rec["source_id"], strict=True)}
    return rec, truth_u


def embed(rec: pd.DataFrame) -> pd.DataFrame:
    """Name embeddings with the production model, computed in Databricks with ai_query (cached locally)."""
    cache = OUT / "embeddings.parquet"
    if cache.exists():
        e = pd.read_parquet(cache)
        if set(e["source_id"]) >= set(rec["source_id"]):
            return e
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState
    w = WorkspaceClient(profile="wizard")
    names = rec[["source", "source_id", "name"]]
    p = OUT / "names.parquet"
    names.to_parquet(p, index=False)
    w.files.upload(f"{VOLUME}/amazon_names.parquet", p.open("rb"), overwrite=True)
    wh = next(iter(w.warehouses.list())).id

    def run(q):
        r = w.statement_execution.execute_statement(statement=q, warehouse_id=wh, wait_timeout="50s")
        while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(3)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(r.status.error.message if r.status.error else r.status.state)
    run(f"""create or replace table wizard.ops.bench_amazon_emb as
            select source, source_id, name, ai_query('databricks-qwen3-embedding-0-6b', name) embedding
            from parquet.`{VOLUME}/amazon_names.parquet`""")
    run(f"insert overwrite directory '{VOLUME}/amazon_emb' using parquet select * from wizard.ops.bench_amazon_emb")
    parts = [f.path for f in w.files.list_directory_contents(f"{VOLUME}/amazon_emb") if f.path.endswith(".parquet")]
    frames = [pd.read_parquet(io.BytesIO(w.files.download(x).contents.read())) for x in parts]  # stream isn't seekable
    e = pd.concat(frames, ignore_index=True)
    e.to_parquet(cache, index=False)
    return e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--s1", type=int, default=4000)
    ap.add_argument("--distractors", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rec, truth = build_sample(a.s1, a.distractors, a.seed)
    print(f"sample: {len(rec)} records ({rec['source'].value_counts().to_dict()}), "
          f"{len(set(truth.values()))} true entities, cities parsed {rec['city'].notna().mean():.0%}")
    emb = embed(rec)
    df = prepare(rec, emb)   # with street addresses: the matcher compares them when present
    pairs, _ = score(df)
    d = decide(pairs, df)                       # no LLM: band pairs stay unlinked
    print(d.groupby("method")["linked"].agg(["count", "sum"]).to_string())
    assign, refused = cluster(df["unique_id"].tolist(), d[d["linked"]], df)
    names = dict(zip(df["unique_id"], df["name"], strict=True))
    results = {}
    for label, cc in (("all", None), ("US", "US"), ("India", "IN")):
        keep = set(df["unique_id"]) if cc is None else set(df.loc[df["country"] == cc, "unique_id"])
        res, _, _ = pairwise({u: v for u, v in assign.items() if u in keep}, truth, names)
        results[label] = {"pairwise": res["overall"], "bcubed": res["bcubed"], "records": res["records"]}
    results["config"] = {"s1": a.s1, "distractors": a.distractors, "seed": a.seed, "llm": False,
                         "llm_band_pairs_left_unlinked": int((d["method"] == "llm_pending").sum()),
                         "refused_by_constraints": len(refused), "seconds": round(time.time() - t0)}
    (OUT / "results.json").write_text(json.dumps(results, indent=1))
    print(f"\n{'subset':8s} {'records':>8s} {'pair P':>8s} {'pair R':>8s} {'pair F1':>8s} {'B3 F1':>8s}")
    for k in ("all", "US", "India"):
        r = results[k]
        print(f"{k:8s} {r['records']:>8d} {r['pairwise']['precision']:>8.3f} {r['pairwise']['recall']:>8.3f} "
              f"{r['pairwise']['f1']:>8.3f} {r['bcubed']['f1']:>8.3f}")
    print(json.dumps(results["config"]))


if __name__ == "__main__":
    main()

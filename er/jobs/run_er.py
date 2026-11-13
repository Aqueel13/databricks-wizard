# Databricks Wizard — entity resolution job (serverless Python task).
#
#   silver.supplier_records ─┐
#   silver.er_name_embeddings (incremental, ai_query) ─┤
#                            ├─ normalize → Splink (DuckDB) → decide (deterministic / exact name / probabilistic /
#   silver.er_llm_adjudications (cache) ─┤                       LLM / steward) → cluster with constraints
#   silver.er_steward_decisions ─────────┘
#        → silver.er_scored_pairs, silver.er_review_queue, silver.er_clusters
#        → gold.supplier_match_evidence, gold.supplier_source_xref, gold.dim_supplier (survivorship)
#        → ops.er_eval (when the generator's answer key is present), ops.er_runs
import json
import os
import sys
import time
import uuid

# serverless runs this file via exec() without __file__: the job passes the package root as --er-root
ER_ROOT = sys.argv[sys.argv.index("--er-root") + 1] if "--er-root" in sys.argv else \
    os.path.abspath(os.path.join(os.path.dirname(globals().get("__file__", ".")), ".."))
sys.path.insert(0, ER_ROOT)  # wizard_er package

import pandas as pd  # noqa: E402
from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from wizard_er.adjudicate import MODEL, PROMPT_VERSION, adjudicate  # noqa: E402
from wizard_er.cluster import cluster, decide  # noqa: E402
from wizard_er.evaluate import pairwise  # noqa: E402
from wizard_er.matching import prepare, score  # noqa: E402
from wizard_er.screening import corroborate, screen, sdn_countries  # noqa: E402

spark = SparkSession.builder.getOrCreate()
CAT = os.environ.get("WIZARD_CATALOG", "wizard")
GT = f"/Volumes/{CAT}/ops/ground_truth"
RUN_ID = uuid.uuid4().hex[:12]
t0 = time.time()


def table(name: str) -> str:
    return f"{CAT}.{name}"


def exists(name: str) -> bool:
    return spark.catalog.tableExists(table(name))


def save(df: pd.DataFrame, name: str, mode: str = "overwrite", derived_from: list | None = None) -> None:
    """Write a pandas result as a Delta table.

    derived_from = [(table, [key columns]) or (table, None)]: the output is joined back to the tables it was
    computed from (semi-join on the keys, or a one-row read when there are none). Rows don't change, but the
    write's query plan now reads those tables, so Unity Catalog lineage links them. Without this, everything
    downstream of the pandas step (golden records!) would have no recorded upstream."""
    if not len(df):
        return
    sdf = spark.createDataFrame(df)
    for src, keys in (derived_from or []):
        if keys:
            sdf = sdf.join(spark.table(table(src)).select(*keys).distinct(), keys, "left_semi")
        else:
            # LEFT join to (at most) one row: reads the table for lineage, never removes rows, even when the
            # source table is empty (an inner cross join with an empty table would drop everything)
            one = spark.table(table(src)).select(F.lit(1).alias("_lineage")).limit(1)
            sdf = sdf.join(one, on=F.lit(True), how="left").drop("_lineage")
    sdf = sdf.withColumn("er_run_id", F.lit(RUN_ID)).withColumn("er_run_at", F.current_timestamp())  # no .cache() on serverless
    n = sdf.count()
    if n != len(df):  # lineage joins must never change the data; fail loudly if they do
        raise RuntimeError(f"{name}: {len(df)} rows computed but {n} after lineage joins")
    sdf.write.mode(mode).option("overwriteSchema", "true").saveAsTable(table(name))


# ---------------------------------------------------------------- 1. inputs + incremental embeddings
spark.sql(f"""
  CREATE TABLE IF NOT EXISTS {table('silver.er_name_embeddings')}
  (source STRING, source_id STRING, name STRING, name_hash STRING, embedding ARRAY<FLOAT>, embedded_at TIMESTAMP)""")
spark.sql(f"""
  INSERT INTO {table('silver.er_name_embeddings')}
  SELECT source, source_id, nm, sha2(nm, 256), ai_query('databricks-qwen3-embedding-0-6b', nm), current_timestamp()
  FROM (SELECT source, source_id, name nm FROM {table('silver.supplier_records')} WHERE name IS NOT NULL
        UNION ALL SELECT source, source_id, name_local FROM {table('silver.supplier_records')} WHERE name_local IS NOT NULL) n
  WHERE NOT EXISTS (SELECT 1 FROM {table('silver.er_name_embeddings')} e
                    WHERE e.source = n.source AND e.source_id = n.source_id AND e.name_hash = sha2(n.nm, 256))""")
records = spark.table(table("silver.supplier_records")).toPandas()
emb = spark.sql(f"""SELECT e.source, e.source_id, e.name, e.embedding FROM {table('silver.er_name_embeddings')} e
                    JOIN {table('silver.supplier_records')} r USING (source, source_id)
                    WHERE e.name IN (r.name, r.name_local)""").toPandas()

# ---------------------------------------------------------------- 2. score + decide
df = prepare(records, emb)
pairs, model = score(df)
first = decide(pairs, df)
spark.sql(f"""CREATE TABLE IF NOT EXISTS {table('silver.er_llm_adjudications')}
              (pair_key STRING, model STRING, prompt_version STRING, verdict STRING, reason STRING, raw STRING)""")
spark.sql(f"""CREATE TABLE IF NOT EXISTS {table('silver.er_steward_decisions')}
              (pair_key STRING, decision STRING, steward STRING, note STRING, decided_at TIMESTAMP)""")
cache = spark.table(table("silver.er_llm_adjudications")).toPandas()
band = first[first["method"] == "llm_pending"]
known = set(cache[(cache["model"] == MODEL) & (cache["prompt_version"] == PROMPT_VERSION)]["pair_key"])
from databricks.sdk import WorkspaceClient  # noqa: E402

new = adjudicate(band[~band["pair_key"].isin(known)], df, client=WorkspaceClient())
if len(new):
    spark.createDataFrame(new[["pair_key", "model", "prompt_version", "verdict", "reason", "raw"]]) \
         .write.mode("append").saveAsTable(table("silver.er_llm_adjudications"))
llm = spark.table(table("silver.er_llm_adjudications")).where(
    f"model = '{MODEL}' AND prompt_version = '{PROMPT_VERSION}'").toPandas()
steward = spark.table(table("silver.er_steward_decisions")).toPandas()
d = decide(pairs, df, llm=llm, steward=steward)

# ---------------------------------------------------------------- 3. cluster with constraints
assign, refused = cluster(df["unique_id"].tolist(), d[d["linked"]], df)
df["supplier_id"] = df["unique_id"].map(assign)

# ---------------------------------------------------------------- 4. outputs
reasons = dict(zip(llm["pair_key"], llm["reason"]))
d["llm_reason"] = d["pair_key"].map(reasons)
gcols = [c for c in d.columns if c.startswith("gamma_")]
d["evidence"] = d[gcols].apply(lambda r: json.dumps({k[6:]: int(v) for k, v in r.items()}), axis=1)
scored = d.drop(columns=[c for c in d.columns if c.startswith(("gamma_", "bf_"))])
save(scored, "silver.er_scored_pairs", derived_from=[("silver.supplier_records", None), ("silver.er_name_embeddings", None), ("silver.er_llm_adjudications", None), ("silver.er_steward_decisions", None)])

RCOLS = ["pair_key", "unique_id_l", "unique_id_r", "match_probability", "reason"]
review = pd.concat([d[d["method"] == "review"].assign(reason="LLM unsure")[RCOLS],
                    refused[RCOLS] if len(refused) else pd.DataFrame(columns=RCOLS)], ignore_index=True)
review = review[~review["pair_key"].isin(set(steward["pair_key"]))]
save(review.assign(status="open"), "silver.er_review_queue", derived_from=[("silver.er_scored_pairs", ["pair_key"])])
save(df[["unique_id", "source", "source_id", "supplier_id"]], "silver.er_clusters",
     derived_from=[("silver.supplier_records", ["source", "source_id"]), ("silver.er_scored_pairs", None)])

linked = scored[scored["linked"]].copy()
linked["supplier_id"] = linked["unique_id_l"].map(assign)
linked = linked[linked["supplier_id"] == linked["unique_id_r"].map(assign)]  # links that survived the constraints
save(linked[["supplier_id", "pair_key", "unique_id_l", "unique_id_r", "method", "match_probability", "match_weight",
             "evidence", "llm_reason"]], "gold.supplier_match_evidence",
     derived_from=[("silver.er_scored_pairs", ["pair_key"]), ("silver.er_clusters", ["supplier_id"])])

# survivorship: field by field, most trusted source first
PRIORITY = {"hicx": 0, "sap": 1, "coupa": 2}
df["_rank"] = df["source"].map(PRIORITY)
valid_id = df["nid"].notna()


def first_by(g: pd.DataFrame, col: str, mask=None):
    g = g if mask is None else g[mask.loc[g.index]]
    g = g[g[col].notna() & (g[col].astype(str) != "")].sort_values("_rank")
    return (g[col].iloc[0], g["source"].iloc[0]) if len(g) else (None, None)


rows = []
for sid, g in df.groupby("supplier_id"):
    name, name_src = first_by(g, "name")
    local = next((x for x in list(g.sort_values("_rank")["name_local"]) + list(g["name"]) if isinstance(x, str)
                  and any("Ѐ" <= ch <= "ӿ" for ch in x)), None)
    nid, nid_src = first_by(g, "national_id", valid_id)
    rows.append(dict(
        supplier_id=sid, legal_name=name, legal_name_source=name_src, legal_name_local=local,
        national_id=nid, national_id_type=g.loc[g["national_id"] == nid, "national_id_type"].iloc[0] if nid else None,
        national_id_source=nid_src, conflicting_national_ids=int(g["nid"].dropna().nunique() > 1),
        country=first_by(g, "country")[0], city=first_by(g, "city")[0],
        legal_form=first_by(g, "legal_form")[0],
        source_systems=sorted(g["source"].unique().tolist()), source_records=int(len(g)),
        sap_vendors=sorted(g.loc[g["source"] == "sap", "source_id"].tolist()),
        coupa_supplier_ids=sorted(g.loc[g["source"] == "coupa", "source_id"].tolist()),
        hicx_ids=sorted(g.loc[g["source"] == "hicx", "source_id"].tolist()),
        purchasing_blocked=bool((g["status"] == "blocked").any()),
    ))
save(pd.DataFrame(rows), "gold.dim_supplier", derived_from=[("silver.er_clusters", ["supplier_id"]), ("silver.supplier_records", None)])

# SCD2 history of the golden record: a new version whenever a tracked attribute changes (survivorship picked a
# different name, an ID was corrected, a source joined or left, a block was set...). Suppliers that disappear
# (merged into another cluster) get their current version closed. dim_supplier stays the current-state view.
TRACKED = ["legal_name", "legal_name_local", "national_id", "national_id_type", "country", "city", "legal_form",
           "source_systems", "purchasing_blocked"]
spark.sql(f"""CREATE TABLE IF NOT EXISTS {table('gold.dim_supplier_history')} (
    supplier_id STRING, legal_name STRING, legal_name_local STRING, national_id STRING, national_id_type STRING,
    country STRING, city STRING, legal_form STRING, source_systems ARRAY<STRING>, purchasing_blocked BOOLEAN,
    attr_hash STRING, valid_from TIMESTAMP, valid_to TIMESTAMP, is_current BOOLEAN, er_run_id STRING)
  COMMENT 'SCD type 2 history of the golden supplier record (one row per version; is_current marks the live one).'""")
cols = ", ".join(TRACKED)
spark.sql(f"""CREATE OR REPLACE TEMP VIEW _dim_now AS
    SELECT {cols}, supplier_id, sha2(to_json(struct({cols})), 256) AS attr_hash FROM {table('gold.dim_supplier')}""")
spark.sql(f"""MERGE INTO {table('gold.dim_supplier_history')} h USING _dim_now n
    ON h.supplier_id = n.supplier_id AND h.is_current
    WHEN MATCHED AND h.attr_hash <> n.attr_hash THEN UPDATE SET valid_to = current_timestamp(), is_current = false
    WHEN NOT MATCHED BY SOURCE AND h.is_current THEN UPDATE SET valid_to = current_timestamp(), is_current = false""")
spark.sql(f"""INSERT INTO {table('gold.dim_supplier_history')}
    SELECT n.supplier_id, {", ".join("n." + c for c in TRACKED)}, n.attr_hash, current_timestamp(), NULL, true, '{RUN_ID}'
    FROM _dim_now n LEFT ANTI JOIN {table('gold.dim_supplier_history')} h
      ON h.supplier_id = n.supplier_id AND h.is_current""")
save(df[["supplier_id", "source", "source_id", "unique_id", "name", "country", "national_id"]]
     .merge(linked.groupby("unique_id_l")["method"].first().rename("match_method"), left_on="unique_id",
            right_index=True, how="left"), "gold.supplier_source_xref",
     derived_from=[("silver.supplier_records", ["source", "source_id"]), ("silver.er_clusters", ["supplier_id"])])

# name index for agent search: every variant (Latin and local script) of every golden supplier with its skeleton
ni = pd.concat([
    df[["supplier_id", "source", "source_id", "name", "skeleton"]],
    df[df["name_local"].notna()][["supplier_id", "source", "source_id", "name_local", "skeleton_local"]]
      .rename(columns={"name_local": "name", "skeleton_local": "skeleton"}),
], ignore_index=True).dropna(subset=["name"]).astype(object)
ni = ni.where(ni.notna(), None).drop_duplicates(["supplier_id", "name"])
save(ni, "gold.supplier_name_index", derived_from=[("silver.er_clusters", ["supplier_id"])])

# ---------------------------------------------------------------- 4b. sanctions screening + cross-source DQ
golden = pd.DataFrame(rows)
sanctions = spark.table(table("silver.sanctions_names")).select(
    "ent_num", "name", "name_type", "program", "sdn_type").toPandas()
hits = screen(golden, sanctions)
# country corroboration from OFAC addresses (bronze.ofac_add: _c0 ent_num, _c4 country)
addr = spark.table(table("bronze.ofac_add")).selectExpr("cast(_c0 AS BIGINT) ent_num", "trim(_c4) country").toPandas()
hits = corroborate(hits, dict(zip(golden["supplier_id"], golden["country"])), sdn_countries(addr))
# steward verdicts on alerts (latest per supplier + sanctioned entity): confirmed / dismissed; otherwise open
spark.sql(f"""CREATE TABLE IF NOT EXISTS {table('silver.sanctions_alert_decisions')}
              (supplier_id STRING, ent_num BIGINT, decision STRING, steward STRING, note STRING,
               screened_name STRING, sanctioned_name STRING, decided_at TIMESTAMP)""")
dec = spark.sql(f"""SELECT supplier_id, ent_num, decision FROM {table('silver.sanctions_alert_decisions')}
                    QUALIFY row_number() OVER (PARTITION BY supplier_id, ent_num ORDER BY decided_at DESC) = 1""").toPandas()
verdict_of = {(a, int(b)): c for a, b, c in zip(dec["supplier_id"], dec["ent_num"], dec["decision"])}
# a steward's verdict wins; otherwise the country rule decides open vs auto_cleared
hits["status"] = [{"confirm": "confirmed", "dismiss": "dismissed"}.get(verdict_of.get((a, int(b))), r)
                  for a, b, r in zip(hits["supplier_id"], hits["ent_num"], hits["rule_status"])]
hits = hits.drop(columns=["rule_status"])
save(hits, "gold.supplier_sanctions_hits",
     derived_from=[("gold.dim_supplier", ["supplier_id"]), ("silver.sanctions_names", ["ent_num"]),
                   ("bronze.ofac_add", None), ("silver.sanctions_alert_decisions", None)])

# city consistency within each supplier (only possible after entity resolution). Compare spellings, not the
# phonetic skeleton used for matching (which deliberately hides "Kyiv" vs "Kyyiv"). The record that differs from
# the supplier's majority is flagged:
#   conflicting_address       different spelling (typo, other city)
#   inconsistent_city_format  same spelling, different case / diacritics ("Kyiv" vs "KYIV", "Kraków" vs "Krakow")
from wizard_er.normalize import latin  # noqa: E402

conf = []
for sid, g in df[df["city"].notna() & (df["city"].astype(str) != "")].groupby("supplier_id"):
    if len(g) < 2:
        continue
    lat = g["city"].map(latin)
    raw = g["city"].astype(str)

    def odd(values: pd.Series) -> pd.Series:
        """Records that disagree: the minority if there is a strict majority, otherwise everyone (a tie gives no
        basis to say which record is wrong, so the steward sees all of them)."""
        counts = values.value_counts()
        if len(counts) < 2:
            return pd.Series(False, index=values.index)
        if counts.iloc[0] > counts.iloc[1]:
            return values != counts.index[0]
        return pd.Series(True, index=values.index)

    odd_lat, odd_raw = odd(lat), odd(raw)
    for i, r in g.iterrows():
        rule = "conflicting_address" if odd_lat[i] else ("inconsistent_city_format" if odd_raw[i] else None)
        if rule:
            conf.append(dict(supplier_id=sid, source=r["source"], source_id=r["source_id"], rule=rule, city=r["city"],
                             other_cities=", ".join(sorted(set(raw) - {r["city"]}))))
save(pd.DataFrame(conf, columns=["supplier_id", "source", "source_id", "rule", "city", "other_cities"]),
     "silver.er_dq_findings", derived_from=[("silver.er_clusters", ["supplier_id"])])

# ---------------------------------------------------------------- 5. evaluation (when the answer key is present)
metrics = None
try:
    gt = spark.read.parquet(f"{GT}/gt_source_record.parquet").select(
        F.concat_ws(":", "source_system", "source_id").alias("u"), "true_supplier_id").toPandas()
    metrics, _, _ = pairwise(assign, dict(zip(gt["u"], gt["true_supplier_id"])), dict(zip(df["unique_id"], df["name"])))
    flat = [dict(scope=k, **v) for k, v in metrics.items() if isinstance(v, dict) and "tp" in v]
    flat.append(dict(scope="bcubed", tp=None, fp=None, fn=None, **metrics["bcubed"]))
    # defects deferred from M3: duplicates must share a cluster; conflicting addresses must be flagged;
    # sanctions near-matches must be hit
    gd = spark.read.parquet(f"{GT}/gt_defect.parquet").toPandas()
    gs = spark.read.parquet(f"{GT}/gt_source_record.parquet").toPandas()
    dup = gd[gd["defect_type"] == "within_system_duplicate"]
    ok = [assign.get(f"{r.source_system}:{r.injected_value}") is not None and
          assign.get(f"{r.source_system}:{r.injected_value}") == assign.get(f"{r.source_system}:{r.original_value}")
          for r in dup.itertuples()]
    flagged = {(c["source"], c["source_id"]) for c in conf}
    ca = gd[gd["defect_type"] == "conflicting_address"]
    ca_ok = [("hicx", k) in flagged for k in ca["entity_key"]]
    sn = gd[gd["defect_type"] == "sanctions_near_match"]
    rec_of = gs.groupby("true_supplier_id").apply(lambda g: [f"{a}:{b}" for a, b in zip(g.source_system, g.source_id)])
    hit_sids = set(hits["supplier_id"])
    sn_ok = [any(assign.get(u) in hit_sids for u in rec_of.get(k, [])) for k in sn["entity_key"]]
    for scope, vals in (("defect:within_system_duplicate", ok), ("defect:conflicting_address", ca_ok),
                        ("defect:sanctions_near_match", sn_ok)):
        flat.append(dict(scope=scope, tp=sum(vals), fp=None, fn=len(vals) - sum(vals), precision=None,
                         recall=round(sum(vals) / len(vals), 4) if vals else None, f1=None))
    save(pd.DataFrame(flat), "ops.er_eval", mode="append")
    print(pd.DataFrame(flat).to_string())
except Exception as e:  # no answer key in a real deployment
    print("evaluation skipped:", e)

run = dict(records=len(df), candidate_pairs=len(pairs), links=int(d["linked"].sum()),
           by_method=d.groupby("method")["linked"].sum().astype(int).to_dict(), refused=len(refused),
           review_queue=len(review), suppliers=int(df["supplier_id"].nunique()), llm_new_calls=len(new),
           seconds=round(time.time() - t0), f1=metrics["overall"]["f1"] if metrics else None,
           f1_cyrillic_latin=metrics.get("cyrillic-latin", {}).get("f1") if metrics else None)
save(pd.DataFrame([dict(summary=json.dumps(run), model=json.dumps(model)[:60000])]), "ops.er_runs", mode="append")
print(json.dumps(run, indent=1))

# Databricks notebook source
# MAGIC %md
# MAGIC # Databricks Wizard: Free Edition capability check
# MAGIC
# MAGIC Run all cells top to bottom. Each check writes a PASS / FAIL / INFO row to the summary table at the end.
# MAGIC Nothing here costs money. **Optional** cells (marked) create resources that use up quota slots, so they are off by default.

# COMMAND ----------

CATALOG = "wizard"          # falls back to "workspace" if you can't create catalogs
RUN_VECTOR_SEARCH_CREATE = False   # uses your single VS endpoint slot; flip to True when ready
results = []

def check(name, fn):
    try:
        out = fn()
        results.append((name, "PASS", str(out)[:300]))
    except Exception as e:
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"[:300]))

from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

# COMMAND ----------
# MAGIC %md ## 1. Compute & runtime

# COMMAND ----------

check("Spark runtime", lambda: spark.version)
check("Serverless SQL works", lambda: spark.sql("SELECT 1 AS ok").collect()[0].ok)
check("SQL warehouses", lambda: [(x.name, x.state) for x in w.warehouses.list()])

# COMMAND ----------
# MAGIC %md ## 2. Unity Catalog: catalog, schemas, volume

# COMMAND ----------

check("Current metastore", lambda: spark.sql("SELECT current_metastore()").collect()[0][0])
check("Existing catalogs", lambda: [c.name for c in w.catalogs.list()])

try:
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
    results.append(("Create catalog", "PASS", CATALOG))
except Exception as e:
    results.append(("Create catalog", "FAIL", f"{e}"[:200] + " -> falling back to 'workspace'"))
    CATALOG = "workspace"

for s in ["bronze", "silver", "gold"]:
    check(f"Create schema {s}", lambda s=s: spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{s}") and f"{CATALOG}.{s}")

check("Create volume bronze.landing",
      lambda: spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.bronze.landing") and f"/Volumes/{CATALOG}/bronze/landing")

# COMMAND ----------
# MAGIC %md ## 3. Delta Lake: write, history, Change Data Feed, time travel

# COMMAND ----------

T = f"{CATALOG}.bronze._capcheck_suppliers"

def delta_test():
    spark.sql(f"DROP TABLE IF EXISTS {T}")
    spark.sql(f"""CREATE TABLE {T} (id INT, name STRING)
                  TBLPROPERTIES (delta.enableChangeDataFeed = true)""")
    spark.sql(f"INSERT INTO {T} VALUES (1,'IBM Corp'),(2,'I.B.M.')")
    spark.sql(f"UPDATE {T} SET name='International Business Machines' WHERE id=2")
    hist = spark.sql(f"DESCRIBE HISTORY {T}").count()
    cdf = spark.sql(f"SELECT * FROM table_changes('{T}', 1)").count()
    tt = spark.sql(f"SELECT * FROM {T} VERSION AS OF 1").count()
    return f"history_versions={hist}, cdf_rows={cdf}, time_travel_rows={tt}"

check("Delta + CDF + time travel", delta_test)

# COMMAND ----------
# MAGIC %md ## 4. Auto Loader (incremental file ingestion) on serverless

# COMMAND ----------

def autoloader_test():
    base = f"/Volumes/{CATALOG}/bronze/landing/_capcheck"
    dbutils.fs.mkdirs(f"{base}/in")
    dbutils.fs.put(f"{base}/in/po_001.json",
                   '{"po_id":"PO-1","supplier":"IBM Corp","amount":1200.5}', overwrite=True)
    q = (spark.readStream.format("cloudFiles")
         .option("cloudFiles.format", "json")
         .option("cloudFiles.schemaLocation", f"{base}/_schema")
         .load(f"{base}/in")
         .writeStream
         .option("checkpointLocation", f"{base}/_ckpt")
         .trigger(availableNow=True)
         .toTable(f"{CATALOG}.bronze._capcheck_autoloader"))
    q.awaitTermination()
    return spark.table(f"{CATALOG}.bronze._capcheck_autoloader").count()

check("Auto Loader availableNow", autoloader_test)

# COMMAND ----------
# MAGIC %md ## 5. Jobs, Lakeflow pipelines, Apps, Genie (list only)

# COMMAND ----------

check("Jobs API", lambda: f"{len(list(w.jobs.list()))} jobs visible")
check("Lakeflow pipelines API", lambda: f"{len(list(w.pipelines.list_pipelines()))} pipelines visible")
check("Databricks Apps API", lambda: [a.name for a in w.apps.list()] or "none yet (API reachable)")
check("Genie API", lambda: w.genie.list_spaces())

# COMMAND ----------
# MAGIC %md ## 6. Model Serving: foundation models (chat + embeddings)

# COMMAND ----------

endpoints = []
def list_serving():
    global endpoints
    endpoints = [e.name for e in w.serving_endpoints.list()]
    return endpoints
check("Serving endpoints", list_serving)

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

chat_ep = next((e for e in endpoints if any(k in e for k in ["llama", "claude", "gpt", "qwen", "gemma"])), None)
emb_ep  = next((e for e in endpoints if any(k in e for k in ["gte", "bge", "embed"])), None)

check(f"Chat query ({chat_ep})", lambda: w.serving_endpoints.query(
    name=chat_ep, max_tokens=500,  # reasoning models (gpt-oss) spend tokens thinking first
    messages=[ChatMessage(role=ChatMessageRole.USER, content="Reply with the word OK")]
).choices[0].message.content)

check(f"Embedding query ({emb_ep})", lambda: f"dim={len(w.serving_endpoints.query(name=emb_ep, input=['supplier risk']).data[0].embedding)}")

check("ai_query() SQL function", lambda: spark.sql(
    f"SELECT ai_query('{chat_ep}', 'Reply with the word OK') AS r").collect()[0].r)

# COMMAND ----------
# MAGIC %md ## 7. Vector Search

# COMMAND ----------

check("Vector Search endpoints", lambda: [(e.name, e.endpoint_status.state if e.endpoint_status else None)
                                          for e in w.vector_search_endpoints.list_endpoints()] or "none yet (API reachable)")

if RUN_VECTOR_SEARCH_CREATE:
    from databricks.sdk.service.vectorsearch import EndpointType
    check("Create VS endpoint 'wizard-vs'", lambda: w.vector_search_endpoints.create_endpoint(
        name="wizard-vs", endpoint_type=EndpointType.STANDARD).result().name)

# COMMAND ----------
# MAGIC %md ## 8. System tables (lineage, billing) – needed for the lineage demo

# COMMAND ----------

check("system.access.table_lineage", lambda: spark.sql(
    "SELECT count(*) FROM system.access.table_lineage").collect()[0][0])
check("system.billing.usage", lambda: spark.sql(
    "SELECT count(*) FROM system.billing.usage").collect()[0][0])
check("system.information_schema", lambda: spark.sql(
    f"SELECT count(*) FROM system.information_schema.tables WHERE table_catalog='{CATALOG}'").collect()[0][0])

# COMMAND ----------
# MAGIC %md ## Summary

# COMMAND ----------

display(spark.createDataFrame(results, "check STRING, status STRING, detail STRING"))

# COMMAND ----------
# MAGIC %md ## Cleanup (optional)

# COMMAND ----------

# spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.bronze._capcheck_suppliers")
# spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.bronze._capcheck_autoloader")
# dbutils.fs.rm(f"/Volumes/{CATALOG}/bronze/landing/_capcheck", recurse=True)

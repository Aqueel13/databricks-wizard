# Databricks Wizard — bronze layer (Lakeflow Declarative Pipeline).
#
# Append-only, schema-on-read. Every table carries ingest metadata:
#   _file, _batch_id, _ingested_at
# SAP CDC events and Coupa records are stored whole as VARIANT (`payload`), so upstream schema
# changes (e.g. Coupa's new `diversity-class` field) never break ingestion. Typing happens in silver.
import dlt
from pyspark.sql import functions as F

LANDING = spark.conf.get("wizard.landing", "/Volumes/wizard/bronze/landing")  # noqa: F821 (spark is provided)

SAP_TABLES = ["lfa1", "lfb1", "ekko", "ekpo", "eket", "ekbe", "rbkp", "rseg", "t001", "t023t", "tcurr"]
COUPA_ENTITIES = ["suppliers", "purchase_orders", "receipts", "invoices", "contracts"]
BRONZE = {"quality": "bronze"}
BINARY = ["hicx_supplier_exports", "docs_binary"]  # one row per file


def _meta(df):
    file_path = F.regexp_replace(F.col("_metadata.file_path"), "^dbfs:", "")
    return (df.withColumn("_file", file_path)
              .withColumn("_batch_id", F.regexp_extract(F.col("_metadata.file_name"), r"^(.+?)(-\d{3})?\.jsonl\.gz$", 1))
              .withColumn("_ingested_at", F.current_timestamp()))


def _autoloader(fmt: str, path: str, **opts):
    r = spark.readStream.format("cloudFiles").option("cloudFiles.format", fmt)  # noqa: F821
    for k, v in opts.items():
        r = r.option(k, v)
    return r.load(path)


# ------------------------------------------------------------------ SAP (Debezium CDC)
def _sap_table(table: str):
    @dlt.table(name=f"sap_{table}_cdc", table_properties=BRONZE,
               comment=f"Raw Debezium change events for SAP {table.upper()} (one row per event, at-least-once).")
    def _t():
        df = _autoloader("text", f"{LANDING}/sap/cdc/{table}").withColumn("payload", F.expr("parse_json(value)"))
        return _meta(df).select(
            F.expr("payload:kafka.topic::string").alias("kafka_topic"),
            F.expr("payload:kafka.partition::int").alias("kafka_partition"),
            F.expr("payload:kafka.offset::bigint").alias("kafka_offset"),
            F.expr("payload:event.op::string").alias("op"),
            F.expr("timestamp_millis(payload:event.ts_ms::bigint)").alias("event_ts"),
            F.expr("payload:event.source.lsn::bigint").alias("source_lsn"),
            "payload", "_file", "_batch_id", "_ingested_at")


for _t in SAP_TABLES:
    _sap_table(_t)


# ------------------------------------------------------------------ Coupa (REST pages)
def _coupa(entity: str):
    @dlt.table(name=f"coupa_{entity}", table_properties=BRONZE,
               comment=f"Raw Coupa API records for {entity}; overlapping pulls may repeat records (dedup in silver).")
    def _t():
        df = _autoloader("text", f"{LANDING}/coupa/{entity}").withColumn("payload", F.expr("parse_json(value)"))
        return _meta(df).select(
            F.expr("payload:id::bigint").alias("id"),
            F.expr("try_cast(payload:['updated-at']::string as timestamp)").alias("updated_at"),
            "payload", "_file", "_batch_id", "_ingested_at")


for _e in COUPA_ENTITIES:
    _coupa(_e)


# ------------------------------------------------------------------ HICX (file exports)
def _hicx_csv(name: str, file: str):
    @dlt.table(name=f"hicx_{name}", table_properties=BRONZE, comment=f"HICX {file} rows, all columns as strings.")
    def _t():
        df = _autoloader("csv", f"{LANDING}/hicx", header="true", pathGlobFilter=file,
                         **{"cloudFiles.inferColumnTypes": "false", "cloudFiles.schemaEvolutionMode": "rescue"})
        return _meta(df).withColumn("export_date", F.regexp_extract("_file", r"/hicx/([^/]+)/", 1))


_hicx_csv("bank_details", "bank_details.csv")
_hicx_csv("certifications", "certifications.csv")


@dlt.table(table_properties=BRONZE, comment="HICX supplier export workbooks (.xlsx) as binary; parsed in silver.")
def hicx_supplier_exports():
    df = _autoloader("binaryFile", f"{LANDING}/hicx", pathGlobFilter="*.xlsx")
    return _meta(df).withColumn("export_date", F.regexp_extract("_file", r"/hicx/([^/]+)/", 1)).drop("path")


# ------------------------------------------------------------------ documents
@dlt.table(table_properties=BRONZE, comment="Supplier documents (PDF) as binary, with owner parsed from the path.")
def docs_binary():
    df = _autoloader("binaryFile", f"{LANDING}/docs", pathGlobFilter="*.pdf")
    return (_meta(df).drop("path")
            .withColumn("doc_type", F.when(F.col("_file").contains("/certificate/"), "certificate")
                        .when(F.col("_file").contains("/contracts/"), "contract").otherwise("other"))
            .withColumn("owner_id", F.regexp_extract("_file", r"/docs/(?:contracts/)?([^/]+)/", 1)))


# ------------------------------------------------------------------ public reference data
def _ofac(name: str, file: str):
    @dlt.table(name=f"ofac_{name}", table_properties=BRONZE, comment=f"OFAC {file} (no header row in source).")
    def _t():
        df = _autoloader("csv", f"{LANDING}/public/ofac", header="false", pathGlobFilter=file,
                         **{"cloudFiles.inferColumnTypes": "false", "cloudFiles.schemaEvolutionMode": "none"})
        # the source file ends with an EOF marker line that is not a record
        return (_meta(df).where(F.col("_c0").rlike("^[0-9]+$"))
                .withColumn("snapshot_date", F.regexp_extract("_file", r"/ofac/([^/]+)/", 1)))


_ofac("sdn", "sdn.csv")
_ofac("alt", "alt.csv")


# ------------------------------------------------------------------ manifests + reconciliation
@dlt.table(table_properties=BRONZE, comment="Shipper manifests: what each batch claims to contain.")
def landing_manifests():
    df = _autoloader("json", f"{LANDING}/_manifests", multiLine="true",
                     **{"cloudFiles.inferColumnTypes": "true", "cloudFiles.schemaEvolutionMode": "rescue",
                        "cloudFiles.schemaHints": "offsets STRING, watermark_from STRING, watermark_to STRING"})
    return df.withColumn("_file", F.regexp_replace(F.col("_metadata.file_path"), "^dbfs:", "")) \
             .withColumn("_ingested_at", F.current_timestamp())


_LOADED = (
    [f"sap_{t}_cdc" for t in SAP_TABLES] + [f"coupa_{e}" for e in COUPA_ENTITIES]
    + ["hicx_bank_details", "hicx_certifications", "hicx_supplier_exports", "docs_binary", "ofac_sdn", "ofac_alt"]
)


@dlt.table(comment="Per file: records the manifest declared vs rows loaded into bronze.")
@dlt.expect("file_fully_loaded", "status = 'OK'")
def landing_reconciliation():
    loaded = None
    for t in _LOADED:
        part = dlt.read(t).groupBy("_file").agg(F.count(F.lit(1)).alias("loaded")).withColumn("bronze_table", F.lit(t))
        loaded = part if loaded is None else loaded.unionByName(part)
    declared = (dlt.read("landing_manifests")
                .select("batch_id", "source", "entity", F.col("created_at").alias("batch_created_at"),
                        F.explode("files").alias("f"))
                .select("batch_id", "source", "entity", "batch_created_at",
                        F.col("f.path").alias("_file"), F.col("f.records").alias("declared")))
    j = declared.join(loaded, "_file", "full_outer")
    binary = F.col("bronze_table").isin(BINARY)
    return j.withColumn("status", F.when(F.col("declared").isNull(), "NO_MANIFEST")
                        .when(F.col("loaded").isNull(), "NOT_LOADED")
                        .when(binary & (F.col("loaded") == 1), "OK")          # binary: the file itself arrived
                        .when(F.col("declared") == F.col("loaded"), "OK").otherwise("MISMATCH"))

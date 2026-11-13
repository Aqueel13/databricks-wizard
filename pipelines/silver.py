# Databricks Wizard — silver layer (Lakeflow Declarative Pipeline).
#
#   SAP     AUTO CDC from bronze Debezium events -> current-state tables (SCD1), vendor master with history (SCD2)
#           * de-duplicated on Kafka coordinates (the shipper is at-least-once)
#           * ordered by (source LSN, Kafka offset), so late / out-of-order events land correctly
#   Coupa   AUTO CDC on (id, updated-at) -> latest record per id; PO lines exploded from the PO payload
#   HICX    workbook parsed from the binary export; CSVs typed
#   OFAC    sanctioned names + aliases
#   DQ      hard rules -> rows quarantined in dq_quarantine (one shared table); soft rules -> dq_findings
import dlt
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

CAT = spark.conf.get("wizard.catalog", "wizard")  # noqa: F821
B = f"{CAT}.bronze"
auto_cdc = getattr(dlt, "create_auto_cdc_flow", None) or dlt.apply_changes


dlt.create_streaming_table(
    name="dq_quarantine",
    comment="Rows that failed hard DQ rules, from every silver entity (one shared table to save schema quota).")


def _quarantine_flow(source: str, entity: str, view: str) -> None:
    @dlt.append_flow(target="dq_quarantine", name=f"quarantine_{source}_{entity}")
    def _f():
        df = dlt.read_stream(view).where(F.size("_dq_failed") > 0)
        return df.select(F.lit(source).alias("source"), F.lit(entity).alias("entity"),
                         F.to_json(F.struct(*[c for c in df.columns if c != "_dq_failed"])).alias("record"),
                         "_dq_failed", "_batch_id", F.current_timestamp().alias("_quarantined_at"))


def _q(rules: dict) -> str:
    """SQL condition that is TRUE when any hard rule fails."""
    return " OR ".join(f"NOT coalesce({c}, false)" for c in rules.values()) or "false"


def _failed(rules: dict):
    return F.filter(F.array(*[F.when(F.expr(f"NOT coalesce({c}, false)"), F.lit(n)) for n, c in rules.items()]),
                    lambda x: x.isNotNull())


# ====================================================================== SAP
S, D, N = "s", "d", "n"
SAP = {
    # table: (keys, scd type, {column: type}, extra hard rules)
    "lfa1": (["lifnr"], 2, dict(lifnr=S, name1=S, name2=S, land1=S, ort01=S, pstlz=S, regio=S, stras=S, stcd1=S,
                                stcd2=S, stceg=S, telf1=S, ktokk=S, loevm=S, sperr=S, spras=S, erdat=D, ernam=S), {}),
    "lfb1": (["lifnr", "bukrs"], 1, dict(lifnr=S, bukrs=S, akont=S, zterm=S, erdat=D), {}),
    "ekko": (["ebeln"], 1, dict(ebeln=S, bukrs=S, bstyp=S, bsart=S, loekz=S, aedat=D, ernam=S, lifnr=S, zterm=S,
                                ekorg=S, ekgrp=S, waers=S, wkurs=N, bedat=D, kdatb=D, kdate=D, inco1=S),
             {"vendor_present": "op = 'd' OR lifnr IS NOT NULL", "order_date_present": "op = 'd' OR bedat IS NOT NULL"}),
    "ekpo": (["ebeln", "ebelp"], 1, dict(ebeln=S, ebelp=S, loekz=S, aedat=D, txz01=S, matnr=S, bukrs=S, werks=S,
                                         matkl=S, menge=N, meins=S, netpr=N, peinh=N, netwr=N, brtwr=N, mwskz=S,
                                         elikz=S, erekz=S, pstyp=S, knttp=S, webaz=N),
             {"quantity_not_negative": "op = 'd' OR menge >= 0", "net_value_present_and_not_negative": "op = 'd' OR netwr >= 0"}),
    "eket": (["ebeln", "ebelp", "etenr"], 1, dict(ebeln=S, ebelp=S, etenr=S, eindt=D, slfdt=D, menge=N, wemng=N),
             {"delivery_date_present": "op = 'd' OR eindt IS NOT NULL"}),
    "ekbe": (["ebeln", "ebelp", "zekkn", "vgabe", "gjahr", "belnr", "buzei"], 1,
             dict(ebeln=S, ebelp=S, zekkn=S, vgabe=S, gjahr=S, belnr=S, buzei=S, bewtp=S, bwart=S, budat=D, menge=N,
                  dmbtr=N, wrbtr=N, waers=S, shkzg=S, xblnr=S, cpudt=D, cputm=S),
             {"posting_date_present": "op = 'd' OR budat IS NOT NULL"}),
    "rbkp": (["belnr", "gjahr"], 1, dict(belnr=S, gjahr=S, blart=S, bldat=D, budat=D, bukrs=S, lifnr=S, waers=S,
                                         kursf=N, rmwwr=N, wmwst1=N, zterm=S, zbd1t=N, xblnr=S, rbstat=S, stblg=S),
             {"vendor_present": "op = 'd' OR lifnr IS NOT NULL"}),
    "rseg": (["belnr", "gjahr", "buzei"], 1, dict(belnr=S, gjahr=S, buzei=S, ebeln=S, ebelp=S, matnr=S, bukrs=S,
                                                  werks=S, wrbtr=N, shkzg=S, mwskz=S, menge=N, bstme=S), {}),
    "t001": (["bukrs"], 1, dict(bukrs=S, butxt=S, ort01=S, land1=S, waers=S), {}),
    "t023t": (["matkl"], 1, dict(matkl=S, wgbez=S), {}),
    "tcurr": (["kurst", "fcurr", "tcurr", "gdatu"], 1, dict(kurst=S, fcurr=S, tcurr=S, gdatu=S, ukurs=N), {}),
}


def _typed(col: str, typ: str):
    v = f"coalesce(payload:event.after.{col}, payload:event.before.{col})"   # deletes only carry `before`
    if typ == D:   # Debezium (time.precision.mode=connect) sends DATE as days since epoch
        return F.expr(f"date_add(DATE'1970-01-01', {v}::int)").alias(col)
    if typ == N:   # decimal.handling.mode=string
        return F.expr(f"try_cast({v}::string as decimal(20,5))").alias(col)
    return F.expr(f"{v}::string").alias(col)


def _sap(table: str):
    keys, scd, cols, extra = SAP[table]
    rules = {f"key_present_{k}": f"{k} IS NOT NULL" for k in keys} | extra

    @dlt.view(name=f"sap_{table}_changes")
    def _changes():
        src = (spark.readStream.table(f"{B}.sap_{table}_cdc")  # noqa: F821
               .dropDuplicates(["kafka_topic", "kafka_partition", "kafka_offset"]))
        return (src.select(*[_typed(c, t) for c, t in cols.items()], "op", "event_ts", "source_lsn", "kafka_offset",
                           "_batch_id")
                .withColumn("_seq", F.struct("source_lsn", "kafka_offset"))
                .withColumn("_dq_failed", _failed(rules)))

    _quarantine_flow("sap", table, f"sap_{table}_changes")

    @dlt.view(name=f"sap_{table}_valid")
    def _valid():
        return dlt.read_stream(f"sap_{table}_changes").where(F.size("_dq_failed") == 0)

    dlt.create_streaming_table(
        name=f"sap_{table}",
        comment=f"SAP {table.upper()} current state{' with full history (SCD2)' if scd == 2 else ''}, from CDC.")
    auto_cdc(target=f"sap_{table}", source=f"sap_{table}_valid", keys=keys, sequence_by="_seq",
             apply_as_deletes=F.expr("op = 'd'"), except_column_list=["op", "_seq", "_dq_failed", "kafka_offset"],
             stored_as_scd_type=scd)


for _t in SAP:
    _sap(_t)


# ====================================================================== Coupa
def _p(path: str, typ: str, alias: str):
    return F.expr(f"try_cast(payload:{path}::string as {typ})").alias(alias)


COUPA = {
    "suppliers": [("id", "bigint", "id"), ("number", "string", "number"), ("name", "string", "name"),
                  ("['display-name']", "string", "display_name"), ("['tax-id']", "string", "tax_id"),
                  ("country", "string", "country"), ("city", "string", "city"), ("commodity", "string", "commodity"),
                  ("status", "string", "status"), ("['diversity-class']", "string", "diversity_class"),
                  ("['created-at']", "timestamp", "created_at")],
    "purchase_orders": [("id", "bigint", "id"), ("['po-number']", "string", "po_number"),
                        ("supplier.id", "bigint", "supplier_id"), ("['company-code']", "string", "company_code"),
                        ("currency", "string", "currency"), ("total", "decimal(18,2)", "total"),
                        ("status", "string", "status"), ("['order-date']", "date", "order_date"),
                        ("['need-by-date']", "date", "need_by_date"), ("['created-at']", "timestamp", "created_at")],
    "receipts": [("id", "bigint", "id"), ("['order-line-id']", "bigint", "order_line_id"),
                 ("['received-date']", "date", "received_date"), ("quantity", "decimal(18,2)", "quantity"),
                 ("['created-at']", "timestamp", "created_at")],
    "invoices": [("id", "bigint", "id"), ("['invoice-number']", "string", "invoice_number"),
                 ("supplier.id", "bigint", "supplier_id"), ("['po-id']", "bigint", "po_id"),
                 ("['invoice-date']", "date", "invoice_date"), ("currency", "string", "currency"),
                 ("total", "decimal(18,2)", "total"), ("['payment-terms']", "string", "payment_terms"),
                 ("status", "string", "status"), ("['created-at']", "timestamp", "created_at")],
    "contracts": [("id", "bigint", "id"), ("number", "string", "number"), ("supplier.id", "bigint", "supplier_id"),
                  ("name", "string", "name"), ("['start-date']", "date", "start_date"),
                  ("['end-date']", "date", "end_date"), ("value", "decimal(18,2)", "value"),
                  ("currency", "string", "currency"), ("status", "string", "status"),
                  ("['created-at']", "timestamp", "created_at")],
}
COUPA_RULES = {
    "purchase_orders": {"supplier_present": "supplier_id IS NOT NULL", "total_not_negative": "total >= 0"},
    "invoices": {"supplier_present": "supplier_id IS NOT NULL", "total_positive": "total > 0"},
    "receipts": {"line_present": "order_line_id IS NOT NULL"},
}


def _coupa(entity: str):
    rules = {"id_present": "id IS NOT NULL", "updated_at_present": "updated_at IS NOT NULL"} | COUPA_RULES.get(entity, {})

    @dlt.view(name=f"coupa_{entity}_changes")
    def _changes():
        src = spark.readStream.table(f"{B}.coupa_{entity}").dropDuplicates(["id", "updated_at"])  # noqa: F821
        return (src.select(*[_p(p, t, a) for p, t, a in COUPA[entity]], "updated_at", "_batch_id")
                .withColumn("_dq_failed", _failed(rules)))

    _quarantine_flow("coupa", entity, f"coupa_{entity}_changes")

    @dlt.view(name=f"coupa_{entity}_valid")
    def _valid():
        return dlt.read_stream(f"coupa_{entity}_changes").where(F.size("_dq_failed") == 0)

    dlt.create_streaming_table(name=f"coupa_{entity}", comment=f"Coupa {entity}: latest version of each record.")
    auto_cdc(target=f"coupa_{entity}", source=f"coupa_{entity}_valid", keys=["id"], sequence_by="updated_at",
             except_column_list=["_dq_failed"], stored_as_scd_type=1)


for _e in COUPA:
    _coupa(_e)


@dlt.view
def coupa_order_lines_changes():
    src = spark.readStream.table(f"{B}.coupa_purchase_orders").dropDuplicates(["id", "updated_at"])  # noqa: F821
    lines = src.select(F.col("id").alias("po_id"), "updated_at",
                       F.expr("explode(cast(payload:['order-lines'] as array<variant>))").alias("l"))
    return lines.select(
        F.expr("l:id::bigint").alias("id"), "po_id", F.expr("l:['line-num']::int").alias("line_num"),
        F.expr("l:description::string").alias("description"), F.expr("l:commodity::string").alias("commodity"),
        F.expr("try_cast(l:quantity::string as decimal(18,2))").alias("quantity"),
        F.expr("try_cast(l:price::string as decimal(18,2))").alias("price"),
        F.expr("try_cast(l:total::string as decimal(18,2))").alias("total"), "updated_at")


dlt.create_streaming_table(name="coupa_order_lines", comment="Coupa PO lines, exploded from purchase orders.")
auto_cdc(target="coupa_order_lines", source="coupa_order_lines_changes", keys=["id"], sequence_by="updated_at",
         stored_as_scd_type=1)


# ====================================================================== HICX
HICX_COLS = ["hicx_id", "legal_name", "legal_name_local", "tax_id_type", "tax_id", "country", "city",
             "primary_category", "status", "onboarding_date"]


def _parse_xlsx(batches):
    import io

    import pandas as pd
    for pdf in batches:
        frames = []
        for _, r in pdf.iterrows():
            x = pd.read_excel(io.BytesIO(r["content"]), header=2, dtype=str, engine="openpyxl")  # 2 title rows
            x.columns = HICX_COLS
            x["export_date"], x["_file"] = r["export_date"], r["_file"]
            frames.append(x)
        out = pd.concat(frames) if frames else pd.DataFrame(columns=HICX_COLS + ["export_date", "_file"])
        yield out.where(out.notna(), None)


@dlt.table(comment="HICX supplier master from the latest weekly export workbook.")
def hicx_suppliers():
    raw = spark.read.table(f"{B}.hicx_supplier_exports").select("export_date", "content", "_file")  # noqa: F821
    schema = ", ".join(f"{c} string" for c in HICX_COLS + ["export_date", "_file"])
    df = raw.mapInPandas(_parse_xlsx, schema)
    latest = df.agg(F.max("export_date").alias("m"))
    return (df.join(latest, F.col("export_date") == F.col("m")).drop("m")
            .withColumn("onboarding_date", F.to_date(F.substring("onboarding_date", 1, 10)))
            .withColumn("export_date", F.to_date("export_date")))


@dlt.table(comment="HICX certifications from the latest export.")
def hicx_certifications():
    df = spark.read.table(f"{B}.hicx_certifications")  # noqa: F821
    latest = df.agg(F.max("export_date").alias("m"))
    return (df.join(latest, F.col("export_date") == F.col("m"))
            .select("hicx_id", "certificate_type", "certificate_number", "issuer",
                    F.to_date("issue_date").alias("issue_date"), F.to_date("expiry_date").alias("expiry_date"),
                    F.to_date("export_date").alias("export_date")))


@dlt.table(comment="HICX verified bank details from the latest export (IBAN masked by column mask in M8).")
def hicx_bank_details():
    df = spark.read.table(f"{B}.hicx_bank_details")  # noqa: F821
    latest = df.agg(F.max("export_date").alias("m"))
    return df.join(latest, F.col("export_date") == F.col("m")).select(
        "hicx_id", "iban", "bank_country", F.to_date("verified_on").alias("verified_on"))


# ====================================================================== OFAC
@dlt.table(comment="OFAC SDN entities and aliases, one row per name.")
def sanctions_names():
    def nz(c):
        return F.when(F.trim(F.col(c)) != "-0-", F.trim(F.col(c)))
    sdn = spark.read.table(f"{B}.ofac_sdn").select(  # noqa: F821
        F.col("_c0").cast("bigint").alias("ent_num"), nz("_c1").alias("name"), F.lit("primary").alias("name_type"),
        nz("_c2").alias("sdn_type"), nz("_c3").alias("program"), "snapshot_date")
    alt = spark.read.table(f"{B}.ofac_alt").select(  # noqa: F821
        F.col("_c0").cast("bigint").alias("ent_num"), nz("_c3").alias("name"), nz("_c2").alias("name_type"),
        "snapshot_date")
    alt = alt.join(sdn.select("ent_num", "sdn_type", "program"), "ent_num", "left")
    return sdn.unionByName(alt).where("name IS NOT NULL")


# ====================================================================== canonical supplier records
@F.udf(T.BooleanType())
def national_id_valid(id_type, value):
    import re
    if value is None or id_type is None:
        return None
    v = re.sub(r"\D", "", value) if id_type != "EIN" else value
    if id_type == "NIP":
        if len(v) != 10:
            return False
        c = sum(int(a) * b for a, b in zip(v, [6, 5, 7, 2, 3, 4, 5, 6, 7])) % 11
        return c != 10 and c == int(v[9])
    if id_type == "EDRPOU":
        if len(v) != 8:
            return False
        n = int(v)
        w = [1, 2, 3, 4, 5, 6, 7] if (n < 30000000 or n > 60000000) else [7, 1, 2, 3, 4, 5, 6]
        s = sum(int(a) * b for a, b in zip(v, w)) % 11
        if s == 10:
            s = sum(int(a) * (b + 2) for a, b in zip(v, w)) % 11
            s = 0 if s == 10 else s
        return s == int(v[7])
    if id_type == "EIN":
        return re.fullmatch(r"\d{2}-\d{7}", v) is not None
    return None


ID_TYPE_BY_COUNTRY = F.expr("CASE country WHEN 'PL' THEN 'NIP' WHEN 'UA' THEN 'EDRPOU' WHEN 'US' THEN 'EIN' END")


@dlt.table(comment="Every supplier record from every source in one canonical shape (input to entity resolution).")
def supplier_records():
    lfb1 = dlt.read("sap_lfb1").groupBy("lifnr").agg(F.sort_array(F.collect_set("bukrs")).alias("company_codes"))
    sap = (dlt.read("sap_lfa1").where("__END_AT IS NULL").join(lfb1, "lifnr", "left")
           .select(F.lit("sap").alias("source"), F.col("lifnr").alias("source_id"),
                   # SAP splits long names at 35 chars mid-word: join without a space when NAME1 is full
                   F.when(F.length("name1") == 35, F.concat_ws("", "name1", "name2"))
                    .otherwise(F.concat_ws(" ", "name1", "name2")).alias("name"),
                   F.lit(None).cast("string").alias("name_local"), F.col("land1").alias("country"),
                   F.col("ort01").alias("city"), F.col("pstlz").alias("postcode"), F.col("stras").alias("street"),
                   F.col("stcd1").alias("national_id"),
                   F.when(F.col("sperr") == "X", "blocked").when(F.col("loevm") == "X", "deleted")
                    .otherwise("active").alias("status"),
                   "company_codes", F.col("__START_AT").cast("string").alias("version")))
    coupa = dlt.read("coupa_suppliers").select(
        F.lit("coupa").alias("source"), F.col("id").cast("string").alias("source_id"), "name",
        F.lit(None).cast("string").alias("name_local"), "country", "city", F.lit(None).cast("string").alias("postcode"),
        F.lit(None).cast("string").alias("street"), F.col("tax_id").alias("national_id"), "status",
        F.lit(None).cast("array<string>").alias("company_codes"), F.col("updated_at").cast("string").alias("version"))
    hicx = dlt.read("hicx_suppliers").select(
        F.lit("hicx").alias("source"), F.col("hicx_id").alias("source_id"), F.col("legal_name").alias("name"),
        F.col("legal_name_local").alias("name_local"), "country", "city", F.lit(None).cast("string").alias("postcode"),
        F.lit(None).cast("string").alias("street"), F.col("tax_id").alias("national_id"), "status",
        F.lit(None).cast("array<string>").alias("company_codes"), F.col("export_date").cast("string").alias("version"))
    df = sap.unionByName(coupa).unionByName(hicx)
    return (df.withColumn("national_id_type", ID_TYPE_BY_COUNTRY)
              .withColumn("national_id_valid", national_id_valid("national_id_type", "national_id")))


# ====================================================================== DQ findings (soft rules)
EXPECTED_COUPA_SUPPLIER_FIELDS = ["id", "number", "name", "display-name", "tax-id", "country", "city", "commodity",
                                  "status", "created-at", "updated-at"]


@dlt.table(comment="One row per data-quality finding (soft rules). Feeds the DQ scorecard.")
def dq_findings():
    f = []
    sr = dlt.read("supplier_records")
    f.append(sr.where("national_id IS NULL AND national_id_type IS NOT NULL")
             .select("source", "source_id", F.lit("supplier").alias("entity"),
                     F.lit("missing_national_id").alias("rule"), F.lit("warn").alias("severity"),
                     F.concat(F.lit("no "), F.col("national_id_type"), F.lit(" for country "), F.col("country")).alias("detail")))
    f.append(sr.where("national_id_valid = false")
             .select("source", "source_id", F.lit("supplier").alias("entity"),
                     F.lit("invalid_national_id").alias("rule"), F.lit("error").alias("severity"),
                     F.concat(F.col("national_id_type"), F.lit(" fails validation: "), F.col("national_id")).alias("detail")))
    inv, po = dlt.read("coupa_invoices"), dlt.read("coupa_purchase_orders")
    j = inv.alias("i").join(po.alias("p"), F.col("i.po_id") == F.col("p.id"))
    f.append(j.where(F.abs(F.col("i.total") - F.col("p.total")) > 0.01)
             .select(F.lit("coupa").alias("source"), F.concat(F.lit("INV-"), F.col("i.id")).alias("source_id"),
                     F.lit("invoice").alias("entity"), F.lit("invoice_amount_mismatch").alias("rule"),
                     F.lit("error").alias("severity"),
                     F.format_string("invoice %s vs PO %s", F.col("i.total"), F.col("p.total")).alias("detail")))
    dup = inv.withColumn("n", F.row_number().over(
        Window.partitionBy("supplier_id", "invoice_number").orderBy("created_at", "id")))
    f.append(dup.where("n > 1").select(
        F.lit("coupa").alias("source"), F.concat(F.lit("INV-"), F.col("id")).alias("source_id"),
        F.lit("invoice").alias("entity"), F.lit("duplicate_invoice").alias("rule"), F.lit("error").alias("severity"),
        F.concat(F.lit("invoice number already received: "), F.col("invoice_number")).alias("detail")))
    certs = dlt.read("hicx_certifications").join(dlt.read("hicx_suppliers").select("hicx_id", "status"), "hicx_id")
    f.append(certs.where("expiry_date < export_date AND status = 'approved'").select(
        F.lit("hicx").alias("source"), F.col("certificate_number").alias("source_id"),
        F.lit("certification").alias("entity"), F.lit("expired_certificate").alias("rule"),
        F.lit("warn").alias("severity"),
        F.format_string("%s expired %s; supplier %s still approved", "certificate_type", "expiry_date",
                        "hicx_id").alias("detail")))
    keys = (spark.read.table(f"{B}.coupa_suppliers")  # noqa: F821
            .select(F.explode(F.expr("json_object_keys(to_json(payload))")).alias("field"), "updated_at")
            .where(~F.col("field").isin(EXPECTED_COUPA_SUPPLIER_FIELDS))
            .groupBy("field").agg(F.count(F.lit(1)).alias("n"), F.min("updated_at").alias("first_seen")))
    f.append(keys.select(F.lit("coupa").alias("source"), F.col("field").alias("source_id"),
                         F.lit("supplier").alias("entity"), F.lit("schema_drift").alias("rule"),
                         F.lit("info").alias("severity"),
                         F.format_string("new field '%s' on %d records since %s", "field", "n", "first_seen").alias("detail")))
    out = f[0]
    for x in f[1:]:
        out = out.unionByName(x)
    return out.withColumn("detected_at", F.current_timestamp())


@dlt.table(comment="DQ scorecard by source and rule: findings and quarantined rows.")
def dq_summary():
    findings = dlt.read("dq_findings").groupBy("source", "entity", "rule", "severity").agg(
        F.count(F.lit(1)).alias("findings"))
    q = dlt.read("dq_quarantine").select("source", "entity", F.explode("_dq_failed").alias("rule"))
    quarantined = q.groupBy("source", "entity", "rule").agg(F.count(F.lit(1)).alias("findings")) \
                   .withColumn("severity", F.lit("quarantined"))
    return findings.unionByName(quarantined)

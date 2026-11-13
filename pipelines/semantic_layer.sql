-- Databricks Wizard: semantic layer (Unity Catalog metric views).
-- KPI definitions live here once; Genie, the AI/BI dashboard and agents all query these views.
-- Run by the `semantic_layer` task of the wizard-ingest job (SQL file task on the serverless warehouse).
-- Note: join aliases avoid `source`/`supplier`/`category`: the fact tables have a `source` column, which collides
-- with the metric view's own `source.` alias in join conditions.

CREATE OR REPLACE VIEW wizard.gold.metrics_spend
COMMENT 'Spend metrics over PO lines (SAP direct + Coupa indirect), in USD at static FX rates.'
WITH METRICS LANGUAGE YAML AS $$
version: 0.1
source: wizard.gold.fact_po_line
joins:
  - name: sup
    source: wizard.gold.supplier_360
    using: [supplier_id]
  - name: cat
    source: wizard.gold.dim_category
    using: [category_id]
dimensions:
  - name: Order Month
    expr: DATE_TRUNC('MONTH', order_date)
  - name: Order Year
    expr: YEAR(order_date)
  - name: Category
    expr: cat.category_name
  - name: Spend Type
    expr: cat.spend_type
  - name: Company Code
    expr: company_code
  - name: Source System
    expr: source
  - name: Supplier
    expr: sup.legal_name
  - name: Supplier ID
    expr: supplier_id
  - name: Supplier Country
    expr: sup.country
measures:
  - name: Spend USD
    expr: SUM(amount_usd)
  - name: PO Lines
    expr: COUNT(1)
  - name: Active Suppliers
    expr: COUNT(DISTINCT supplier_id)
  - name: Average Line Value USD
    expr: AVG(amount_usd)
$$;

CREATE OR REPLACE VIEW wizard.gold.metrics_delivery
COMMENT 'Delivery performance over received PO lines: on-time rate and lateness.'
WITH METRICS LANGUAGE YAML AS $$
version: 0.1
source: wizard.gold.fact_delivery
joins:
  - name: sup
    source: wizard.gold.supplier_360
    using: [supplier_id]
  - name: cat
    source: wizard.gold.dim_category
    using: [category_id]
dimensions:
  - name: Receipt Month
    expr: DATE_TRUNC('MONTH', received_date)
  - name: Category
    expr: cat.category_name
  - name: Company Code
    expr: company_code
  - name: Supplier
    expr: sup.legal_name
  - name: Supplier ID
    expr: supplier_id
  - name: Deteriorating Supplier
    expr: sup.deteriorating
measures:
  - name: Receipts
    expr: COUNT(1)
  - name: On-Time Rate
    expr: AVG(CAST(on_time AS INT))
  - name: Average Days Late
    expr: AVG(GREATEST(days_late, 0))
  - name: Late Receipts
    expr: COUNT_IF(NOT on_time)
$$;

CREATE OR REPLACE VIEW wizard.gold.metrics_supplier
COMMENT 'Supplier portfolio metrics: risk, data quality, spend, deterioration (one row per golden supplier).'
WITH METRICS LANGUAGE YAML AS $$
version: 0.1
source: wizard.gold.supplier_360
dimensions:
  - name: Risk Band
    expr: risk_band
  - name: Country
    expr: country
  - name: Main Category
    expr: main_category
  - name: Deteriorating
    expr: deteriorating
  - name: Purchasing Blocked
    expr: purchasing_blocked
  - name: Systems Count
    expr: SIZE(source_systems)
measures:
  - name: Suppliers
    expr: COUNT(1)
  - name: High Risk Suppliers
    expr: COUNT_IF(risk_band = 'high')
  - name: Average Risk Score
    expr: AVG(risk_score)
  - name: Average DQ Score
    expr: AVG(dq_score)
  - name: Suppliers With DQ Issues
    expr: COUNT_IF(SIZE(dq_issues) > 0)
  - name: Lifetime Spend USD
    expr: SUM(lifetime_spend_usd)
$$;

-- ===================================================================== agent support (M7)
-- Lineage snapshot for the MCP server's trace_metric tool (the app's service principal cannot read system tables).
CREATE OR REPLACE TABLE wizard.ops.lineage_edges
COMMENT 'Table-level lineage within the wizard catalog, from system.access.table_lineage (refreshed every run).'
AS SELECT source_table_full_name AS source, target_table_full_name AS target, max(event_time) AS last_seen
FROM system.access.table_lineage
WHERE target_table_catalog = 'wizard' AND source_table_full_name IS NOT NULL
  AND source_table_full_name <> target_table_full_name
GROUP BY ALL;

CREATE TABLE IF NOT EXISTS wizard.ops.agent_tool_calls (
  called_at TIMESTAMP, server STRING, tool STRING, arguments STRING, caller STRING, ok BOOLEAN,
  rows_returned INT, millis INT, error STRING)
COMMENT 'Audit log: every MCP tool call made against Databricks Wizard.';

CREATE TABLE IF NOT EXISTS wizard.ops.agent_proposals (
  proposal_id STRING, kind STRING, item_key STRING, decision STRING, rationale STRING, proposed_by STRING,
  status STRING, created_at TIMESTAMP, reviewed_by STRING, reviewed_at TIMESTAMP, review_note STRING)
COMMENT 'Decisions proposed by agents through the stewardship MCP server. Only a person approves them (steward app).';

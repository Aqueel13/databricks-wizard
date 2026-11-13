-- Databricks Wizard: gold layer (Lakeflow Declarative Pipeline, SQL).
-- Business-ready tables keyed on the resolved supplier_id from entity resolution.
-- ${catalog} is set in the pipeline configuration.

-- ===================================================================== reference
CREATE OR REFRESH MATERIALIZED VIEW dim_fx
COMMENT 'Static FX rates to USD (simplification; SAP TCURR parsing is a later improvement).'
AS SELECT * FROM VALUES ('USD', 1.0), ('EUR', 1.08), ('PLN', 0.2512), ('UAH', 0.0240) AS fx(currency, usd_per_unit);

CREATE OR REFRESH MATERIALIZED VIEW dim_category
COMMENT 'Spend categories: direct ingredients (SAP material groups) and the five indirect categories (Coupa).'
AS SELECT * FROM VALUES
  ('DIRECT_INGREDIENTS', 'Direct ingredients', 'direct'),
  ('IT_SERVICES', 'IT services', 'indirect'), ('MRO', 'Maintenance, repair & operations', 'indirect'),
  ('LOGISTICS', 'Logistics', 'indirect'), ('PROFESSIONAL_SERVICES', 'Professional services', 'indirect'),
  ('FACILITIES', 'Facilities', 'indirect') AS c(category_id, category_name, spend_type);

-- ===================================================================== facts
CREATE OR REFRESH MATERIALIZED VIEW fact_po_line
COMMENT 'One row per PO line from SAP (direct) and Coupa (indirect), keyed on the golden supplier.'
AS
WITH sap AS (
  SELECT 'sap' AS source, p.ebeln AS po_number, concat(p.ebeln, '/', p.ebelp) AS line_key, k.lifnr AS source_supplier_id,
         k.bukrs AS company_code, 'DIRECT_INGREDIENTS' AS category_id, t.wgbez AS subcategory, p.txz01 AS description,
         k.bedat AS order_date, s.promised_date, p.menge AS quantity, p.netwr AS amount_local, k.waers AS currency
  FROM ${catalog}.silver.sap_ekpo p
  JOIN ${catalog}.silver.sap_ekko k ON k.ebeln = p.ebeln
  LEFT JOIN ${catalog}.silver.sap_t023t t ON t.matkl = p.matkl
  LEFT JOIN (SELECT ebeln, ebelp, min(eindt) promised_date FROM ${catalog}.silver.sap_eket GROUP BY ALL) s
         ON s.ebeln = p.ebeln AND s.ebelp = p.ebelp
  WHERE p.loekz IS NULL AND k.loekz IS NULL
),
coupa AS (
  SELECT 'coupa', o.po_number, concat(o.po_number, '/', l.line_num), cast(o.supplier_id AS STRING), o.company_code,
         l.commodity, NULL, l.description, o.order_date, o.need_by_date, l.quantity, l.total, o.currency
  FROM ${catalog}.silver.coupa_order_lines l
  JOIN ${catalog}.silver.coupa_purchase_orders o ON o.id = l.po_id
)
SELECT x.*, c.supplier_id, round(x.amount_local * fx.usd_per_unit, 2) AS amount_usd
FROM (SELECT * FROM sap UNION ALL SELECT * FROM coupa) x
LEFT JOIN ${catalog}.silver.er_clusters c ON c.source = x.source AND c.source_id = x.source_supplier_id
LEFT JOIN dim_fx fx ON fx.currency = x.currency;

CREATE OR REFRESH MATERIALIZED VIEW fact_delivery
COMMENT 'One row per received PO line: promised vs first receipt date. days_late > 0 means late.'
AS
WITH sap AS (
  SELECT 'sap' AS source, concat(ebeln, '/', ebelp) AS line_key, min(budat) AS received_date
  FROM ${catalog}.silver.sap_ekbe WHERE vgabe = '1' GROUP BY ALL
),
coupa AS (
  SELECT 'coupa', concat(o.po_number, '/', l.line_num), min(r.received_date)
  FROM ${catalog}.silver.coupa_receipts r
  JOIN ${catalog}.silver.coupa_order_lines l ON l.id = r.order_line_id
  JOIN ${catalog}.silver.coupa_purchase_orders o ON o.id = l.po_id
  GROUP BY ALL
)
SELECT f.source, f.line_key, f.supplier_id, f.company_code, f.category_id, f.order_date, f.promised_date,
       d.received_date, datediff(d.received_date, f.promised_date) AS days_late,
       d.received_date <= f.promised_date AS on_time
FROM (SELECT * FROM sap UNION ALL SELECT * FROM coupa) d
JOIN fact_po_line f ON f.source = d.source AND f.line_key = d.line_key
WHERE f.promised_date IS NOT NULL;

CREATE OR REFRESH MATERIALIZED VIEW fact_invoice
COMMENT 'Invoices from SAP (RBKP) and Coupa, keyed on the golden supplier.'
AS
WITH x AS (
  SELECT 'sap' AS source, concat(belnr, '/', gjahr) AS invoice_key, lifnr AS source_supplier_id, bukrs AS company_code,
         budat AS invoice_date, rmwwr AS amount_local, waers AS currency
  FROM ${catalog}.silver.sap_rbkp
  UNION ALL
  SELECT 'coupa', invoice_number || '#' || id, cast(supplier_id AS STRING), NULL, invoice_date, total, currency
  FROM ${catalog}.silver.coupa_invoices
)
SELECT x.*, c.supplier_id, round(x.amount_local * fx.usd_per_unit, 2) AS amount_usd
FROM x LEFT JOIN ${catalog}.silver.er_clusters c ON c.source = x.source AND c.source_id = x.source_supplier_id
LEFT JOIN dim_fx fx ON fx.currency = x.currency;


-- ===================================================================== documents
CREATE OR REFRESH MATERIALIZED VIEW supplier_documents
COMMENT 'Every supplier document with its extracted fields (silver.doc_extractions), linked to the golden supplier, next to what the system of record says (HICX certification / Coupa contract). doc_key comes from the file path, not from extraction, so an extraction error cannot hide a mismatch.'
AS
WITH x AS (
  SELECT e.doc_id, e.file_name, regexp_replace(e.file_name, '\\.pdf$', '') AS doc_key, e.doc_type, e.owner_id, e.model,
         CASE WHEN e.doc_type = 'certificate' THEN e.fields:holder::string ELSE e.fields:supplier::string END AS party_name,
         coalesce(e.fields:certificate_number::string, e.fields:agreement_number::string) AS extracted_number,
         try_cast(coalesce(e.fields:issue_date::string, e.fields:start_date::string) AS DATE) AS start_date,
         try_cast(coalesce(e.fields:expiry_date::string, e.fields:end_date::string) AS DATE) AS end_date,
         e.fields:certificate_type::string AS certificate_type, e.fields:issuer::string AS issuer,
         try_cast(e.fields:contract_value::string AS DECIMAL(18, 2)) AS contract_value, e.fields:currency::string AS currency,
         try_cast(e.fields:payment_terms_days::string AS INT) AS payment_terms_days,
         try_cast(e.fields:notice_days::string AS INT) AS notice_days,
         e.fields:liability_cap::string AS liability_cap, e.fields:governing_law::string AS governing_law
  FROM ${catalog}.silver.doc_extractions e
),
owner AS (
  SELECT 'certificate' AS doc_type, source_id AS owner_id, supplier_id FROM ${catalog}.silver.er_clusters WHERE source = 'hicx'
  UNION ALL
  SELECT 'contract', s.number, c.supplier_id
  FROM ${catalog}.silver.coupa_suppliers s
  JOIN ${catalog}.silver.er_clusters c ON c.source = 'coupa' AND c.source_id = cast(s.id AS STRING)
)
SELECT x.*, o.supplier_id,
       h.expiry_date AS system_expiry_date, k.value AS system_contract_value, k.end_date AS system_end_date,
       CASE WHEN x.doc_type = 'certificate' THEN x.end_date <> h.expiry_date
            ELSE abs(x.contract_value - k.value) > 0.01 OR x.end_date <> k.end_date END AS metadata_mismatch,
       x.extracted_number <> x.doc_key AS number_mismatch
FROM x
LEFT JOIN owner o ON o.doc_type = x.doc_type AND o.owner_id = x.owner_id
LEFT JOIN ${catalog}.silver.hicx_certifications h ON x.doc_type = 'certificate' AND h.certificate_number = x.doc_key
LEFT JOIN ${catalog}.silver.coupa_contracts k ON x.doc_type = 'contract' AND k.number = x.doc_key;

CREATE OR REFRESH MATERIALIZED VIEW document_findings
COMMENT 'Documents that disagree with the system of record: certificate expiry (HICX) or contract value / end date (Coupa).'
AS
SELECT supplier_id, doc_type, doc_key, file_name,
       CASE WHEN doc_type = 'certificate' THEN 'certificate_metadata_mismatch' ELSE 'contract_metadata_mismatch' END AS rule,
       CASE WHEN doc_type = 'certificate'
            THEN format_string('document expiry %s vs HICX %s', end_date, system_expiry_date)
            ELSE format_string('document value %s %s / end %s vs Coupa %s / %s', currency, contract_value, end_date,
                               system_contract_value, system_end_date) END AS detail
FROM supplier_documents WHERE metadata_mismatch;

-- ===================================================================== KPIs
CREATE OR REFRESH MATERIALIZED VIEW supplier_kpi_monthly
COMMENT 'Per supplier and month: receipts, on-time %, average days late, spend (USD).'
AS
WITH d AS (
  SELECT supplier_id, date_trunc('MONTH', received_date) AS month, count(*) AS receipts,
         avg(CAST(on_time AS INT)) AS on_time_pct, avg(greatest(days_late, 0)) AS avg_days_late
  FROM fact_delivery WHERE supplier_id IS NOT NULL GROUP BY ALL
),
s AS (
  SELECT supplier_id, date_trunc('MONTH', order_date) AS month, sum(amount_usd) AS spend_usd, count(*) AS po_lines
  FROM fact_po_line WHERE supplier_id IS NOT NULL GROUP BY ALL
)
SELECT coalesce(d.supplier_id, s.supplier_id) AS supplier_id, coalesce(d.month, s.month) AS month,
       coalesce(d.receipts, 0) AS receipts, round(d.on_time_pct, 4) AS on_time_pct,
       round(d.avg_days_late, 2) AS avg_days_late, round(coalesce(s.spend_usd, 0), 2) AS spend_usd,
       coalesce(s.po_lines, 0) AS po_lines
FROM d FULL OUTER JOIN s ON d.supplier_id = s.supplier_id AND d.month = s.month;

CREATE OR REFRESH MATERIALIZED VIEW supplier_delivery_trend
COMMENT 'Delivery deterioration: last 6 months vs the supplier''s own prior 12 months, adjusted for the market. Deteriorating = on-time rate down >= 10 points AND market-adjusted lateness up >= 5 days with one-sided significance (z >= 1.64), on >= 5 recent and >= 8 baseline receipts. Absolute thresholds are meaningless here (market on-time ~60% and swinging 31-77% month to month).'
AS
WITH f AS (
  SELECT supplier_id, received_date, CAST(on_time AS INT) AS ot,
         least(greatest(days_late, -15), 45) AS dl,                      -- clip extremes (e.g. receipts years early)
         date_trunc('MONTH', received_date) AS m
  FROM fact_delivery WHERE supplier_id IS NOT NULL
),
mkt AS (SELECT m, avg(dl) AS market_dl FROM f GROUP BY m),
asof AS (SELECT max(received_date) AS d FROM f),
x AS (
  SELECT f.supplier_id, f.ot, f.dl, f.dl - mkt.market_dl AS excess_dl, a.d AS as_of_date,
         CASE WHEN f.received_date >= a.d - INTERVAL 180 DAYS THEN 'recent'
              WHEN f.received_date >= a.d - INTERVAL 540 DAYS THEN 'baseline' END AS w
  FROM f JOIN mkt USING (m) CROSS JOIN asof a
),
s AS (
  SELECT supplier_id, max(as_of_date) AS as_of_date,
         count_if(w = 'baseline') AS n_baseline, count_if(w = 'recent') AS n_recent,
         avg(CASE WHEN w = 'baseline' THEN ot END) AS on_time_baseline,
         avg(CASE WHEN w = 'recent' THEN ot END) AS on_time_recent,
         avg(CASE WHEN w = 'baseline' THEN dl END) AS days_late_baseline,
         avg(CASE WHEN w = 'recent' THEN dl END) AS days_late_recent,
         avg(CASE WHEN w = 'recent' THEN excess_dl END) - avg(CASE WHEN w = 'baseline' THEN excess_dl END) AS excess_days_late_increase,
         try_divide(avg(CASE WHEN w = 'recent' THEN excess_dl END) - avg(CASE WHEN w = 'baseline' THEN excess_dl END),
                    sqrt(try_divide(coalesce(var_samp(CASE WHEN w = 'recent' THEN excess_dl END), 250), count_if(w = 'recent'))
                       + try_divide(coalesce(var_samp(CASE WHEN w = 'baseline' THEN excess_dl END), 250), count_if(w = 'baseline')))) AS z_score
  FROM x GROUP BY supplier_id
)
SELECT supplier_id, as_of_date, n_baseline, n_recent,
       round(on_time_baseline, 4) AS on_time_baseline, round(on_time_recent, 4) AS on_time_recent,
       round(on_time_baseline - on_time_recent, 4) AS on_time_drop,
       round(days_late_baseline, 1) AS days_late_baseline, round(days_late_recent, 1) AS days_late_recent,
       round(excess_days_late_increase, 1) AS excess_days_late_increase, round(z_score, 2) AS z_score,
       coalesce(n_baseline >= 8 AND n_recent >= 5 AND on_time_baseline - on_time_recent >= 0.10
                AND excess_days_late_increase >= 5 AND z_score >= 1.64, false) AS deteriorating,
       CASE WHEN n_baseline < 8 OR n_recent < 5 THEN 'insufficient deliveries to assess' END AS assessment_note
FROM s;

CREATE OR REFRESH MATERIALIZED VIEW spend_concentration
COMMENT 'Spend concentration per category over the last 12 months: Herfindahl-Hirschman index (0-10,000) and top-supplier shares.'
AS
WITH asof AS (SELECT max(order_date) AS d FROM fact_po_line),
sup AS (
  SELECT f.category_id, f.supplier_id, sum(f.amount_usd) AS spend
  FROM fact_po_line f CROSS JOIN asof a
  WHERE f.order_date > a.d - INTERVAL 365 DAYS AND f.supplier_id IS NOT NULL GROUP BY ALL
),
ranked AS (
  SELECT *, spend / sum(spend) OVER (PARTITION BY category_id) AS share,
         row_number() OVER (PARTITION BY category_id ORDER BY spend DESC) AS rnk
  FROM sup
)
SELECT r.category_id, c.category_name, c.spend_type, round(sum(r.spend), 2) AS spend_usd_12m,
       count(*) AS suppliers, round(sum(pow(r.share * 100, 2)), 0) AS hhi,
       round(max(CASE WHEN rnk = 1 THEN share END), 4) AS top1_share,
       round(sum(CASE WHEN rnk <= 3 THEN share END), 4) AS top3_share,
       max_by(r.supplier_id, r.spend) AS top_supplier_id,
       CASE WHEN sum(pow(r.share * 100, 2)) >= 2500 THEN 'highly concentrated'
            WHEN sum(pow(r.share * 100, 2)) >= 1500 THEN 'moderately concentrated' ELSE 'competitive' END AS concentration
FROM ranked r JOIN dim_category c ON c.category_id = r.category_id
GROUP BY ALL;

-- ===================================================================== data quality per supplier
CREATE OR REFRESH MATERIALIZED VIEW supplier_dq_scorecard
COMMENT 'Master-data quality per golden supplier: completeness, validity, consistency (0-100), duplicates, and the full list of issues. has_issues = size(issues) > 0.'
AS
WITH rec AS (
  SELECT c.supplier_id, r.*
  FROM ${catalog}.silver.supplier_records r
  JOIN ${catalog}.silver.er_clusters c ON c.source = r.source AND c.source_id = r.source_id
),
f AS (
  SELECT c.supplier_id, collect_set(f.rule) AS rules, count(*) AS n
  FROM ${catalog}.silver.dq_findings f
  JOIN ${catalog}.silver.er_clusters c ON c.source = f.source AND c.source_id = f.source_id
  GROUP BY ALL
),
x AS (
  SELECT supplier_id, collect_set(rule) AS rules, count(*) AS n
  FROM (SELECT supplier_id, rule FROM ${catalog}.silver.er_dq_findings
        UNION ALL SELECT supplier_id, rule FROM document_findings WHERE supplier_id IS NOT NULL)
  GROUP BY ALL
),
agg AS (
  SELECT supplier_id, count(*) AS records, count(DISTINCT source) AS systems,
         count(*) - count(DISTINCT source) AS duplicate_records,
         -- completeness: national id (where the country has one), city
         avg((CASE WHEN national_id_type IS NULL OR national_id IS NOT NULL THEN 1 ELSE 0 END
              + CASE WHEN city IS NOT NULL THEN 1 ELSE 0 END) / 2.0) AS completeness,
         coalesce(avg(CASE WHEN national_id IS NOT NULL THEN CAST(coalesce(national_id_valid, true) AS INT) END), 1) AS validity,
         count_if(national_id_type IS NOT NULL AND national_id IS NULL) AS missing_id_records,
         count_if(city IS NULL) AS missing_city_records,
         count_if(national_id_valid = false) AS invalid_id_records
  FROM rec GROUP BY supplier_id
)
SELECT a.supplier_id, a.records, a.systems, a.duplicate_records,
       round(100 * a.completeness, 1) AS completeness,
       round(100 * a.validity, 1) AS validity,
       round(100 * greatest(0, 1 - coalesce(x.n, 0) / a.records - CASE WHEN d.conflicting_national_ids = 1 THEN 0.5 ELSE 0 END), 1) AS consistency,
       round(greatest(0, (100 * a.completeness + 100 * a.validity
              + 100 * greatest(0, 1 - coalesce(x.n, 0) / a.records - CASE WHEN d.conflicting_national_ids = 1 THEN 0.5 ELSE 0 END)) / 3
              - 10 * a.duplicate_records), 1) AS dq_score,
       -- issues is the single source of truth: every problem the scores reflect appears here, so
       -- "suppliers with master data issues" = size(issues) > 0
       array_sort(array_distinct(concat(coalesce(f.rules, array()), coalesce(x.rules, array()),
              CASE WHEN a.missing_id_records > 0 THEN array('missing_national_id') ELSE array() END,
              CASE WHEN a.invalid_id_records > 0 THEN array('invalid_national_id') ELSE array() END,
              CASE WHEN a.missing_city_records > 0 THEN array('missing_city') ELSE array() END,
              CASE WHEN a.duplicate_records > 0 THEN array('duplicate_records') ELSE array() END,
              CASE WHEN d.conflicting_national_ids = 1 THEN array('conflicting_national_ids') ELSE array() END))) AS issues,
       size(array_distinct(concat(coalesce(f.rules, array()), coalesce(x.rules, array())))) > 0
         OR a.missing_id_records > 0 OR a.invalid_id_records > 0 OR a.missing_city_records > 0
         OR a.duplicate_records > 0 OR d.conflicting_national_ids = 1 AS has_issues
FROM agg a
JOIN ${catalog}.gold.dim_supplier d ON d.supplier_id = a.supplier_id
LEFT JOIN f ON f.supplier_id = a.supplier_id
LEFT JOIN x ON x.supplier_id = a.supplier_id;

-- ===================================================================== risk
CREATE OR REFRESH MATERIALIZED VIEW supplier_risk_score
COMMENT 'Supplier risk 0-100 = 30% delivery + 30% compliance + 20% concentration + 20% data quality. Bands: high >= 30 (two significant factors), medium >= 15 (one). Open sanctions alerts count as pending review (50), only steward-confirmed matches as 100. No financial component yet (no financial source loaded).'
AS
WITH spend AS (
  SELECT supplier_id, category_id, sum(amount_usd) AS spend FROM fact_po_line WHERE supplier_id IS NOT NULL GROUP BY ALL
),
share AS (
  SELECT supplier_id, max(spend / cat_total) AS max_category_share, sum(spend) AS spend_usd
  FROM (SELECT *, sum(spend) OVER (PARTITION BY category_id) AS cat_total FROM spend) GROUP BY supplier_id
),
certs AS (
  -- the certificate document itself outranks the HICX field when they disagree
  SELECT c.supplier_id, count(*) AS expired_certificates
  FROM ${catalog}.silver.hicx_certifications h
  JOIN ${catalog}.silver.hicx_suppliers s ON s.hicx_id = h.hicx_id
  JOIN ${catalog}.silver.er_clusters c ON c.source = 'hicx' AND c.source_id = h.hicx_id
  LEFT JOIN supplier_documents d ON d.doc_type = 'certificate' AND d.doc_key = h.certificate_number
  WHERE coalesce(d.end_date, h.expiry_date) < h.export_date AND s.status = 'approved' GROUP BY ALL
),
alerts AS (
  SELECT supplier_id, count_if(status = 'open') AS sanctions_alerts, count_if(status = 'confirmed') AS sanctions_confirmed
  FROM ${catalog}.gold.supplier_sanctions_hits GROUP BY ALL
),
parts AS (
  SELECT d.supplier_id,
         -- delivery: only with enough evidence (>= 5 recent receipts): statistically tested deterioration (60)
         -- plus recent lateness (40). Small samples score 0 rather than swinging to extremes.
         CASE WHEN coalesce(t.n_recent, 0) >= 5
              THEN round(least(100, CASE WHEN t.deteriorating THEN 60 ELSE 0 END + 40 * (1 - t.on_time_recent)), 1)
              ELSE 0 END AS delivery_risk,
         -- compliance: CONFIRMED sanctions match (100) > open screening alert, a name similarity pending review (50)
         --             > expired certificate while approved (60 wins over an open alert) > purchasing block (40)
         CASE WHEN coalesce(a.sanctions_confirmed, 0) > 0 THEN 100 WHEN coalesce(c.expired_certificates, 0) > 0 THEN 60
              WHEN coalesce(a.sanctions_alerts, 0) > 0 THEN 50 WHEN d.purchasing_blocked THEN 40 ELSE 0 END AS compliance_risk,
         round(least(100, 100 * coalesce(sh.max_category_share, 0) / 0.30), 1) AS concentration_risk,
         round(100 - coalesce(q.dq_score, 100), 1) AS data_quality_risk,
         coalesce(t.deteriorating, false) AS deteriorating, coalesce(a.sanctions_alerts, 0) AS sanctions_alerts,
         coalesce(a.sanctions_confirmed, 0) AS sanctions_confirmed,
         coalesce(c.expired_certificates, 0) AS expired_certificates, coalesce(sh.spend_usd, 0) AS spend_usd,
         sh.max_category_share
  FROM ${catalog}.gold.dim_supplier d
  LEFT JOIN supplier_delivery_trend t ON t.supplier_id = d.supplier_id
  LEFT JOIN share sh ON sh.supplier_id = d.supplier_id
  LEFT JOIN certs c ON c.supplier_id = d.supplier_id
  LEFT JOIN alerts a ON a.supplier_id = d.supplier_id
  LEFT JOIN supplier_dq_scorecard q ON q.supplier_id = d.supplier_id
)
SELECT *,
       round(0.3 * delivery_risk + 0.3 * compliance_risk + 0.2 * concentration_risk + 0.2 * data_quality_risk, 1) AS risk_score,
       CASE WHEN 0.3 * delivery_risk + 0.3 * compliance_risk + 0.2 * concentration_risk + 0.2 * data_quality_risk >= 30 THEN 'high'
            WHEN 0.3 * delivery_risk + 0.3 * compliance_risk + 0.2 * concentration_risk + 0.2 * data_quality_risk >= 15 THEN 'medium'
            ELSE 'low' END AS risk_band,
       filter(array(CASE WHEN deteriorating THEN 'delivery performance deteriorating' END,
                    CASE WHEN sanctions_confirmed > 0 THEN 'confirmed sanctions match' END,
                    CASE WHEN sanctions_alerts > 0 THEN 'sanctions screening alert pending review' END,
                    CASE WHEN expired_certificates > 0 THEN 'expired certificate while approved' END,
                    CASE WHEN concentration_risk >= 70 THEN 'high share of category spend' END,
                    CASE WHEN data_quality_risk >= 30 THEN 'poor master data quality' END), x -> x IS NOT NULL) AS drivers,
       'financial risk not assessed (no financial data source loaded)' AS limitations
FROM parts;

-- ===================================================================== Supplier 360
CREATE OR REFRESH MATERIALIZED VIEW supplier_360
COMMENT 'One row per golden supplier: identity, spend, delivery trend, risk and data quality. The main table for Genie and agents.'
AS
WITH cat AS (
  SELECT supplier_id, max_by(category_id, spend) AS main_category, sum(spend) AS lifetime_spend_usd,
         min(first_order) AS first_order, max(last_order) AS last_order
  FROM (SELECT supplier_id, category_id, sum(amount_usd) AS spend, min(order_date) AS first_order,
               max(order_date) AS last_order
        FROM fact_po_line WHERE supplier_id IS NOT NULL GROUP BY ALL)
  GROUP BY supplier_id
)
SELECT d.supplier_id, d.legal_name, d.legal_name_local, d.country, d.city, d.national_id_type, d.national_id,
       d.source_systems, d.source_records, d.purchasing_blocked,
       c.main_category, round(c.lifetime_spend_usd, 2) AS lifetime_spend_usd, c.first_order, c.last_order,
       t.on_time_baseline, t.on_time_recent, t.on_time_drop, t.n_recent AS recent_receipts, t.deteriorating,
       r.risk_score, r.risk_band, r.drivers AS risk_drivers, r.delivery_risk, r.compliance_risk,
       r.concentration_risk, r.data_quality_risk, r.sanctions_alerts, r.expired_certificates,
       q.dq_score, q.completeness, q.validity, q.consistency, q.duplicate_records, q.issues AS dq_issues
FROM ${catalog}.gold.dim_supplier d
LEFT JOIN cat c ON c.supplier_id = d.supplier_id
LEFT JOIN supplier_delivery_trend t ON t.supplier_id = d.supplier_id
LEFT JOIN supplier_risk_score r ON r.supplier_id = d.supplier_id
LEFT JOIN supplier_dq_scorecard q ON q.supplier_id = d.supplier_id;

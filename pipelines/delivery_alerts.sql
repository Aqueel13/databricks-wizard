-- Databricks Wizard: delivery deterioration alerts (job task after gold).
-- gold.supplier_delivery_trend says which suppliers are deteriorating *now*. This table remembers *when* each one
-- started, so consumers can ask "what is new since my last look?": WatchDog polls it through the MCP tool
-- list_delivery_alerts. One open alert per supplier. It is resolved when the supplier no longer meets the rule.
-- supplier_id is stable across runs (hash of the cluster's smallest member), so alerts survive re-resolution.

CREATE TABLE IF NOT EXISTS wizard.gold.supplier_delivery_alerts (
  alert_id STRING NOT NULL,
  supplier_id STRING NOT NULL,
  legal_name STRING,
  country STRING,
  status STRING NOT NULL COMMENT 'open | resolved',
  opened_at TIMESTAMP NOT NULL COMMENT 'first run that found the supplier deteriorating',
  resolved_at TIMESTAMP COMMENT 'first run that no longer did',
  last_evaluated_at TIMESTAMP NOT NULL,
  as_of_date DATE COMMENT 'latest goods receipt in the data when last evaluated',
  on_time_baseline DOUBLE COMMENT 'on-time rate, prior 12 months',
  on_time_recent DOUBLE COMMENT 'on-time rate, last 6 months',
  on_time_drop DOUBLE,
  excess_days_late_increase DOUBLE COMMENT 'increase in lateness beyond the market average, days',
  z_score DOUBLE,
  n_recent BIGINT,
  n_baseline BIGINT,
  lifetime_spend_usd DOUBLE,
  risk_band STRING
)
COMMENT 'Delivery deterioration alerts with open/resolved history (rule: gold.supplier_delivery_trend). An alert says delivery performance got worse relative to the market; it says nothing about the cause.'
TBLPROPERTIES ('certified' = 'true');

MERGE INTO wizard.gold.supplier_delivery_alerts a
USING (
  SELECT t.supplier_id, s.legal_name, s.country, t.as_of_date, t.on_time_baseline, t.on_time_recent, t.on_time_drop,
         t.excess_days_late_increase, t.z_score, t.n_recent, t.n_baseline, s.lifetime_spend_usd, s.risk_band
  FROM wizard.gold.supplier_delivery_trend t
  JOIN wizard.gold.supplier_360 s USING (supplier_id)
  WHERE t.deteriorating
) d
ON a.supplier_id = d.supplier_id AND a.status = 'open'
WHEN MATCHED THEN UPDATE SET
  legal_name = d.legal_name, country = d.country, last_evaluated_at = current_timestamp(),
  as_of_date = d.as_of_date, on_time_baseline = d.on_time_baseline, on_time_recent = d.on_time_recent,
  on_time_drop = d.on_time_drop, excess_days_late_increase = d.excess_days_late_increase, z_score = d.z_score,
  n_recent = d.n_recent, n_baseline = d.n_baseline, lifetime_spend_usd = d.lifetime_spend_usd,
  risk_band = d.risk_band
WHEN NOT MATCHED THEN INSERT (alert_id, supplier_id, legal_name, country, status, opened_at, resolved_at,
                              last_evaluated_at, as_of_date, on_time_baseline, on_time_recent, on_time_drop,
                              excess_days_late_increase, z_score, n_recent, n_baseline, lifetime_spend_usd, risk_band)
VALUES ('DA-' || upper(substr(sha2(d.supplier_id || cast(current_timestamp() AS STRING), 256), 1, 12)),
        d.supplier_id, d.legal_name, d.country, 'open', current_timestamp(), NULL, current_timestamp(),
        d.as_of_date, d.on_time_baseline, d.on_time_recent, d.on_time_drop, d.excess_days_late_increase, d.z_score,
        d.n_recent, d.n_baseline, d.lifetime_spend_usd, d.risk_band)
WHEN NOT MATCHED BY SOURCE AND a.status = 'open' THEN UPDATE SET
  status = 'resolved', resolved_at = current_timestamp(), last_evaluated_at = current_timestamp();

-- Databricks Wizard: governance applied on every run (idempotent), after entity resolution and gold.
-- Masks: stewards (workspace group wizard_stewards) see real values; everyone else, including the agents' MCP
-- service principal, sees masked values. Re-applied every run so a table rewrite can never drop a mask.
-- Note: is_member() checks workspace groups (Free Edition); is_account_group_member() would check account groups.
-- Functions are created IF NOT EXISTS: replacing a mask/filter function briefly invalidates every table that uses it,
-- so concurrent readers (Genie, agents, other tasks) would fail. To change one deliberately, run
-- CREATE OR REPLACE FUNCTION by hand in a quiet window.

CREATE FUNCTION IF NOT EXISTS wizard.ops.mask_national_id(id STRING)
RETURNS STRING
COMMENT 'Tax / registration IDs. Stewards: full value. Others: last 4 characters; US SSN-format values fully masked.'
RETURN CASE
  WHEN id IS NULL THEN NULL
  WHEN is_member('wizard_stewards') THEN id
  WHEN id RLIKE '^[0-9]{3}-[0-9]{2}-[0-9]{4}$' THEN 'XXX-XX-XXXX'
  ELSE concat(repeat('*', greatest(length(id) - 4, 0)), right(id, 4))
END;

CREATE FUNCTION IF NOT EXISTS wizard.ops.mask_iban(iban STRING)
RETURNS STRING
COMMENT 'Bank accounts. Stewards: full IBAN. Others: country/check digits and last 4 characters.'
RETURN CASE
  WHEN iban IS NULL THEN NULL
  WHEN is_member('wizard_stewards') THEN iban
  ELSE concat(left(iban, 4), ' **** **** ', right(iban, 4))
END;

CREATE FUNCTION IF NOT EXISTS wizard.ops.company_code_access(company_code STRING)
RETURNS BOOLEAN
COMMENT 'Entity-scoped rows: stewards and analysts see every company code; members of wizard_cc_<code> (e.g. wizard_cc_pl01, the Polish procurement team) see only their own.'
RETURN is_member('wizard_stewards') OR is_member('wizard_analysts')
       OR (company_code IS NOT NULL AND is_member(concat('wizard_cc_', lower(company_code))));

-- ------------------------------------------------------------------ row filters
ALTER MATERIALIZED VIEW wizard.gold.fact_invoice SET ROW FILTER wizard.ops.company_code_access ON (company_code);

-- ------------------------------------------------------------------ masks
ALTER MATERIALIZED VIEW wizard.silver.supplier_records ALTER COLUMN national_id SET MASK wizard.ops.mask_national_id;
ALTER MATERIALIZED VIEW wizard.silver.hicx_suppliers ALTER COLUMN tax_id SET MASK wizard.ops.mask_national_id;
ALTER MATERIALIZED VIEW wizard.silver.hicx_bank_details ALTER COLUMN iban SET MASK wizard.ops.mask_iban;
ALTER STREAMING TABLE wizard.silver.sap_lfa1 ALTER COLUMN stcd1 SET MASK wizard.ops.mask_national_id;
ALTER STREAMING TABLE wizard.silver.sap_lfa1 ALTER COLUMN stcd2 SET MASK wizard.ops.mask_national_id;
ALTER STREAMING TABLE wizard.silver.sap_lfa1 ALTER COLUMN stceg SET MASK wizard.ops.mask_national_id;
ALTER STREAMING TABLE wizard.silver.coupa_suppliers ALTER COLUMN tax_id SET MASK wizard.ops.mask_national_id;
ALTER TABLE wizard.gold.dim_supplier ALTER COLUMN national_id SET MASK wizard.ops.mask_national_id;
ALTER TABLE wizard.gold.supplier_source_xref ALTER COLUMN national_id SET MASK wizard.ops.mask_national_id;
ALTER MATERIALIZED VIEW wizard.gold.supplier_360 ALTER COLUMN national_id SET MASK wizard.ops.mask_national_id;

-- ------------------------------------------------------------------ tags
ALTER TABLE wizard.gold.dim_supplier SET TAGS ('layer' = 'gold', 'data_owner' = 'procurement', 'contains_pii' = 'true', 'certified' = 'true');
ALTER TABLE wizard.gold.dim_supplier ALTER COLUMN national_id SET TAGS ('pii' = 'national_id');
ALTER TABLE wizard.gold.dim_supplier_history ALTER COLUMN national_id SET MASK wizard.ops.mask_national_id;
ALTER TABLE wizard.gold.dim_supplier_history SET TAGS ('layer' = 'gold', 'data_owner' = 'procurement', 'contains_pii' = 'true', 'certified' = 'false');
ALTER TABLE wizard.gold.dim_supplier_history ALTER COLUMN national_id SET TAGS ('pii' = 'national_id');
ALTER TABLE wizard.gold.supplier_source_xref ALTER COLUMN national_id SET TAGS ('pii' = 'national_id');
ALTER MATERIALIZED VIEW wizard.gold.supplier_360 SET TAGS ('layer' = 'gold', 'data_owner' = 'procurement', 'contains_pii' = 'true', 'certified' = 'true');
ALTER MATERIALIZED VIEW wizard.gold.supplier_360 ALTER COLUMN national_id SET TAGS ('pii' = 'national_id');
ALTER MATERIALIZED VIEW wizard.silver.hicx_bank_details ALTER COLUMN iban SET TAGS ('pii' = 'bank_account');
ALTER MATERIALIZED VIEW wizard.silver.supplier_records ALTER COLUMN national_id SET TAGS ('pii' = 'national_id');
ALTER STREAMING TABLE wizard.silver.sap_lfa1 ALTER COLUMN stcd1 SET TAGS ('pii' = 'national_id');
ALTER STREAMING TABLE wizard.silver.coupa_suppliers ALTER COLUMN tax_id SET TAGS ('pii' = 'national_id');

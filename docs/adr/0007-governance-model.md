# 0007: Unity Catalog as the only governance layer

**Status:** Accepted · 2026-11-13

## Context

Supplier data includes PII and sensitive financial fields (bank accounts, tax IDs, contact details). People, BI tools and agents all read the same data. If access rules live in application code, they drift apart and can't be audited.

## Decision

All access control, masking, classification and lineage is in Unity Catalog:

- **Groups:** `wizard_stewards` (unmasked; the steward app), `wizard_analysts` (masked, all company codes; the MCP app), `wizard_cc_<code>` (masked, one company code). Each app and integration has its own service principal: `wizard-mcp` and `wizard-steward` (app identities), `wizard-watchdog` (WatchDog calling the MCP servers) and `wizard-ci` (deploys).
- **Column masks** on `tax_id`, `bank_account`, contact email and phone, using SQL UDFs that check `is_member('wizard_stewards')`.
- **Row filters:** company-code scoping on `fact_invoice` (a `wizard_cc_pl01` member sees only PL01).
- **Tags:** `pii`, `source_system`, `data_owner`, `certified`, applied by the pipelines.
- **Lineage:** from system tables, exposed to agents via `trace_metric`. The last hop, from landing file to bronze, is carried per row in `_file` and `_batch_id`.
- **Audit:** the MCP server logs every tool call to `wizard.ops.agent_tool_calls`.

## Alternatives considered

- **Masking in the MCP server.** Easy to bypass through other access paths, and not visible to auditors.
- **Separate masked copies of tables.** Doubles storage and drifts out of sync.

## Consequences

- One policy applies to Genie, dashboards, notebooks and agents alike.
- Free Edition is a single-user workspace, so group-based behaviour is demonstrated with groups and service principals inside it. The write-up explains how this maps to a multi-workspace production setup.

## Implementation (M8)

`pipelines/governance.sql` runs as a job task after `gold` and before `semantic_layer`, so every rebuild re-applies policy.

| Policy | Function | Applied to |
|---|---|---|
| National/tax ID mask | `ops.mask_national_id`: stewards see the value; others see `XXX-XX-XXXX` (EIN/SSN shape) or `****` + last 4 | `silver.supplier_records`, `silver.hicx_suppliers`, `silver.sap_lfa1` (`stcd1`, `stcd2`, `stceg`), `silver.coupa_suppliers`, `gold.dim_supplier`, `gold.supplier_source_xref`, `gold.supplier_360` |
| Bank account mask | `ops.mask_iban`: first 4 + last 4 | `silver.hicx_bank_details.iban` |
| Company-code row filter | `ops.company_code_access`: stewards and analysts see everything; `wizard_cc_<code>` members see their company code | `gold.fact_invoice` |
| Tags | `layer`, `data_owner`, `contains_pii`, `certified` on tables; `pii` on columns | all of the above |

Implementation notes:

- **`is_member`, not `is_account_group_member`.** Free Edition groups are workspace groups. `is_account_group_member` returned false for them.
- **Functions are `CREATE ... IF NOT EXISTS`.** `CREATE OR REPLACE` briefly drops the function, and every table that uses it fails with `UC_DEPENDENCY_DOES_NOT_EXIST` until it is back. The first scheduled run failed this way: `semantic_layer` read `supplier_360` in parallel with the governance task. The fix was to create the functions only if missing and to run the tasks in order. Changing a policy is a deliberate, manual `CREATE OR REPLACE`.
- **DQ findings don't copy masked values.** The `invalid_national_id` finding used to include the raw ID in its detail text, which would have leaked it past the mask. It no longer does.
- **Health check enforces it:** the job fails if fewer than 10 masks exist, or if any `pii`-tagged column has no mask.

Verified:

- The steward sees `36501757`. The MCP agent's service principal, which is not a steward, sees `****1757` through the same tool.
- `scripts/row_filter_check.sh` queries `fact_invoice` as the `wizard-test-pl01` service principal using a short-lived OAuth secret. It returns only `PL01` (2,431 rows), while the owner sees all five company-code groups.
- Rows with no company code (invoices from Coupa-only spend) are hidden from company-scoped users. This is deliberate: deny by default.

### Identities and groups (M8)

| Principal | Groups | Can do |
|---|---|---|
| Data steward (person) | `wizard_stewards` | Unmasked PII, all rows |
| `wizard-steward` app | `wizard_stewards` | Same as a steward; writes decisions |
| `wizard-mcp` app (agents) | `wizard_analysts` | Masked PII, all rows; writes only the audit log and proposals |
| `wizard-watchdog` | none | CAN_USE on the MCP app only. It reaches data through the app, and every call is logged under its ID |
| `wizard-test-pl01` | `wizard_cc_pl01` | Masked PII; PL01 invoices only |
| `wizard-ci` | none | CAN_MANAGE on the bundle's resources; runs nothing |

`scripts/groups_setup.sh` creates the groups, `watchdog_sp.sh` sets up the WatchDog principal, and `grant_ci_sp.sh` sets up the CI principal.

**Free Edition limit:** Unity Catalog can grant privileges only to account groups, and Free Edition has workspace groups only. So group membership drives the masks and row filters, which use `is_member()`, while table grants are given to each service principal. In production these would be account groups synced from the identity provider, and grants would go to the groups.

### Lineage, end to end

`scripts/lineage_e2e_check.py` is the M8 exit test and passes:

- **Table level:** every metric view and Supplier 360 walks back through Unity Catalog lineage to bronze tables of SAP, Coupa and HICX, with no table missing its upstream.
- **Row level:** 25 sampled invoices per source go from gold, to silver (`_batch_id`), to bronze, to the landing file (`_file`), to the shipper manifest that declared that file (reconciliation `OK`). Unity Catalog doesn't record Auto Loader landing files as upstream of bronze, so this last hop is carried in the data itself.

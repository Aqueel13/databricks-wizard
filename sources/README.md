# Source systems (M1)

Local, simulated enterprise systems for **Larkspur Foods**, a fictional food manufacturer with four company codes: US01 (USD), NL01 (EUR), PL01 (PLN) and UA01 (UAH).

| System | What it is | How Databricks gets the data | Where |
|---|---|---|---|
| **SAP S/4 mock** | Real SAP demo tables (`LFA1`, `LFB1`, `EKKO`, `EKPO`, `EKET`, `EKBE`, `RBKP`, `RSEG`, `T001`, `T023T`, `TCURR`) in Postgres | CDC: Debezium → Redpanda topics `erp.sap.<table>` | db `erp`, schema `sap`, port 5433 |
| **Coupa mock** | Indirect spend: suppliers, POs and lines, receipts, invoices, contracts | REST, `updated-at[gt]` watermark, 50 per page | http://localhost:8081/docs (header `X-COUPA-API-KEY: wizard-dev-key`) |
| **HICX exports** | Supplier master, bank details, certifications | Weekly file drop (`.xlsx` + `.csv`) | `data/exports/hicx/<date>/` |
| **Document repository** | Certificate PDFs (20% scanned) and contract PDFs | File drop | `data/exports/docs/` |

## Run it

```bash
make data       # once: download external datasets (~7 GB)
make sources    # Postgres + Redpanda + console, seed everything, Debezium CDC, Coupa API
make simulate   # live inserts/updates/deletes in SAP and Coupa
make status
```

Redpanda Console (topics, messages, connector): http://localhost:8080

To reseed from scratch, run `make reset && make sources`. Seeding drops and recreates the SAP schema, so don't run it while the CDC connector is attached.

## What the generator does

`wizard-sources seed` runs five steps. They're deterministic for a given `WIZARD_SEED` (default 42).

1. **universe**: builds about 3,200 real-world suppliers from SAP demo vendors (with natural duplicates), World Bank awardees, Polish public-tender contractors (companies only, real NIP), Ukrainian ProZorro suppliers (companies only, real EDRPOU, Cyrillic names) and fictional suppliers. Each one is placed into SAP, Coupa and HICX under system-specific variants: SAP's 35-character name split, uppercase, Cyrillic, transliteration (official and legacy), translated legal forms, abbreviations and typos.
2. **sap**: loads the real SAP document flows. POs are re-pointed to the universe (Pareto-weighted), line values are log-compressed, NL01 POs are cloned into PL01 and UA01, dates are shifted to end 2026-10-31, and delivery drift is injected.
3. **coupa**: 22k indirect POs over 3 years. Receipt lateness is sampled from the real SAP lateness distribution. Invoice defects are injected.
4. **hicx**: export files with title rows above the header, as a real UI export has, plus certifications (some expired).
5. **docs**: certificate and contract PDFs. A few disagree with system metadata on purpose.

## Answer key (`data/stage/`)

| File | Contents |
|---|---|
| `gt_supplier.parquet` | One row per real-world supplier: origin, category, real or fictional identity, storyline (`delivery_drift`, `sanctions_near_match:<SDN name>`) |
| `gt_source_record.parquet` | Every SAP, Coupa and HICX supplier record → `true_supplier_id`, with the variant methods applied |
| `gt_defect.parquet` | Every injected defect (15 types, about 1,200 rows) |
| `gt_document.parquet` | True field values of every generated document, the answer key for extraction |

Real names supply identity only. All behaviour is synthetic, and sanctions near-matches are only on fictional suppliers (ADR 0003).

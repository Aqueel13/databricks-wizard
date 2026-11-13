# 0003: Real public and open datasets, linked and damaged on purpose

**Status:** Accepted · 2026-11-13 (revised the same day after the Kaggle datasets were verified)

## Context

Fully synthetic supplier data looks fake: uniform names, no real addresses, no real corporate structures. Real enterprise ERP data isn't available. Data-quality and entity-resolution work can only be evaluated if the true answer is known.

## Decision

### Data sources

- **Real ERP structure:** SAP tables from Google's SAP demo replication (`LFA1`, `EKKO`, `EKPO`, `EKET`, `EKBE`, `RBKP`, `RSEG`, `TCURR`, …) form the SAP mock. POs are spread across the full supplier universe and dates shifted to the last 3 years.
- **Real identity and reference data:** USAspending.gov, GLEIF, OFAC SDN, SEC EDGAR, World Bank corporate awards.
- **Real regional suppliers:** Polish public tenders (NIP, diacritics) and ProZorro (Cyrillic, EDRPOU), excluding private individuals and sole traders.
- **Real documents:** CUAD contracts with expert labels, Northwind invoice/PO PDFs.
- **External matching benchmark:** a sample of the Amazon ML Challenge 2026 entity-resolution data (internal use only).
- **Calibration only:** Olist delivery delays shape the generator's lateness distributions.

Full list, licences and profiling results: [data-model.md](../data-model.md#external-datasets).

**As built (M1–M8):** USAspending, GLEIF and SEC EDGAR were not used. World Bank awards are the international source, and the generator's answer key is the ground truth for cross-script pairs. The supplier documents are generated (contracts, ISO certificates including scans, W-9-style forms) so that every field is in the answer key; Northwind PDFs were not used. CUAD is kept for a separate clause-extraction benchmark. OFAC addresses and ECB exchange rates were added. The [README](../../README.md#data-sources-and-attribution) lists what is actually used.

### What the generator does

It doesn't invent most records. It **links and damages** real ones:

- places the same real supplier into several systems under realistic name variants (this is what creates the cross-system identity problem),
- injects defects (duplicates, invalid IDs, conflicting addresses, delivery drift, expired certificates, near-sanctions aliases, out-of-order CDC, schema drift),
- records every link and defect in a ground-truth table.

### Indirect spend categories

IT services, MRO (maintenance, repair and operations), logistics, professional services, facilities.

### Real company names (policy)

- Real data supplies **identity only**: names, addresses, LEIs, national IDs, parent companies, published filings.
- All **behaviour** (delivery performance, risk scores, documents tied to a supplier, defects) is synthetic and labelled as such in the README, the dashboards and the Genie space instructions.
- **Sanctions near-matches are attached only to fictional suppliers.** No real company is shown as a sanctions hit.
- Demo storylines about "bad" suppliers use fictional suppliers.

### SEC EDGAR contact

SEC requires a User-Agent with contact details. The contact string is read from the `SEC_USER_AGENT` environment variable, which is set locally and never committed. If it's unset, the SEC step is skipped.

## Alternatives considered

- **Fully synthetic data (Faker).** Easy to make, but obviously fake and with no link to real entities.
- **Kaggle procurement datasets on their own.** The small ones (5–40 suppliers) are too thin, and none has a cross-system identity problem. That's why Kaggle data is used as raw material that the generator links and damages, not as the finished dataset.

## Consequences

- Entity resolution and DQ can be measured (precision, recall, F1, defect detection rate), including against an external benchmark.
- The demo shows real SAP structures, real company identities and real contracts.
- Attribution for every dataset goes in the README. Olist (CC BY-NC-SA) is used for calibration only, so no Olist rows are republished.
- Raw data is never committed. `make data` rebuilds it.
- Large sources (ProZorro, the ER benchmark) are sampled before they're sent to Databricks, to stay inside Free Edition limits.

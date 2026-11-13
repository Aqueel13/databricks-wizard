# 0003: Real public data plus synthetic ERP data with recorded defects

**Status:** Accepted · 2026-11-13

## Context

Fully synthetic supplier data looks fake: uniform names, no real addresses, no real corporate structures. Real enterprise ERP data isn't available. Data-quality and entity-resolution work can only be evaluated if the true answer is known.

## Decision

- Use **real public reference data**: USAspending.gov (recipients and awards), GLEIF (LEI and parent relationships), OFAC SDN (sanctions), SEC EDGAR (financial facts).
- Seed part of the synthetic supplier base from real USAspending recipients, so names, addresses and industries look real.
- Generate ERP, P2P and SIM records synthetically, with **defects injected on purpose** (duplicates, name variants, invalid tax IDs, conflicting addresses, delivery drift, expired certificates, near-sanctions aliases, out-of-order CDC, schema drift).
- Record every injected defect in a ground-truth table.

## Alternatives considered

- **Kaggle procurement datasets.** Small, single-source, and with no cross-system identity problem.
- **Fully synthetic data (Faker).** Easy to make, but obviously fake and with no link to real entities.

## Consequences

- Entity resolution and DQ can be measured (precision, recall, F1, defect detection rate).
- The demo can show real public companies with real LEIs and filings.
- Public datasets carry licences: USAspending, OFAC and SEC data are US public domain, and GLEIF data is CC0. Attribution goes in the README.
- Snapshot sizes are kept small (filtered by NAICS and state) to stay inside Free Edition storage and compute.

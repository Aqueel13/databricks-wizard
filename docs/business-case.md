# Business case: a governed Supplier 360

<!-- NUMBERS: values marked † come from the v1 build and are refreshed after the v2 rebuild -->

This page is written for a procurement or finance leader deciding whether to fund a supplier data foundation like Databricks Wizard. It separates three kinds of number:

- **measured in this project** (against a known answer key);
- **properties of the demo data** (which the generator planted, so they say nothing about a real company);
- **assumptions** that a real company replaces with its own figures.

## The problem in one line

When the same supplier exists as three records in three systems, every supplier number the company reports is wrong. Spend is split, risk is missed and performance can't be compared. The people and agents asking questions have no way to tell.

## What changes

| Today (typical) | With a resolved, governed Supplier 360 |
|---|---|
| Spend per supplier is a manual monthly reconciliation across ERP, P2P and spreadsheets | One `supplier_id` across every fact, refreshed minutes after a source changes |
| Delivery problems are noticed when a plant runs short | A market-adjusted deterioration rule flags suppliers that are getting worse relative to their peers |
| Certificate expiry lives in a supplier portal field that drifts from the actual PDF | The document is parsed. Its expiry date outranks the system field and feeds the risk score |
| Sanctions screening produces long alert lists that are cleared by hand | Name and address evidence auto-clears alerts with a country conflict. Every remaining alert goes to a person, with evidence, and is never shown as a finding |
| AI assistants either can't see supplier data or see it ungoverned | Agents use the same governed tables through MCP. PII is masked and every call is audited. Agents propose steward decisions and people approve them |

## Value drivers

Each driver comes with the evidence this project produced and the formula a real company would fill in. All figures are annual.

**1. Spend visibility → negotiation leverage.**
- Evidence: every supplier source record across SAP, Coupa and HICX is resolved to a golden record, at pairwise F1 0.968 † against the answer key. It works across scripts: Cyrillic–Latin F1 is 0.972 †.
- On an external benchmark outside the supplier domain, with no tuning, the matcher reaches F1 0.829.
- Formula: `addressable spend × share newly consolidated × negotiated saving`.
- Assumption to replace: the saving rate the category team believes is achievable once volumes are visible per supplier.

**2. Fewer payment errors.**
- Evidence: invoice facts sit on the resolved supplier, so duplicate invoices across systems and supplier records become joinable.
- The demo data contains planted duplicate invoices and amount mismatches. Their counts are a property of the generator, not a benchmark.
- Formula: `invoices per year × duplicate/overpayment rate × average invoice value × recovery rate`.
- Assumption to replace: the duplicate-payment rate from your last AP recovery audit.

**3. Steward and analyst time.**
- Evidence: the matcher auto-decides most pairs. A Qwen3-Next adjudicator settles the uncertain middle band with 0 false non-matches on the benchmark. Only refused or unsure links reach the steward queue.
- Genie answers the four core supplier questions correctly, scored against gold: 4/4 †.
- Formula: `hours per month on manual reconciliation and ad-hoc supplier queries × loaded hourly cost × share removed`.

**4. Compliance and supply risk.**
- Evidence: expired certificates are caught from the document itself. All 28 † planted document/system mismatches were found, with no false positives.
- Sanctions near-matches are always alerted (6 of 6), and alerts with a conflicting country are auto-cleared.
- Supplier deterioration is flagged on a market-adjusted basis. It also found 12 real deteriorations in the untouched SAP history.
- Formula: `expected incidents avoided × cost per incident`. This value is real but hard to put a number on, so treat it as risk reduction rather than savings.

**5. AI readiness (an option, not a saving).**
- Every future agent reuses the same governed MCP tools instead of building its own data access.
- Intelligence WatchDog already does this: it answers cross-system questions using its own process data plus Wizard's supplier data.

## Cost

| Component | This project | Production (drivers, not quotes) |
|---|---|---|
| Platform | Databricks Free Edition: ₹0 | Serverless DBUs for pipelines, jobs, SQL warehouse, model serving and Vector Search, sized by data volume and refresh frequency |
| Source connectivity | Local Docker shipper | Lakeflow Connect or CDC into cloud storage |
| Identity and access | Workspace groups, service principals | Account groups synced from the identity provider |
| People | One engineer | Platform engineer, data steward(s), a category owner as sponsor |
| Sanctions data | OFAC public list | Licensed screening provider |

## How to decide

1. Pick one category with fragmented spend and run the matcher on its supplier records. This takes days, not months, because the matcher trains without labels.
2. Measure the share of spend that moves when records are resolved. That turns driver 1 from an assumption into a number.
3. If driver 1 alone covers the run cost, fund the foundation and add drivers 2–4 as they are measured.

## Risks

- **Bad links are worse than no links.** The design prefers precision: cluster constraints, an LLM ambiguity guard, and a steward review of refused links. Precision is tracked on every run in `ops.er_eval`.
- **Adoption.** The value needs people to use one supplier ID. Genie, the dashboard and agents all read the same metric views, which removes the "my number vs your number" argument.
- **Over-trusting AI.** Agents can't approve decisions. Answers carry provenance, and sanctions alerts are never presented as findings.

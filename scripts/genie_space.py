"""Create or update the "Supplier 360" Genie space (idempotent, matched by title).

  uv run --project shipper python scripts/genie_space.py
"""
from __future__ import annotations

import json
import os
import uuid

from databricks.sdk import WorkspaceClient

TITLE = "Supplier 360 (Databricks Wizard)"
CAT = os.environ.get("WIZARD_CATALOG", "wizard")
TABLES = [f"{CAT}.gold.{t}" for t in (
    "supplier_360", "metrics_spend", "metrics_delivery", "metrics_supplier", "spend_concentration",
    "supplier_delivery_trend", "supplier_risk_score", "supplier_dq_scorecard", "dim_category")]
INSTRUCTIONS = """You answer procurement questions for Larkspur Foods, a fictional food manufacturer (company codes US01, NL01, PL01, UA01).
- One row in supplier_360 is one real-world supplier, resolved across SAP, Coupa and HICX (supplier_id). Always use supplier_id, never source-system vendor ids.
- Money is in USD (amount_usd / spend_usd / "Spend USD"), converted at static FX rates.
- Prefer the metric views (metrics_spend, metrics_delivery, metrics_supplier) for totals and rates; query them with MEASURE(`<measure name>`) and GROUP BY ALL.
- "Deteriorating delivery performance" means supplier_delivery_trend.deteriorating = true: the last 6 months vs the supplier's own prior 12 months, market-adjusted, on-time rate down >= 10 points AND lateness up >= 5 days with z >= 1.64. Show on_time_baseline, on_time_recent, excess_days_late_increase and z_score. Suppliers with too few deliveries are not assessed.
- Risk: supplier_risk_score / supplier_360.risk_score (0-100) = 30% delivery + 30% compliance + 20% concentration + 20% data quality; bands high >= 30, medium >= 15. Compliance risk comes from expired certificates, sanctions screening and purchasing blocks. Financial risk is NOT assessed (no financial data loaded): say so when asked about financial risk.
- A sanctions screening ALERT is a name similarity pending review, never a finding. Never state that a supplier is sanctioned unless sanctions_confirmed > 0.
- Spend concentration: spend_concentration (last 12 months, HHI 0-10,000; >= 2,500 highly concentrated, 1,500-2,500 moderately concentrated).
- Master data quality: suppliers with incomplete, invalid or inconsistent master data are exactly supplier_dq_scorecard.has_issues = true (size(issues) > 0). Show legal_name, dq_score and the issues array; issue names: missing_national_id, invalid_national_id, missing_city, duplicate_records, conflicting_address, inconsistent_city_format, conflicting_national_ids, expired_certificate, invoice findings.
- When listing suppliers, include legal_name, country and the measure that answers the question, ordered by severity."""
SAMPLE_QUESTIONS = [
    "Which suppliers have deteriorating delivery performance?",
    "Which suppliers have elevated compliance risk?",
    "What is our spend concentration by category?",
    "Which suppliers have incomplete or inconsistent master data?",
]


def _hex() -> str:
    return uuid.uuid4().hex


def serialized() -> str:
    return json.dumps({
        "version": 1,
        "config": {"sample_questions": [{"id": _hex(), "question": [q]} for q in SAMPLE_QUESTIONS]},
        "data_sources": {"tables": [{"identifier": t} for t in sorted(TABLES)]},
        "instructions": {"text_instructions": [{"id": _hex(), "content": [INSTRUCTIONS]}]},
    })


def main() -> None:
    w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
    wh = os.environ.get("WIZARD_WAREHOUSE_ID") or next(iter(w.warehouses.list())).id
    me = w.current_user.me().user_name
    spaces = w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces", [])
    existing = next((s for s in spaces if s.get("title") == TITLE), None)
    body = {"title": TITLE, "warehouse_id": wh, "serialized_space": serialized(),
            "description": "Ask about Larkspur Foods suppliers: delivery deterioration, risk, spend concentration, "
                           "master data quality. Built on governed gold tables and metric views."}
    if existing:
        sid = existing["space_id"]
        w.api_client.do("PATCH", f"/api/2.0/genie/spaces/{sid}", body=body)
        print("updated", sid)
    else:
        r = w.api_client.do("POST", "/api/2.0/genie/spaces", body={**body, "parent_path": f"/Workspace/Users/{me}"})
        sid = r["space_id"]
        print("created", sid)
    host = w.config.host.rstrip("/")
    print(f"{host}/genie/rooms/{sid}")


if __name__ == "__main__":
    main()

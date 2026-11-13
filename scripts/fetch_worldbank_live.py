"""Fetch the live World Bank Corporate Procurement Contract Awards (Finances One dataset DS00028, CC BY 4.0) and
save a dated snapshot in the Kaggle file's column layout, so the generator can use it instead of the 2021 snapshot.

The live data changes quarterly, so it is opt-in: builds stay reproducible on the pinned snapshot unless
WIZARD_WORLDBANK_SNAPSHOT points at a live file.

  python scripts/fetch_worldbank_live.py              # -> data/raw/worldbank_awards/live-<date>.csv (needs curl)
  WIZARD_WORLDBANK_SNAPSHOT=data/raw/worldbank_awards/live-2026-11-14.csv make seed
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

API = ("https://datacatalogapi.worldbank.org/dexapps/fone/api/apiservice"
       "?datasetId=DS00028&resourceId=RS00025&type=json&top={top}&skip={skip}")
COLUMNS = [  # Kaggle column -> API field
    ("Award Date", "award_date"), ("Quarter and Fiscal Year", "quarter_and_fiscal_year"),
    ("Commodity Category", "commodity_category"), ("Contract Description", "contract_description"),
    ("WBG Organization", "wbg_organization"), ("Selection Number", "selection_number"), ("Supplier", "supplier"),
    ("Supplier Country", "supplier_country"), ("Supplier Country Code", "supplier_country_code"),
    ("Contract Award Amount", "contract_award_amount"), ("Fund Source", "fund_source"),
    ("VPU description", "vpu_description")]


def fetch(page: int = 1000) -> list[dict]:
    rows, skip = [], 0
    while True:
        # downloaded with curl, like every other dataset in scripts/fetch_data.sh
        d = json.loads(subprocess.run(["curl", "-fsSL", "-m", "120", API.format(top=page, skip=skip)],
                                      check=True, capture_output=True).stdout)
        rows += d["data"]
        skip += page
        if not d["data"] or skip >= int(d["count"]):
            return rows


def main() -> None:
    rows = fetch()
    out = Path(__file__).resolve().parents[1] / "data/raw/worldbank_awards" / f"live-{date.today()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([k for k, _ in COLUMNS])
        for r in rows:
            d = r.get("award_date")
            try:
                d = datetime.strptime(d, "%d-%b-%Y").strftime("%Y-%m-%dT00:00:00.000") if d else None
            except ValueError:
                pass
            w.writerow([d if api == "award_date" else r.get(api) for _, api in COLUMNS])
    print(f"{len(rows)} awards -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

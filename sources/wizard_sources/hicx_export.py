"""HICX supplier-information exports (weekly file drops).

Writes data/exports/hicx/<export_date>/:
  suppliers.xlsx       title rows above the header, like a real UI export
  bank_details.csv
  certifications.csv
Certifications are also written to data/stage/hicx_certifications.parquet for the document generator.
"""
from __future__ import annotations

import csv
import random
from datetime import timedelta

import duckdb
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from . import config as C
from .universe import write_parquet

CERTS_FOOD = [("ISO 22000", "Food safety management"), ("FSSC 22000", "Food safety system certification"),
              ("BRCGS Food Safety", "Global standard for food safety"), ("ISO 9001", "Quality management")]
CERTS_OTHER = [("ISO 9001", "Quality management"), ("ISO 14001", "Environmental management"),
               ("ISO 27001", "Information security management"), ("ISO 45001", "Occupational health and safety")]
ISSUERS = ["Bureau Veritas Certification", "SGS United Kingdom Ltd", "TÜV SÜD Management Service GmbH",
           "DNV Business Assurance", "Lloyd's Register Quality Assurance", "Intertek Certification Ltd"]


def export() -> None:
    rng = random.Random(C.SEED + 2)
    db = duckdb.connect()
    rel = db.sql(f"select * from '{C.STAGE / 'sys_hicx_supplier.parquet'}' order by hicx_id")
    rows = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    gt = C.STAGE / "gt_defect.parquet"
    rel = db.sql(f"select * from '{gt}' where defect_type not in ('expired_certificate')")
    defects = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    next_d = len(defects) + 1

    export_date = C.HISTORY_END
    out = C.EXPORTS / "hicx" / export_date.isoformat()
    out.mkdir(parents=True, exist_ok=True)

    # suppliers.xlsx
    wb = Workbook()
    ws = wb.active
    ws.title = "Suppliers"
    ws.append(["HICX Supplier Information Management — Supplier Export"])
    ws.append([f"Generated {export_date.isoformat()} 06:00 UTC · Larkspur Foods · {len(rows)} records"])
    header = ["HICX ID", "Legal Name", "Legal Name (Local)", "Tax ID Type", "Tax ID", "Country", "City",
              "Primary Category", "Status", "Onboarding Date"]
    ws.append(header)
    for c in ws[3]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E79")
    for r in rows:
        ws.append([r["hicx_id"], r["legal_name"], r["legal_name_local"], r["national_id_type"], r["national_id"],
                   r["country"], r["city"], (r["category"] or "").replace("_", " ").title() or None, r["status"],
                   r["onboarding_date"]])
    wb.save(out / "suppliers.xlsx")

    with open(out / "bank_details.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["hicx_id", "iban", "bank_country", "verified_on"])
        for r in rows:
            if r["iban"]:
                w.writerow([r["hicx_id"], r["iban"], r["country"],
                            (r["onboarding_date"] + timedelta(days=rng.randint(1, 30))).isoformat()])

    certs = []
    for r in rows:
        food = r["category"] == "DIRECT_INGREDIENTS"
        n = rng.choice([1, 1, 2]) if food else rng.choice([0, 0, 1])
        for ctype, scope in rng.sample(CERTS_FOOD if food else CERTS_OTHER, n):
            issued = export_date - timedelta(days=rng.randint(30, 1400))
            expires = issued + timedelta(days=3 * 365)
            num = f"{ctype.split()[0][:4].upper()}-{rng.randint(100000, 999999)}"
            certs.append(dict(hicx_id=r["hicx_id"], legal_name=r["legal_name"], legal_name_local=r["legal_name_local"],
                              country=r["country"], city=r["city"], cert_type=ctype, scope=scope, cert_number=num,
                              issuer=rng.choice(ISSUERS), issue_date=issued, expiry_date=expires,
                              supplier_status=r["status"]))
            if expires < export_date and r["status"] == "approved":
                defects.append(dict(defect_id=f"D{next_d:06d}", defect_type="expired_certificate",
                                    source_system="hicx", entity="certification", entity_key=num, field="expiry_date",
                                    original_value=None, injected_value=expires.isoformat(),
                                    note=f"{ctype} expired while supplier {r['hicx_id']} is approved"))
                next_d += 1
    with open(out / "certifications.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["hicx_id", "certificate_type", "certificate_number", "issuer", "issue_date", "expiry_date"])
        for c in certs:
            w.writerow([c["hicx_id"], c["cert_type"], c["cert_number"], c["issuer"], c["issue_date"], c["expiry_date"]])

    write_parquet(db, certs, C.STAGE / "hicx_certifications.parquet")
    write_parquet(db, defects, gt)
    expired = sum(1 for c in certs if c["expiry_date"] < export_date)
    print(f"hicx: {len(rows)} suppliers, {len(certs)} certifications ({expired} expired) -> {out}")

"""Generate supplier documents (the document repository source).

  certificates   one PDF per HICX certification; ~20% rendered as noisy "scans" (image-only PDF)
  contracts      one PDF per Coupa contract (supply / services agreement from a template)

Some documents deliberately disagree with the system metadata (expiry date, contract value); those
are recorded in gt_defect. data/stage/gt_document.parquet holds the true field values of every document,
which is the answer key for document extraction.
"""
from __future__ import annotations

import random
import shutil
from datetime import timedelta
from pathlib import Path

import duckdb
import psycopg
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import config as C
from .universe import write_parquet

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/Library/Fonts/Arial Unicode.ttf"]
FONT_FILE = next((p for p in FONT_PATHS if Path(p).exists()), None)
GOVERNING_LAW = {"US": "the State of Illinois", "PL": "Poland", "UA": "Ukraine", "NL": "the Netherlands",
                 "DE": "Germany"}


def _font() -> str:
    if FONT_FILE and "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", FONT_FILE))
    return "DejaVu" if FONT_FILE else "Helvetica"


def _lines_pdf(path: Path, title: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    f = _font()
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFont(f, 16)
    c.drawString(60, h - 80, title)
    c.setFont(f, 10)
    y = h - 120
    for ln in lines:
        if y < 70:
            c.showPage()
            c.setFont(f, 10)
            y = h - 70
        c.drawString(60, y, ln)
        y -= 16
    c.save()


def _scan_pdf(path: Path, title: str, lines: list[str], rng: random.Random) -> None:
    """Image-only PDF that looks like a skewed, noisy scan (needs OCR / a vision model)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("L", (1240, 1754), 248)
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT_FILE, 34) if FONT_FILE else ImageFont.load_default()
    small = ImageFont.truetype(FONT_FILE, 22) if FONT_FILE else ImageFont.load_default()
    d.text((110, 140), title, fill=20, font=big)
    y = 240
    for ln in lines:
        d.text((110, y), ln, fill=rng.randint(20, 60), font=small)
        y += 36
    for _ in range(4000):
        d.point((rng.randint(0, 1239), rng.randint(0, 1753)), fill=rng.randint(120, 200))
    img = img.rotate(rng.uniform(-1.8, 1.8), fillcolor=235, expand=False).filter(ImageFilter.GaussianBlur(0.7))
    img.convert("RGB").save(path, "PDF", resolution=150)


def generate() -> None:
    rng = random.Random(C.SEED + 3)
    db = duckdb.connect()
    root = C.EXPORTS / "docs"
    if root.exists():  # regenerate from scratch so stale documents never linger
        shutil.rmtree(root)
    gt = C.STAGE / "gt_defect.parquet"
    rel = db.sql(f"select * from '{gt}' where defect_type not in ('certificate_metadata_mismatch', 'contract_metadata_mismatch') order by defect_id")
    defects = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    nd = len(defects) + 1
    docs = []

    rel = db.sql(f"select * from '{C.STAGE / 'hicx_certifications.parquet'}'")
    for c in (dict(zip(rel.columns, r)) for r in rel.fetchall()):
        expiry = c["expiry_date"]
        if rng.random() < 0.03:  # document says something different from the HICX record
            doc_expiry = expiry + timedelta(days=rng.choice([-365, -180, 180, 365]))
            defects.append(dict(defect_id=f"D{nd:06d}", defect_type="certificate_metadata_mismatch",
                                source_system="docs", entity="certification", entity_key=c["cert_number"],
                                field="expiry_date", original_value=str(expiry), injected_value=str(doc_expiry),
                                note="PDF expiry differs from HICX record"))
            nd += 1
            expiry = doc_expiry
        holder = c["legal_name_local"] or c["legal_name"]
        lines = ["This is to certify that the management system of", "", f"    {holder}",
                 f"    {c['city'] or ''}, {c['country']}", "", f"has been assessed and found to conform to {c['cert_type']}",
                 f"Scope: {c['scope']}", "", f"Certificate number: {c['cert_number']}",
                 f"Original issue date: {c['issue_date']}", f"Expiry date: {expiry}", "",
                 f"Issued by: {c['issuer']}", "Validity can be checked with the certification body."]
        path = root / c["hicx_id"] / "certificate" / f"{c['cert_number']}.pdf"
        scanned = rng.random() < 0.2
        (_scan_pdf(path, f"CERTIFICATE · {c['cert_type']}", lines, rng) if scanned
         else _lines_pdf(path, f"CERTIFICATE · {c['cert_type']}", lines))
        docs.append(dict(path=str(path.relative_to(C.EXPORTS)), doc_type="certificate", owner_system="hicx",
                         owner_id=c["hicx_id"], doc_key=c["cert_number"], scanned=scanned, holder=holder,
                         cert_type=c["cert_type"], issuer=c["issuer"], issue_date=c["issue_date"],
                         expiry_date=expiry, contract_value=None, currency=None, payment_terms_days=None,
                         notice_days=None, liability_cap=None, governing_law=None))

    with psycopg.connect(C.pg_dsn("coupa")) as pg:
        contracts = pg.execute("""select k.number, k.name, k.start_date, k.end_date, k.value, k.currency,
                                         s.number, s.name, s.country
                                  from contracts k join suppliers s on s.id = k.supplier_id
                                  order by k.number""").fetchall()
    for num, cname, start, end, value, cur, snum, sname, country in contracts:
        value = float(value)
        doc_value = value
        if rng.random() < 0.03:
            doc_value = round(value * rng.choice([0.8, 1.25, 1.5]), 2)
            defects.append(dict(defect_id=f"D{nd:06d}", defect_type="contract_metadata_mismatch",
                                source_system="docs", entity="contract", entity_key=num, field="value",
                                original_value=str(value), injected_value=str(doc_value),
                                note="contract PDF value differs from Coupa"))
            nd += 1
        terms = rng.choice([30, 45, 60])
        notice = rng.choice([30, 60, 90])
        cap = rng.choice(["the fees paid in the preceding 12 months", "two times the annual contract value",
                          f"{cur} {round(doc_value * 0.5):,}"])
        law = GOVERNING_LAW.get(country, "England and Wales")
        entity = {"US": "Larkspur Foods Inc.", "PL": "Larkspur Foods Polska Sp. z o.o.",
                  "UA": "LLC Larkspur Foods Ukraine"}.get(country, "Larkspur Foods B.V.")
        lines = [f"Agreement no. {num}", "", f"This {cname} is entered into on {start} between",
                 f"{entity} (\"Customer\") and {sname} (\"Supplier\").", "",
                 "1. TERM", f"   The Agreement starts on {start} and ends on {end} unless terminated earlier.",
                 "2. FEES", f"   Total contract value shall not exceed {cur} {doc_value:,.2f}.",
                 f"   Invoices are payable within {terms} days of receipt.",
                 "3. TERMINATION", f"   Either party may terminate for convenience with {notice} days' written notice.",
                 "4. LIABILITY", f"   Each party's aggregate liability is capped at {cap}.",
                 "5. GOVERNING LAW", f"   This Agreement is governed by the laws of {law}.", "",
                 "Signed for Customer: ______________________   Signed for Supplier: ______________________"]
        path = root / "contracts" / snum / f"{num}.pdf"
        _lines_pdf(path, cname.upper(), lines)
        docs.append(dict(path=str(path.relative_to(C.EXPORTS)), doc_type="contract", owner_system="coupa",
                         owner_id=snum, doc_key=num, scanned=False, holder=sname, cert_type=None, issuer=None,
                         issue_date=start, expiry_date=end, contract_value=doc_value, currency=cur,
                         payment_terms_days=terms, notice_days=notice, liability_cap=cap, governing_law=law))

    write_parquet(db, docs, C.STAGE / "gt_document.parquet")
    write_parquet(db, defects, gt)
    print(f"docs: {sum(d['doc_type'] == 'certificate' for d in docs)} certificates "
          f"({sum(d['scanned'] for d in docs)} scanned), {sum(d['doc_type'] == 'contract' for d in docs)} contracts "
          f"-> {root}")

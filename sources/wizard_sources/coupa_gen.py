"""Generate indirect spend for the Coupa mock (Postgres database `coupa`).

Suppliers come from the universe. POs, receipts, invoices and contracts are synthetic, with:
  * receipt lateness sampled from the *real* SAP goods-receipt lateness distribution
  * delivery drift for storyline suppliers
  * injected invoice defects (amount mismatch, duplicate invoice) recorded in gt_defect
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import duckdb
import psycopg

from . import config as C
from .universe import write_parquet

DDL = """
drop table if exists receipts, order_lines, invoices, purchase_orders, contracts, suppliers cascade;
create table suppliers (
  id integer primary key, number varchar(20), name varchar(200), display_name varchar(200), tax_id varchar(20),
  country varchar(3), city varchar(80), commodity varchar(40), status varchar(20), diversity_class varchar(40),
  created_at timestamp, updated_at timestamp);
create table purchase_orders (
  id integer primary key, po_number varchar(20) unique, supplier_id integer references suppliers(id),
  company_code varchar(4), currency varchar(3), total numeric(14,2), status varchar(20), order_date date,
  need_by_date date, created_at timestamp, updated_at timestamp);
create table order_lines (
  id integer primary key, po_id integer references purchase_orders(id), line_num integer, description varchar(200),
  commodity varchar(40), quantity numeric(12,2), price numeric(14,2), total numeric(14,2),
  created_at timestamp, updated_at timestamp);
create table receipts (
  id integer primary key, order_line_id integer references order_lines(id), received_date date,
  quantity numeric(12,2), created_at timestamp, updated_at timestamp);
create table invoices (
  id integer primary key, invoice_number varchar(30), supplier_id integer references suppliers(id),
  po_id integer references purchase_orders(id), invoice_date date, currency varchar(3), total numeric(14,2),
  payment_terms varchar(20), status varchar(20), created_at timestamp, updated_at timestamp);
create table contracts (
  id integer primary key, number varchar(20), supplier_id integer references suppliers(id), name varchar(200),
  start_date date, end_date date, value numeric(14,2), currency varchar(3), status varchar(20),
  created_at timestamp, updated_at timestamp);
create index on purchase_orders (updated_at); create index on order_lines (updated_at);
create index on receipts (updated_at); create index on invoices (updated_at);
create index on contracts (updated_at); create index on suppliers (updated_at);
"""

CURRENCY = {"US": "USD", "PL": "PLN", "UA": "UAH"}
ENTITY = {"US": "US01", "PL": "PL01", "UA": "UA01"}
# (median line value in EUR, lead days, items)
CATEGORY = {
    "IT_SERVICES": (6000, 14, ["Laptop refresh", "Cloud subscription", "Network switch", "Helpdesk support (monthly)",
                               "Software licence renewal", "Security audit", "Data migration services"]),
    "MRO": (1200, 7, ["Bearings and seals", "Conveyor belt", "Pump repair", "Lubricants", "Safety gloves",
                      "Spare motor", "Calibration service"]),
    "LOGISTICS": (4000, 5, ["Pallet freight EU", "Cold-chain trucking", "Warehouse handling", "Customs brokerage",
                            "Container transport"]),
    "PROFESSIONAL_SERVICES": (15000, 21, ["Audit fees", "Legal advisory", "Management consulting", "Recruitment fee",
                                          "Training programme", "Tax advisory"]),
    "FACILITIES": (2500, 10, ["Office cleaning (monthly)", "Waste collection", "HVAC maintenance", "Security services",
                              "Canteen services", "Office furniture"]),
    "DIRECT_INGREDIENTS": (8000, 12, ["Packaging film", "Glass jars", "Labels", "Corrugated boxes"]),
}
N_POS = 22000
START = date(2023, 11, 1)


def _lateness_sample(db: duckdb.DuckDBPyConnection) -> list[int]:
    rows = db.sql("""
        with gr as (select ebeln, ebelp, min(budat) gr from pg.sap.ekbe where vgabe = '1' group by 1, 2),
             due as (select ebeln, ebelp, min(eindt) due from pg.sap.eket group by 1, 2)
        select gr - due from gr join due using (ebeln, ebelp)
        where gr - due between -30 and 60 and ebeln not like '46%'   -- history only, not simulator output
        order by ebeln, ebelp""").fetchall()
    return [r[0] for r in rows]


def load() -> None:
    rng = random.Random(C.SEED + 1)
    db = duckdb.connect()
    db.execute("install postgres; load postgres;")
    db.execute(f"attach '{C.pg_dsn('erp')}' as pg (type postgres, read_only)")
    lateness = _lateness_sample(db)

    rel = db.sql(f"""
        select c.*, g.true_supplier_id, g.storyline, g.origin
        from '{C.STAGE / 'sys_coupa_supplier.parquet'}' c
        join '{C.STAGE / 'gt_source_record.parquet'}' r on r.source_system = 'coupa' and r.source_id = cast(c.id as varchar)
        join '{C.STAGE / 'gt_supplier.parquet'}' g using (true_supplier_id)""")
    sup = [dict(zip(rel.columns, r)) for r in rel.fetchall()]

    suppliers, pos, lines, receipts, invoices, contracts, defects = [], [], [], [], [], [], []
    for s in sup:
        upd = s["updated_at"]
        suppliers.append(dict(id=s["id"], number=s["number"], name=s["name"], display_name=s["display_name"],
                              tax_id=s["tax_id"], country=s["country"], city=s["city"], commodity=s["category"],
                              status=s["status"],
                              # schema drift: field introduced in 2025; only records touched after that carry it
                              diversity_class=rng.choice(["small_business", "none", "women_owned", "none"])
                              if upd >= datetime(2025, 6, 1) else None,
                              created_at=s["created_at"], updated_at=upd))

    # Pareto spend: weight by rank; SAP-demo direct vendors only hold contracts in Coupa
    buyers = [s for s in sup if s["origin"] != "sap_demo"]
    rng.shuffle(buyers)
    weights = [1 / (i + 1) ** 0.9 for i in range(len(buyers))]
    end = C.HISTORY_END
    span = (end - START).days
    line_id = rec_id = inv_id = 1
    for po_id in range(1, N_POS + 1):
        s = rng.choices(buyers, weights)[0]
        cat = s["category"] or "MRO"
        median, lead, items = CATEGORY[cat]
        cc = s["country"]
        cur = CURRENCY.get(cc, "EUR")
        fx = C.FX_FROM_EUR[cur]
        order = START + timedelta(days=rng.randint(0, span))
        need_by = order + timedelta(days=lead + rng.randint(-2, 5))
        n_lines = rng.choice([1, 1, 1, 2, 2, 3])
        total = 0.0
        created = datetime.combine(order, datetime.min.time()) + timedelta(hours=rng.randint(7, 18))
        po_lines = []
        for ln in range(1, n_lines + 1):
            value = math.exp(rng.gauss(math.log(median), 0.8)) * fx / n_lines
            qty = rng.choice([1, 1, 2, 5, 10, 20])
            price = round(value / qty, 2)
            lt = round(price * qty, 2)
            total += lt
            po_lines.append(dict(id=line_id, po_id=po_id, line_num=ln, description=rng.choice(items), commodity=cat,
                                 quantity=qty, price=price, total=lt, created_at=created, updated_at=created))
            line_id += 1
        status = "closed"
        received_any = False
        for pl in po_lines:
            late = rng.choice(lateness)
            if s["storyline"] == "delivery_drift" and order > end - timedelta(days=C.DRIFT_WINDOW_DAYS):
                frac = (order - (end - timedelta(days=C.DRIFT_WINDOW_DAYS))).days / C.DRIFT_WINDOW_DAYS
                if rng.random() < 0.6 * frac:
                    late += rng.randint(3, 15)
            rd = need_by + timedelta(days=late)
            if rd > end:
                status = "issued"
                pl["updated_at"] = created
                continue
            received_any = True
            rts = datetime.combine(rd, datetime.min.time()) + timedelta(hours=rng.randint(8, 17))
            receipts.append(dict(id=rec_id, order_line_id=pl["id"], received_date=rd, quantity=pl["quantity"],
                                 created_at=rts, updated_at=rts))
            rec_id += 1
        lines += po_lines
        last = max([r["updated_at"] for r in receipts[-n_lines:]] + [created]) if received_any else created
        pos.append(dict(id=po_id, po_number=f"PO{po_id + 100000}", supplier_id=s["id"],
                        company_code=ENTITY.get(cc, "NL01"), currency=cur, total=round(total, 2), status=status,
                        order_date=order, need_by_date=need_by, created_at=created, updated_at=last))
        if received_any:
            inv_date = min(need_by + timedelta(days=rng.randint(0, 25)), end)
            inv_total = round(total, 2)
            r = rng.random()
            if r < 0.03:
                bad = round(total * rng.choice([1.05, 1.08, 1.12, 0.9]), 2)
                defects.append(("invoice_amount_mismatch", f"INV-{inv_id}", inv_total, bad))
                inv_total = bad
            its = datetime.combine(inv_date, datetime.min.time()) + timedelta(hours=10)
            inv = dict(id=inv_id, invoice_number=f"{s['number'][-4:]}-{inv_id:06d}", supplier_id=s["id"], po_id=po_id,
                       invoice_date=inv_date, currency=cur, total=inv_total,
                       payment_terms=rng.choice(["Net 30", "Net 45", "Net 60"]),
                       status="paid" if inv_date < end - timedelta(days=45) else "approved",
                       created_at=its, updated_at=its)
            invoices.append(inv)
            inv_id += 1
            if r > 0.99:  # same invoice submitted twice
                invoices.append(dict(inv, id=inv_id, created_at=its + timedelta(days=9), updated_at=its + timedelta(days=9),
                                     status="approved"))
                defects.append(("duplicate_invoice", f"INV-{inv_id}", inv["invoice_number"], inv["invoice_number"]))
                inv_id += 1

    cid = 1
    for s in sup:
        if rng.random() < 0.4:
            start = START - timedelta(days=rng.randint(0, 700))
            endd = start + timedelta(days=rng.choice([365, 730, 1095]))
            cur = CURRENCY.get(s["country"], "EUR")
            ts = datetime.combine(start, datetime.min.time())
            contracts.append(dict(id=cid, number=f"CTR-{cid:05d}", supplier_id=s["id"],
                                  name=f"{(s['category'] or 'Supply').replace('_', ' ').title()} agreement",
                                  start_date=start, end_date=endd,
                                  value=round(math.exp(rng.gauss(math.log(150000), 0.9)) * C.FX_FROM_EUR[cur], 2),
                                  currency=cur, status="expired" if endd < end else "active",
                                  created_at=ts, updated_at=ts))
            cid += 1

    with psycopg.connect(C.pg_dsn("coupa"), autocommit=True) as pg:
        pg.execute(DDL)
        for table, rows in [("suppliers", suppliers), ("purchase_orders", pos), ("order_lines", lines),
                            ("receipts", receipts), ("invoices", invoices), ("contracts", contracts)]:
            if not rows:
                continue
            keys = list(rows[0])
            with pg.cursor().copy(f"copy {table} ({', '.join(keys)}) from stdin") as cp:
                for r in rows:
                    cp.write_row([r[k] for k in keys])

    # append invoice defects to ground truth
    gt = C.STAGE / "gt_defect.parquet"
    # idempotent re-runs: drop only the defects this step creates
    rel = db.sql(f"select * from '{gt}' where defect_type not in ('invoice_amount_mismatch', 'duplicate_invoice', 'schema_drift')")
    existing = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    n = len(existing)
    for i, (kind, key, orig, new) in enumerate(defects, start=1):
        existing.append(dict(defect_id=f"D{n + i:06d}", defect_type=kind, source_system="coupa", entity="invoice",
                             entity_key=key, field="total" if "mismatch" in kind else "invoice_number",
                             original_value=str(orig), injected_value=str(new), note=""))
    existing.append(dict(defect_id=f"D{n + len(defects) + 1:06d}", defect_type="schema_drift", source_system="coupa",
                         entity="supplier", entity_key="*", field="diversity_class", original_value=None,
                         injected_value="new field", note="present only on suppliers updated on/after 2025-06-01"))
    write_parquet(db, existing, gt)
    print(f"coupa: suppliers={len(suppliers)} purchase_orders={len(pos)} order_lines={len(lines)} "
          f"receipts={len(receipts)} invoices={len(invoices)} contracts={len(contracts)} defects={len(defects)}")

"""Live change simulator: keeps SAP and Coupa moving so CDC and incremental pulls have work.

Each batch advances a simulated business clock by a few hours (starting the day after HISTORY_END) and:
  SAP    new POs (ekko/ekpo/eket), goods receipts for due lines (ekbe), vendor master edits (lfa1 UPDATE),
         deletion flags (ekpo.loekz) and the occasional hard DELETE of a just-created PO (CDC delete events)
  Coupa  new POs + lines, receipts, invoices, supplier edits (bumps updated_at; newer records carry the
         schema-drift field `diversity_class`)
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

import psycopg

from . import config as C


class Sim:
    def __init__(self) -> None:
        self.rng = random.Random()
        self.erp = psycopg.connect(C.pg_dsn("erp"), autocommit=True)
        self.coupa = psycopg.connect(C.pg_dsn("coupa"), autocommit=True)
        last = self.erp.execute("select max(ebeln) from sap.ekko where ebeln like '46%'").fetchone()[0]
        self.next_ebeln = int(last) + 1 if last else 4600000000
        last_mat = self.erp.execute("select max(belnr) from sap.ekbe where belnr like '49%'").fetchone()[0]
        self.next_matdoc = int(last_mat) + 1 if last_mat else 4990000000
        self.clock = datetime.combine(C.HISTORY_END + timedelta(days=1), datetime.min.time()) + timedelta(hours=8)
        mx = self.coupa.execute("select max(updated_at) from purchase_orders").fetchone()[0]
        if mx and mx > self.clock:
            self.clock = mx + timedelta(hours=1)
        mx = self.erp.execute("select max(cpudt) from sap.ekbe").fetchone()[0]
        if mx and datetime.combine(mx, datetime.min.time()) > self.clock:
            self.clock = datetime.combine(mx, datetime.min.time()) + timedelta(hours=8)

    # ------------------------------------------------------------------ SAP
    def sap_new_po(self) -> str:
        e = self.erp
        tmpl = e.execute("""select ebeln, bukrs, lifnr, waers, zterm, ekorg, ekgrp from sap.ekko
                            where bukrs in ('US01','NL01','PL01','UA01') order by random() limit 1""").fetchone()
        ebeln = str(self.next_ebeln)
        self.next_ebeln += 1
        d = self.clock.date()
        e.execute("""insert into sap.ekko (ebeln, bukrs, bstyp, bsart, aedat, ernam, lifnr, zterm, ekorg, ekgrp,
                     waers, wkurs, bedat) values (%s,%s,'F','NB',%s,'SIMULATOR',%s,%s,%s,%s,%s,1.0,%s)""",
                  (ebeln, tmpl[1], d, tmpl[2], tmpl[4], tmpl[5], tmpl[6], tmpl[3], d))
        for i, line in enumerate(e.execute("""select txz01, matnr, werks, matkl, menge, meins, netpr, peinh, mwskz
                                              from sap.ekpo where ebeln = %s order by ebelp limit 3""", (tmpl[0],))):
            ebelp = f"{(i + 1) * 10:05d}"
            menge = round(float(line[4] or 1) * self.rng.uniform(0.5, 1.5), 3)
            netwr = round(menge * float(line[6] or 1) / float(line[7] or 1), 2)
            e.execute("""insert into sap.ekpo (ebeln, ebelp, aedat, txz01, matnr, bukrs, werks, matkl, menge, meins,
                         netpr, peinh, netwr, brtwr, mwskz, pstyp) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'0')""",
                      (ebeln, ebelp, d, line[0], line[1], tmpl[1], line[2], line[3], menge, line[5], line[6], line[7],
                       netwr, netwr, line[8]))
            e.execute("insert into sap.eket (ebeln, ebelp, etenr, eindt, slfdt, menge, wemng) values (%s,%s,'0001',%s,%s,%s,0)",
                      (ebeln, ebelp, d + timedelta(days=self.rng.randint(3, 21)), d, menge))
        return ebeln

    def sap_goods_receipts(self, n: int) -> int:
        e = self.erp
        due = e.execute("""select t.ebeln, t.ebelp, t.menge, p.netpr, p.peinh, k.waers from sap.eket t
                           join sap.ekpo p using (ebeln, ebelp) join sap.ekko k using (ebeln)
                           where t.wemng = 0 and t.eindt <= %s and t.ebeln like '46%%' order by t.eindt limit %s""",
                        (self.clock.date() + timedelta(days=5), n)).fetchall()
        for ebeln, ebelp, menge, netpr, peinh, waers in due:
            belnr = str(self.next_matdoc)
            self.next_matdoc += 1
            amt = round(float(menge) * float(netpr or 0) / float(peinh or 1), 2)
            d = self.clock.date()
            e.execute("""insert into sap.ekbe (ebeln, ebelp, zekkn, vgabe, gjahr, belnr, buzei, bewtp, bwart, budat,
                         menge, dmbtr, wrbtr, waers, shkzg, cpudt, cputm)
                         values (%s,%s,'00','1',%s,%s,'0001','E','101',%s,%s,%s,%s,%s,'S',%s,%s)""",
                      (ebeln, ebelp, str(d.year), belnr, d, menge, amt, amt, waers, d, self.clock.strftime("%H:%M:%S")))
            e.execute("update sap.eket set wemng = menge where ebeln = %s and ebelp = %s", (ebeln, ebelp))
        return len(due)

    def sap_vendor_edit(self) -> None:
        r = self.rng.random()
        if r < 0.5:
            self.erp.execute("""update sap.lfa1 set telf1 = %s where lifnr = (select lifnr from sap.lfa1
                                order by random() limit 1)""", (f"+{self.rng.randint(1, 99)} {self.rng.randint(10**8, 10**9 - 1)}",))
        elif r < 0.8:
            self.erp.execute("""update sap.lfa1 set stras = %s where lifnr = (select lifnr from sap.lfa1
                                order by random() limit 1)""", (f"{self.rng.randint(1, 400)} Industrial Way",))
        else:
            self.erp.execute("""update sap.lfa1 set sperr = 'X' where lifnr = (select lifnr from sap.lfa1
                                where sperr is null order by random() limit 1)""")  # purchasing block

    def sap_deletes(self) -> None:
        if self.rng.random() < 0.3:
            self.erp.execute("""update sap.ekpo set loekz = 'L' where (ebeln, ebelp) = (select ebeln, ebelp from sap.ekpo
                                where ebeln like '46%%' and loekz is null order by random() limit 1)""")
        if self.rng.random() < 0.1:  # PO created by mistake and removed before any receipt
            row = self.erp.execute("""select k.ebeln from sap.ekko k where k.ebeln like '46%%'
                                      and not exists (select 1 from sap.ekbe b where b.ebeln = k.ebeln)
                                      order by k.ebeln desc limit 1""").fetchone()
            if row:
                for t in ("eket", "ekpo", "ekko"):
                    self.erp.execute(f"delete from sap.{t} where ebeln = %s", row)

    # ------------------------------------------------------------------ Coupa
    def coupa_activity(self) -> None:
        c, ts = self.coupa, self.clock
        sup = c.execute("select id, country from suppliers order by random() limit 1").fetchone()
        po_id = c.execute("select coalesce(max(id), 0) + 1 from purchase_orders").fetchone()[0]
        line_id = c.execute("select coalesce(max(id), 0) + 1 from order_lines").fetchone()[0]
        cur = {"US": "USD", "PL": "PLN", "UA": "UAH"}.get(sup[1], "EUR")
        total = round(self.rng.uniform(200, 20000), 2)
        c.execute("""insert into purchase_orders (id, po_number, supplier_id, company_code, currency, total, status,
                     order_date, need_by_date, created_at, updated_at) values (%s,%s,%s,%s,%s,%s,'issued',%s,%s,%s,%s)""",
                  (po_id, f"PO{po_id + 100000}", sup[0], {"US": "US01", "PL": "PL01", "UA": "UA01"}.get(sup[1], "NL01"),
                   cur, total, ts.date(), ts.date() + timedelta(days=self.rng.randint(5, 20)), ts, ts))
        c.execute("""insert into order_lines (id, po_id, line_num, description, commodity, quantity, price, total,
                     created_at, updated_at) values (%s,%s,1,'Simulated order',
                     (select commodity from suppliers where id=%s),1,%s,%s,%s,%s)""",
                  (line_id, po_id, sup[0], total, total, ts, ts))
        # receive + invoice an older open PO
        open_po = c.execute("""select p.id, p.total, p.currency, p.supplier_id, l.id, l.quantity from purchase_orders p
                               join order_lines l on l.po_id = p.id where p.status = 'issued' and p.id < %s
                               order by p.need_by_date limit 1""", (po_id,)).fetchone()
        if open_po:
            rid = c.execute("select coalesce(max(id), 0) + 1 from receipts").fetchone()[0]
            iid = c.execute("select coalesce(max(id), 0) + 1 from invoices").fetchone()[0]
            c.execute("insert into receipts values (%s,%s,%s,%s,%s,%s)", (rid, open_po[4], ts.date(), open_po[5], ts, ts))
            c.execute("update purchase_orders set status = 'closed', updated_at = %s where id = %s", (ts, open_po[0]))
            c.execute("""insert into invoices values (%s,%s,%s,%s,%s,%s,%s,'Net 30','pending',%s,%s)""",
                      (iid, f"SIM-{iid:06d}", open_po[3], open_po[0], ts.date(), open_po[2], open_po[1], ts, ts))
        if self.rng.random() < 0.3:
            c.execute("""update suppliers set updated_at = %s, diversity_class = coalesce(diversity_class, 'none'),
                         city = coalesce(city, 'Unknown') where id = (select id from suppliers order by random() limit 1)""",
                      (ts,))

    # ------------------------------------------------------------------ loop
    def batch(self) -> dict:
        self.clock += timedelta(hours=self.rng.randint(2, 6))
        new = [self.sap_new_po() for _ in range(self.rng.randint(1, 4))]
        grs = self.sap_goods_receipts(self.rng.randint(1, 6))
        for _ in range(self.rng.randint(0, 2)):
            self.sap_vendor_edit()
        self.sap_deletes()
        for _ in range(self.rng.randint(1, 3)):
            self.coupa_activity()
        return {"clock": self.clock.isoformat(timespec="minutes"), "sap_new_pos": len(new), "sap_grs": grs}


def run(interval: float, batches: int) -> None:
    sim = Sim()
    i = 0
    while batches == 0 or i < batches:
        print(sim.batch(), flush=True)
        i += 1
        if batches == 0 or i < batches:
            time.sleep(interval)

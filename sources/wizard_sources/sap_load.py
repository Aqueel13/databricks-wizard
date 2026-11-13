"""Load the SAP mock (Postgres database `erp`, schema `sap`) from the real SAP demo tables.

Transformations (all recorded in data/stage):
  * company codes renamed to Larkspur Foods (US01, NL01) and two regional codes added (PL01, UA01)
  * POs re-pointed from the 38 original vendors to the supplier universe (Pareto-weighted)
  * line values log-compressed (quantities scaled, unit prices kept) so amounts are plausible
  * NL01 POs sampled and cloned into PL01 (PLN) and UA01 (UAH) for regional food suppliers
  * dates shifted so the newest PO lands on config.HISTORY_END
  * delivery drift injected for storyline suppliers (goods receipts get later over the drift window)
"""
from __future__ import annotations

import duckdb
import psycopg

from . import config as C

DDL = """
drop schema if exists sap cascade;
create schema sap;
create table sap.t001  (bukrs varchar(4) primary key, butxt varchar(60), ort01 varchar(40), land1 varchar(3), waers varchar(5));
create table sap.t023t (matkl varchar(9) primary key, wgbez varchar(60));
create table sap.tcurr (kurst varchar(4), fcurr varchar(5), tcurr varchar(5), gdatu varchar(8), ukurs numeric(15,5),
                        primary key (kurst, fcurr, tcurr, gdatu));
create table sap.lfa1  (lifnr varchar(10) primary key, name1 varchar(35), name2 varchar(200), land1 varchar(3),
                        ort01 varchar(60), pstlz varchar(10), regio varchar(3), stras varchar(60), stcd1 varchar(16),
                        stcd2 varchar(16), stceg varchar(20), telf1 varchar(30), ktokk varchar(4), loevm varchar(1),
                        sperr varchar(1), spras varchar(2), erdat date, ernam varchar(12));
create table sap.lfb1  (lifnr varchar(10), bukrs varchar(4), akont varchar(10), zterm varchar(4), erdat date,
                        primary key (lifnr, bukrs));
create table sap.ekko  (ebeln varchar(12) primary key, bukrs varchar(4), bstyp varchar(1), bsart varchar(4),
                        loekz varchar(1), aedat date, ernam varchar(12), lifnr varchar(10), zterm varchar(4),
                        ekorg varchar(4), ekgrp varchar(3), waers varchar(5), wkurs numeric(12,5), bedat date,
                        kdatb date, kdate date, inco1 varchar(3));
create table sap.ekpo  (ebeln varchar(12), ebelp varchar(5), loekz varchar(1), aedat date, txz01 varchar(60),
                        matnr varchar(40), bukrs varchar(4), werks varchar(4), matkl varchar(9), menge numeric(17,3),
                        meins varchar(3), netpr numeric(15,2), peinh numeric(7,0), netwr numeric(17,2),
                        brtwr numeric(17,2), mwskz varchar(2), elikz varchar(1), erekz varchar(1), pstyp varchar(1),
                        knttp varchar(1), webaz numeric(5,0), primary key (ebeln, ebelp));
create table sap.eket  (ebeln varchar(12), ebelp varchar(5), etenr varchar(4), eindt date, slfdt date,
                        menge numeric(17,3), wemng numeric(17,3), primary key (ebeln, ebelp, etenr));
create table sap.ekbe  (ebeln varchar(12), ebelp varchar(5), zekkn varchar(2), vgabe varchar(1), gjahr varchar(4),
                        belnr varchar(12), buzei varchar(4), bewtp varchar(1), bwart varchar(3), budat date,
                        menge numeric(17,3), dmbtr numeric(17,2), wrbtr numeric(17,2), waers varchar(5),
                        shkzg varchar(1), xblnr varchar(16), cpudt date, cputm varchar(8),
                        primary key (ebeln, ebelp, zekkn, vgabe, gjahr, belnr, buzei));
create table sap.rbkp  (belnr varchar(12), gjahr varchar(4), blart varchar(2), bldat date, budat date,
                        bukrs varchar(4), lifnr varchar(10), waers varchar(5), kursf numeric(12,5),
                        rmwwr numeric(17,2), wmwst1 numeric(17,2), zterm varchar(4), zbd1t numeric(3,0),
                        xblnr varchar(16), rbstat varchar(1), stblg varchar(12), primary key (belnr, gjahr));
create table sap.rseg  (belnr varchar(12), gjahr varchar(4), buzei varchar(6), ebeln varchar(12), ebelp varchar(5),
                        matnr varchar(40), bukrs varchar(4), werks varchar(4), wrbtr numeric(17,2), shkzg varchar(1),
                        mwskz varchar(2), menge numeric(17,3), bstme varchar(3), primary key (belnr, gjahr, buzei));
-- full before-images in CDC update/delete events
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'sap' loop
    execute format('alter table sap.%I replica identity full', t);
  end loop; end $$;
"""


def _csv(t: str) -> str:
    return f"read_csv('{C.RAW / 'sap' / (t + '.csv')}', all_varchar=true, header=true, quote='\"', escape='\"')"


def _d(col: str) -> str:
    """shifted date expression"""
    return f"(try_cast(nullif({col}, '') as date) + getvariable('shift'))"


def _n(col: str) -> str:
    return f"try_cast(nullif({col}, '') as double)"


def date_shift_days(db: duckdb.DuckDBPyConnection) -> int:
    """Days added to every SAP date so the newest PO lands on HISTORY_END. One definition for load() and
    drift_patch(): the newest order date AFTER de-duplicating SAP clients (the raw extract repeats documents
    across clients with different dates)."""
    max_bedat = db.sql(f"""
        select max(try_cast(bedat as date)) from (
          select * from {_csv('ekko')} qualify row_number() over (partition by ebeln order by mandt desc) = 1)
        where bukrs in ({', '.join(repr(k) for k in C.SAP_BUKRS_MAP)})""").fetchone()[0]
    return (C.HISTORY_END - max_bedat).days


def drift_sql(b: str, seed: int, end: str) -> str:
    """Delay rule for drift suppliers' goods receipts (shared by load() and drift_patch(), so both give the
    same result): the share of delayed receipts rises linearly across the window to DRIFT_MAX_SHARE."""
    budat0 = _d(f"{b}.budat")
    return f"""                         and hash({b}.belnr || 'p{seed}') % 100 < {C.DRIFT_MAX_SHARE}.0 * ({budat0} - (date '{end}' - {C.DRIFT_WINDOW_DAYS})) / {C.DRIFT_WINDOW_DAYS}
                    then {C.DRIFT_DELAY_MIN} + cast(hash({b}.belnr || '{seed}') % {C.DRIFT_DELAY_SPAN} as integer)"""


def drift_patch() -> None:
    """Re-apply delivery drift to an existing SAP mock with the current DRIFT_* settings, in place.

    For every goods receipt of a drift supplier, the original posting date is recomputed from the raw SAP data
    and the current delay rule applied, exactly as a fresh load() would. Rows are UPDATEd in Postgres, so the
    change reaches Databricks through CDC like any other change. (The fiscal year key GJAHR is left as is.)"""
    db = duckdb.connect()
    db.execute("install postgres; load postgres;")
    db.execute(f"attach '{C.pg_dsn('erp')}' as pg (type postgres)")
    seed, end = C.SEED, C.HISTORY_END.isoformat()
    db.execute(f"create view raw_ekbe as select * from {_csv('ekbe')} "
               f"qualify row_number() over (partition by ebeln, ebelp, zekkn, vgabe, gjahr, belnr, buzei order by mandt desc) = 1")
    db.execute(f"set variable shift = {date_shift_days(db)}")
    db.execute(f"""
        create table patch as
        select g.ebeln, g.ebelp, g.zekkn, g.vgabe, g.gjahr, g.belnr, g.buzei, g.budat as old_budat,
               least({_d('b.budat')} + case when {_d('b.budat')} > date '{end}' - {C.DRIFT_WINDOW_DAYS}
{drift_sql('b', seed, end)}
                    else 0 end, date '{end}') as new_budat
        from pg.sap.ekbe g
        join pg.sap.ekko k on k.ebeln = g.ebeln
        join (select d.lifnr from '{C.STAGE / 'sys_sap_direct.parquet'}' d
              join '{C.STAGE / 'gt_supplier.parquet'}' s using (true_supplier_id) where s.storyline = 'delivery_drift') v
          on v.lifnr = k.lifnr
        join raw_ekbe b on b.ebeln = g.ebeln and b.ebelp = g.ebelp and coalesce(b.zekkn, '00') = g.zekkn
                       and b.vgabe = g.vgabe and b.belnr = g.belnr and coalesce(b.buzei, '0000') = g.buzei
        where g.vgabe = '1'""")
    changed = db.sql("select count(*) from patch where new_budat <> old_budat").fetchone()[0]
    total = db.sql("select count(*) from patch").fetchone()[0]
    db.execute("""update pg.sap.ekbe set budat = p.new_budat, cpudt = p.new_budat from patch p
                  where ekbe.ebeln = p.ebeln and ekbe.ebelp = p.ebelp and ekbe.zekkn = p.zekkn and ekbe.vgabe = p.vgabe
                    and ekbe.gjahr = p.gjahr and ekbe.belnr = p.belnr and ekbe.buzei = p.buzei
                    and p.new_budat <> p.old_budat""")
    late = db.sql("""select count(*) from patch p join pg.sap.eket e on e.ebeln = p.ebeln and e.ebelp = p.ebelp
                     where p.new_budat > e.eindt and p.new_budat > date '2026-10-31' - interval 270 days""").fetchone()[0]
    print(f"drift-patch: {total} goods receipts of drift suppliers, {changed} updated; "
          f"{late} late receipts in the drift window now")


def load() -> None:
    with psycopg.connect(C.pg_dsn("erp"), autocommit=True) as pg:
        pg.execute(DDL)

    db = duckdb.connect()
    db.execute("install postgres; load postgres;")
    db.execute(f"attach '{C.pg_dsn('erp')}' as pg (type postgres)")
    seed = C.SEED
    bukrs_map = " ".join(f"when '{k}' then '{v}'" for k, v in C.SAP_BUKRS_MAP.items())
    fx = C.FX_FROM_EUR

    # ---------------------------------------------------------------- base views
    # the demo extract repeats rows across SAP clients (mandt); keep one row per SAP key
    keys = {"lfa1": "lifnr", "ekko": "ebeln", "ekpo": "ebeln, ebelp", "eket": "ebeln, ebelp, etenr",
            "ekbe": "ebeln, ebelp, zekkn, vgabe, gjahr, belnr, buzei", "rbkp": "belnr, gjahr",
            "rseg": "belnr, gjahr, buzei", "t023t": "spras, matkl", "tcurr": "kurst, fcurr, tcurr, gdatu"}
    for t, k in keys.items():
        db.execute(f"create table raw_{t} as select * from {_csv(t)} "
                   f"qualify row_number() over (partition by {k} order by mandt desc) = 1")
    db.execute(f"create table ekko0 as select *, case bukrs {bukrs_map} end nb from raw_ekko "
               f"where bukrs in ({', '.join(repr(k) for k in C.SAP_BUKRS_MAP)})")
    db.execute(f"set variable shift = {date_shift_days(db)}")

    # vendors receiving direct spend, Pareto weights -> cumulative intervals per company code
    db.execute(f"""
        create table vend as
        with v as (select bukrs, lifnr, true_supplier_id, 1.0 / pow(rank, 1.1) w from '{C.STAGE / 'sys_sap_direct.parquet'}'
                   union all
                   select bukrs, lifnr, null, 1.0 / pow(row_number() over (partition by bukrs order by hash(lifnr || '{seed}')), 1.1)
                   from '{C.STAGE / 'sys_sap_vendor.parquet'}' s
                   join '{C.STAGE / 'gt_source_record.parquet'}' r on r.source_system = 'sap' and r.source_id = s.lifnr
                   join '{C.STAGE / 'gt_supplier.parquet'}' g using (true_supplier_id)
                   where s.bukrs in ('PL01', 'UA01') and g.category = 'DIRECT_INGREDIENTS' and not r.is_injected_duplicate)
        select bukrs, lifnr, sum(w) over (partition by bukrs order by w desc, lifnr rows unbounded preceding) / sum(w) over (partition by bukrs) hi,
               (sum(w) over (partition by bukrs order by w desc, lifnr rows unbounded preceding) - w) / sum(w) over (partition by bukrs) lo
        from v""")

    # PO headers: originals + regional clones
    db.execute(f"""
        create table hdr as
        select ebeln src_ebeln, ebeln, nb bukrs, 'EUR' cur_from, case nb when 'US01' then 'USD' else 'EUR' end waers, 1.0 fxf, * exclude (ebeln, bukrs, waers, nb)
        from ekko0
        union all
        select ebeln, '71' || ebeln, 'PL01', 'EUR', 'PLN', {fx['PLN']}, * exclude (ebeln, bukrs, waers, nb)
        from ekko0 where nb = 'NL01' and hash(ebeln || 'PL{seed}') % 1000 < {int(C.PL_PO_SHARE * 1000)}
        union all
        select ebeln, '72' || ebeln, 'UA01', 'EUR', 'UAH', {fx['UAH']}, * exclude (ebeln, bukrs, waers, nb)
        from ekko0 where nb = 'NL01' and hash(ebeln || 'UA{seed}') % 1000 < {int(C.UA_PO_SHARE * 1000)}""")
    db.execute(f"""
        create table hdr2 as
        select h.*, v.lifnr new_lifnr from hdr h
        join vend v on v.bukrs = h.bukrs
          and (hash(h.ebeln || '{seed}') % 1000000) / 1000000.0 >= v.lo
          and (hash(h.ebeln || '{seed}') % 1000000) / 1000000.0 < v.hi""")
    pfx = "case h.bukrs when 'PL01' then '71' when 'UA01' then '72' else '' end"

    # line-level value compression factor
    db.execute(f"""
        create table lines as
        select h.ebeln, p.ebelp, h.src_ebeln, h.bukrs, h.fxf, h.waers,
               {_n('p.netwr')} netwr0,
               case when coalesce({_n('p.netwr')}, 0) > 1
                    then least(pow({_n('p.netwr')}, 0.75), 2000000) / {_n('p.netwr')} else 1 end f
        from hdr2 h join raw_ekpo p on p.ebeln = h.src_ebeln""")

    ins = []
    ins.append("""insert into pg.sap.t001 values
        """ + ",".join(f"('{k}', '{v[0].replace(chr(39), chr(39) * 2)}', '{v[3]}', '{v[1]}', '{v[2]}')"
                       for k, v in C.COMPANY_CODES.items()))
    ins.append("insert into pg.sap.t023t select matkl, any_value(wgbez) from raw_t023t where spras = 'E' and matkl is not null group by 1")
    ins.append("""insert into pg.sap.tcurr select kurst, fcurr, tcurr, gdatu, any_value(try_cast(ukurs as double))
                  from raw_tcurr where kurst is not null and gdatu is not null group by all""")
    ins.append(f"""insert into pg.sap.lfa1
        select lifnr, name1, name2, land1, ort01, pstlz, regio, stras, nullif(stcd1, ''), nullif(stcd2, ''),
               nullif(stceg, ''), nullif(telf1, ''), ktokk, nullif(loevm, ''), nullif(sperr, ''), spras,
               {_d('erdat')}, ernam
        from raw_lfa1
        qualify row_number() over (partition by lifnr order by mandt desc) = 1
        union all
        select lifnr, name1, name2, land1, ort01, pstlz, null, stras, stcd1, null, null, null, ktokk, null, null,
               spras, try_cast(erdat as date), 'GENERATOR'
        from '{C.STAGE / 'sys_sap_vendor.parquet'}'""")
    ins.append(f"""insert into pg.sap.lfb1
        select lifnr, bukrs, '160000', 'Z030', min(erdat) from (
          select lifnr, bukrs, try_cast(erdat as date) erdat from '{C.STAGE / 'sys_sap_vendor.parquet'}'
          union all
          select l.lifnr, case when l.land1 = 'US' then 'US01' else 'NL01' end, {_d('l.erdat')} from raw_lfa1 l
          union all
          select lifnr, bukrs, null from '{C.STAGE / 'sys_sap_direct.parquet'}'
        ) group by 1, 2""")
    ins.append(f"""insert into pg.sap.ekko
        select ebeln, bukrs, bstyp, bsart, nullif(loekz, ''), {_d('aedat')}, ernam, new_lifnr, zterm, ekorg,
               ekgrp, waers, 1.0, {_d('bedat')}, {_d('kdatb')}, {_d('kdate')}, nullif(inco1, '')
        from hdr2""")
    ins.append(f"""insert into pg.sap.ekpo
        select l.ebeln, p.ebelp, nullif(p.loekz, ''), {_d('p.aedat')}, p.txz01, p.matnr, l.bukrs, p.werks, p.matkl,
               round({_n('p.menge')} * l.f, 3), p.meins, round({_n('p.netpr')} * l.fxf, 2), {_n('p.peinh')},
               round({_n('p.netwr')} * l.f * l.fxf, 2), round({_n('p.brtwr')} * l.f * l.fxf, 2), p.mwskz,
               nullif(p.elikz, ''), nullif(p.erekz, ''), p.pstyp, nullif(p.knttp, ''), {_n('p.webaz')}
        from lines l join raw_ekpo p on p.ebeln = l.src_ebeln and p.ebelp = l.ebelp""")
    ins.append(f"""insert into pg.sap.eket
        select l.ebeln, e.ebelp, e.etenr, {_d('e.eindt')}, {_d('e.slfdt')}, round({_n('e.menge')} * l.f, 3),
               round({_n('e.wemng')} * l.f, 3)
        from lines l join raw_eket e on e.ebeln = l.src_ebeln and e.ebelp = l.ebelp""")

    # goods receipts / invoice receipts, with drift for storyline suppliers
    db.execute(f"""
        create table drift_vendors as
        select d.lifnr from '{C.STAGE / 'sys_sap_direct.parquet'}' d
        join '{C.STAGE / 'gt_supplier.parquet'}' g using (true_supplier_id) where g.storyline = 'delivery_drift'""")
    end = C.HISTORY_END.isoformat()
    db.execute(f"""
        create table ekbe1 as
        select l.ebeln, b.* exclude (ebeln), l.f, l.fxf, l.waers new_waers, {pfx.replace('h.', 'l.')} pfx,
               {_d('b.budat')} budat0,
               case when b.vgabe = '1' and h.new_lifnr in (select lifnr from drift_vendors)
                         and {_d('b.budat')} > date '{end}' - {C.DRIFT_WINDOW_DAYS}
{drift_sql('b', seed, end)}
                    else 0 end delay
        from lines l join raw_ekbe b on b.ebeln = l.src_ebeln and b.ebelp = l.ebelp
        join hdr2 h on h.ebeln = l.ebeln""")
    ins.append(f"""insert into pg.sap.ekbe
        select ebeln, ebelp, coalesce(zekkn, '00'), vgabe, cast(year(least(budat0 + delay, date '{end}')) as varchar),
               pfx || belnr, coalesce(buzei, '0000'), bewtp, bwart, least(budat0 + delay, date '{end}'), round({_n('menge')} * f, 3),
               round({_n('dmbtr')} * f * fxf, 2), round({_n('wrbtr')} * f * fxf, 2), new_waers, shkzg,
               nullif(xblnr, ''), least({_d('cpudt')} + delay, date '{end}'), cputm
        from ekbe1
        qualify row_number() over (partition by ebeln, ebelp, coalesce(zekkn, '00'), vgabe, belnr, coalesce(buzei, '0000'),
                                   year(least(budat0 + delay, date '{end}')) order by budat0) = 1""")

    # invoices: follow the PO via RSEG; header totals rescaled by the lines' factors
    db.execute(f"""
        create table rseg1 as
        select l.ebeln new_ebeln, l.bukrs new_bukrs, l.f, l.fxf, l.waers new_waers,
               {pfx.replace('h.', 'l.')} pfx, r.*
        from raw_rseg r join lines l on l.src_ebeln = r.ebeln and l.ebelp = r.ebelp""")
    db.execute(f"""
        create table inv as
        select r.pfx, r.belnr, r.gjahr, any_value(r.new_bukrs) bukrs, any_value(r.new_waers) waers,
               any_value(h.new_lifnr) lifnr,
               sum({_n('r.wrbtr')} * r.f * r.fxf) / nullif(sum({_n('r.wrbtr')}), 0) ratio
        from rseg1 r join hdr2 h on h.ebeln = r.new_ebeln group by 1, 2, 3""")
    ins.append(f"""insert into pg.sap.rbkp
        select i.pfx || k.belnr, cast(year({_d('k.budat')}) as varchar), k.blart, {_d('k.bldat')}, {_d('k.budat')},
               i.bukrs, i.lifnr, i.waers, 1.0, round({_n('k.rmwwr')} * coalesce(i.ratio, 1), 2),
               round({_n('k.wmwst1')} * coalesce(i.ratio, 1), 2), k.zterm, {_n('k.zbd1t')}, nullif(k.xblnr, ''),
               nullif(k.rbstat, ''), nullif(k.stblg, '')
        from inv i join raw_rbkp k on k.belnr = i.belnr and k.gjahr = i.gjahr
        qualify row_number() over (partition by i.pfx, k.belnr, year({_d('k.budat')}) order by k.budat) = 1""")
    ins.append(f"""insert into pg.sap.rseg
        select r.pfx || r.belnr, cast(year({_d('k.budat')}) as varchar), r.buzei, r.new_ebeln, r.ebelp, r.matnr,
               r.new_bukrs, r.werks, round({_n('r.wrbtr')} * r.f * r.fxf, 2), r.shkzg, r.mwskz,
               round({_n('r.menge')} * r.f, 3), r.bstme
        from rseg1 r join raw_rbkp k on k.belnr = r.belnr and k.gjahr = r.gjahr
        qualify row_number() over (partition by r.pfx, r.belnr, r.gjahr, r.buzei order by r.ebeln) = 1""")

    for sql in ins:
        db.execute(sql)

    # record what was done
    drift = db.sql("select count(*) from ekbe1 where delay > 0").fetchone()[0]
    counts = {t: db.sql(f"select count(*) from pg.sap.{t}").fetchone()[0]
              for t in ("lfa1", "lfb1", "ekko", "ekpo", "eket", "ekbe", "rbkp", "rseg", "tcurr")}
    by_bukrs = db.sql("select bukrs, count(*), count(distinct lifnr), min(bedat), max(bedat) from pg.sap.ekko "
                      "group by 1 order by 1").fetchall()
    db.execute(f"copy (select bukrs, lifnr, lo, hi from vend) to '{C.STAGE / 'sap_po_vendor_weights.parquet'}' (format parquet)")
    print("rows:", counts)
    print("POs by company code (bukrs, POs, vendors, first, last):")
    for r in by_bukrs:
        print("  ", r)
    print(f"goods receipts delayed by drift: {drift}")

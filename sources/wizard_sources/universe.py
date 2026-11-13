"""Build the supplier universe and place every supplier into the source systems.

Outputs (data/stage/):
  gt_supplier.parquet        one row per real-world supplier (the answer key)
  gt_source_record.parquet   every source-system record -> true supplier, with how its name was varied
  gt_defect.parquet          every injected defect (appended to by later steps)
  sys_sap_vendor.parquet     new LFA1 rows (regional, indirect, fictional, duplicates)
  sys_sap_direct.parquet     which SAP vendors receive the real direct-spend POs, per company code
  sys_coupa_supplier.parquet
  sys_hicx_supplier.parquet
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import date, datetime, timedelta

import duckdb

from . import config as C
from . import text as T

SAP_CSV = "read_csv('{f}', all_varchar=true, header=true, quote='\"', escape='\"')"

CPV2_CATEGORY = {
    "15": "DIRECT_INGREDIENTS",
    "30": "IT_SERVICES", "32": "IT_SERVICES", "48": "IT_SERVICES", "72": "IT_SERVICES", "64": "IT_SERVICES",
    "31": "MRO", "42": "MRO", "43": "MRO", "44": "MRO", "50": "MRO",
    "34": "LOGISTICS", "60": "LOGISTICS", "63": "LOGISTICS",
    "79": "PROFESSIONAL_SERVICES", "71": "PROFESSIONAL_SERVICES", "73": "PROFESSIONAL_SERVICES",
    "80": "PROFESSIONAL_SERVICES", "66": "PROFESSIONAL_SERVICES",
    "90": "FACILITIES", "45": "FACILITIES", "39": "FACILITIES", "70": "FACILITIES", "65": "FACILITIES",
    "09": "FACILITIES", "55": "FACILITIES",
}


def stable_hash(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:12], 16)


def wb_category(c: str | None) -> str:
    c = (c or "").upper()
    if any(k in c for k in ("SOFTWARE", "IT ", "IT HARD", "CLOUD", "TELECOM")) or c == "IT SERVICES":
        return "IT_SERVICES"
    if "TRANSPORT" in c:
        return "LOGISTICS"
    if any(k in c for k in ("BUILDING", "FURNITURE", "CATERED", "MEETING")):
        return "FACILITIES"
    if c.startswith("MISC"):
        return "MRO"
    return "PROFESSIONAL_SERVICES"


def _rnd_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def norm_key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Builder:
    def __init__(self) -> None:
        self.rng = random.Random(C.SEED)
        self.db = duckdb.connect()
        self.suppliers: list[dict] = []
        self.records: list[dict] = []
        self.defects: list[dict] = []
        self.sap_new: list[dict] = []
        self.coupa: list[dict] = []
        self.hicx: list[dict] = []
        self.sap_direct: list[dict] = []
        self._lifnr_next = {"WB": 7000000, "FIC": 7050000, "PL": 7100000, "UA": 7200000, "DUP": 7900000}
        self._coupa_next = 1
        self._hicx_next = 1000001
        self._defect_next = 1

    # ------------------------------------------------------------ helpers
    def defect(self, kind: str, system: str, entity: str, key: str, field: str | None, original, injected, note=""):
        self.defects.append(dict(defect_id=f"D{self._defect_next:06d}", defect_type=kind, source_system=system,
                                 entity=entity, entity_key=key, field=field,
                                 original_value=None if original is None else str(original),
                                 injected_value=None if injected is None else str(injected), note=note))
        self._defect_next += 1

    def record(self, system: str, source_id: str, ts_id: str, name: str, methods: list[str], dup: bool = False):
        self.records.append(dict(source_system=system, source_id=source_id, true_supplier_id=ts_id,
                                 name_as_recorded=name, variant_methods=",".join(methods) or "none",
                                 is_injected_duplicate=dup))

    def new_lifnr(self, kind: str) -> str:
        while True:
            n = self._lifnr_next[kind]
            self._lifnr_next[kind] += 1
            if f"{n:010d}" not in self._taken:
                return f"{n:010d}"

    # ------------------------------------------------------------ national IDs with defects
    def maybe_bad_id(self, system: str, key: str, id_type: str, value: str | None, p_missing: float,
                     p_corrupt: float) -> str | None:
        if not value:
            return None
        r = self.rng.random()
        if r < p_missing:
            self.defect("missing_national_id", system, "supplier", key, id_type, value, None)
            return None
        if r < p_missing + p_corrupt:
            bad = T.corrupt_check_digit(value, self.rng, ein_format=C.EIN_FORMAT_DEFECTS)
            self.defect("invalid_national_id", system, "supplier", key, id_type, value, bad, "check digit changed")
            return bad
        return value

    # ------------------------------------------------------------ 1. SAP demo vendors (natural duplicates)
    def sap_demo(self) -> None:
        rows = self.db.sql(f"""
            select lifnr, name1, name2, land1, ort01, pstlz, stras, loevm
            from {SAP_CSV.format(f=C.RAW / 'sap/lfa1.csv')}
            qualify row_number() over (partition by lifnr order by mandt desc) = 1""").fetchall()
        self._taken = {r[0] for r in rows}
        clusters: dict[str, list] = {}
        for r in rows:
            clusters.setdefault(norm_key(r[1]) + "|" + norm_key(r[4]), []).append(r)
        for members in clusters.values():
            members.sort(key=lambda r: r[0])
            canon = members[0]
            ts = f"TS-SAP-{canon[0]}"
            self.suppliers.append(dict(true_supplier_id=ts, canonical_name=canon[1], country=canon[3], origin="sap_demo",
                                       national_id_type=None, national_id=None, category=None, real_identity=False,
                                       city=canon[4], storyline=None))
            for m in members:
                self.record("sap", m[0], ts, m[1], ["natural"] if m is not canon else [])
        # direct-spend vendors per company code: one active lifnr per cluster, sorted deterministically
        def pick(countries: set[str], n: int, bukrs: str):
            cands = [ms[0] for ms in clusters.values() if ms[0][3] in countries and not ms[0][7] and ms[0][1]]
            cands.sort(key=lambda r: stable_hash(f"{C.SEED}:{r[0]}"))
            for rank, r in enumerate(cands[:n], start=1):
                self.sap_direct.append(dict(bukrs=bukrs, lifnr=r[0], rank=rank, true_supplier_id=f"TS-SAP-{r[0]}"))
        pick({"US"}, C.N_SAP_DIRECT_US, "US01")
        pick({"DE", "NL", "FR", "GB", "IT", "ES", "BE", "AT", "CH", "DK", "SE"}, C.N_SAP_DIRECT_NL, "NL01")
        direct_ids = {d["true_supplier_id"] for d in self.sap_direct}
        for s in self.suppliers:
            if s["true_supplier_id"] in direct_ids:
                s["category"] = "DIRECT_INGREDIENTS"

    # ------------------------------------------------------------ 2-5. external suppliers
    def worldbank(self) -> list[dict]:
        rows = self.db.sql(f"""
            select Supplier, any_value("Supplier Country Code") cc, mode("Commodity Category") cat
            from read_csv('{C.RAW / 'worldbank_awards/corporate-procurement-contract-awards.csv'}', header=true)
            where Supplier is not null group by 1
            order by hash(Supplier || '{C.SEED}') limit {C.N_WORLDBANK}""").fetchall()
        out = []
        for i, (name, cc, cat) in enumerate(rows):
            out.append(dict(true_supplier_id=f"TS-WB-{i:05d}", canonical_name=name.strip(), country=cc or "US",
                            origin="worldbank", national_id_type="EIN" if cc == "US" else None,
                            national_id=T.fake_ein(self.rng) if cc == "US" else None,
                            category=wb_category(cat), real_identity=True, city=None, storyline=None))
        return out

    def poland(self) -> list[dict]:
        cat_case = " ".join(f"when '{k}' then '{v}'" for k, v in CPV2_CATEGORY.items())
        rows = self.db.sql(f"""
            with t as (
              select contractor_national_id nip, substr(cpv_code, 1, 2) cpv2
              from read_parquet(['{C.RAW / 'pl_tenders/tenders_2024.parquet'}', '{C.RAW / 'pl_tenders/tenders_2025.parquet'}'])
              where contractor_national_id is not null and contractor_name <> '[Osoba fizyczna]'),
            cat as (select nip, mode(case cpv2 {cat_case} end) category from t group by 1)
            select c.nip, c.name, c.city, cat.category
            from read_parquet('{C.RAW / 'pl_tenders/contractors.parquet'}') c join cat using (nip)
            where c.name <> '[Osoba fizyczna]' and c.nip not like 'anon%' and cat.category is not null
            order by hash(c.nip || '{C.SEED}')""").fetchall()
        # companies only: sole traders (jednoosobowa działalność) are private individuals
        company = re.compile(r"sp\.? ?z ?o\.? ?o|spółka|s\.a\.|\bsa\b|sp\. ?[jk]\.|spółdzielnia|przedsiębiorstwo państwowe", re.I)
        rows = [r for r in rows if T.nip_valid(r[0]) and company.search(r[1] or '')]
        food = [r for r in rows if r[3] == "DIRECT_INGREDIENTS"][: int(C.N_POLAND * 0.4)]
        other = [r for r in rows if r[3] != "DIRECT_INGREDIENTS"][: C.N_POLAND - len(food)]
        return [dict(true_supplier_id=f"TS-PL-{nip}", canonical_name=name.strip(), country="PL", origin="pl_tenders",
                     national_id_type="NIP", national_id=nip, category=cat, real_identity=True,
                     city=(city or "").title() or None, storyline=None)
                for nip, name, city, cat in food + other]

    def ukraine(self) -> list[dict]:
        cat_case = " ".join(f"when '{k}' then '{v}'" for k, v in CPV2_CATEGORY.items())
        rows = self.db.sql(f"""
            with s as (
              select supplier_code code, supplier_name sname, supplier_region region, substr(lot_cpv, 1, 2) cpv2
              from read_csv('{C.RAW / 'prozorro/Suppliers.csv'}', header=true, all_varchar=true)
              where length(supplier_code) = 8),
            g as (select code, mode(sname) sname, mode(region) region, mode(case cpv2 {cat_case} end) category,
                         count(*) wins from s group by 1)
            select code, sname, region, category from g
            where category is not null and wins >= 3
            order by hash(code || '{C.SEED}')""").fetchall()
        rows = [r for r in rows if T.edrpou_valid(r[0]) and T.has_cyrillic(r[1] or "")]
        food = [r for r in rows if r[3] == "DIRECT_INGREDIENTS"][: int(C.N_UKRAINE * 0.4)]
        other = [r for r in rows if r[3] != "DIRECT_INGREDIENTS"][: C.N_UKRAINE - len(food)]
        return [dict(true_supplier_id=f"TS-UA-{code}", canonical_name=name.strip(), country="UA", origin="prozorro",
                     national_id_type="EDRPOU", national_id=code, category=cat, real_identity=True,
                     city=region, storyline=None)
                for code, name, region, cat in food + other]

    def fictional(self) -> list[dict]:
        out, seen = [], set()
        sdn = self._sdn_entities()
        while len(out) < C.N_FICTIONAL:
            name = T.fictional_name(self.rng)
            if name in seen:
                continue
            seen.add(name)
            i = len(out)
            cc = self.rng.choice(["US", "US", "NL", "DE", "PL"])
            story = None
            if sdn and i < 6:
                # near-match to a real sanctioned entity: only ever on fictional suppliers (ADR 0003)
                base = sdn[i]
                name = T.typo(T.title(base), self.rng)
                story = f"sanctions_near_match:{base}"
            elif i < 6 + C.N_DRIFT_SUPPLIERS // 3:
                story = "delivery_drift"
            out.append(dict(true_supplier_id=f"TS-FIC-{i:04d}", canonical_name=name, country=cc, origin="fictional",
                            national_id_type="EIN" if cc == "US" else None,
                            national_id=T.fake_ein(self.rng) if cc == "US" else None,
                            category=self.rng.choice(C.INDIRECT_CATEGORIES), real_identity=False, city=None,
                            storyline=story))
        for s in out:
            if s["storyline"] and s["storyline"].startswith("sanctions"):
                self.defect("sanctions_near_match", "universe", "supplier", s["true_supplier_id"], "name",
                            s["storyline"].split(":", 1)[1], s["canonical_name"])
        return out

    def _sdn_entities(self) -> list[str]:
        f = C.RAW / "ofac/sdn.csv"
        if not f.exists():
            return []
        rows = self.db.sql(f"""
            select column01 from read_csv('{f}', header=false, all_varchar=true, quote='"', null_padding=true)
            where column02 = '-0- ' or column02 = '-0-'
            order by hash(column01 || '{C.SEED}') limit 50""").fetchall()
        return [r[0].strip() for r in rows if r[0] and 8 < len(r[0]) < 40][:6]

    # ------------------------------------------------------------ placement
    def place_sap(self, s: dict, bukrs: str) -> str:
        rng = self.rng
        kind = {"pl_tenders": "PL", "prozorro": "UA", "worldbank": "WB", "fictional": "FIC"}[s["origin"]]
        lifnr = self.new_lifnr(kind)
        name, methods = s["canonical_name"], []
        if s["origin"] in ("pl_tenders", "worldbank") and rng.random() < 0.35:
            name, methods = name.upper(), ["uppercase"]
        name1, name2 = T.sap_truncate(name), name[35:].strip() or None
        if name2:
            methods.append("sap_35char_split")
        nid = self.maybe_bad_id("sap", lifnr, s["national_id_type"] or "", s["national_id"], 0.06, 0.04)
        erdat = _rnd_date(rng, date(2014, 1, 1), date(2023, 6, 30))
        row = dict(lifnr=lifnr, bukrs=bukrs, name1=name1, name2=name2, land1=s["country"], ort01=s["city"],
                   pstlz=None, stras=None, stcd1=nid, ktokk="KRED", erdat=erdat, spras="E")
        self.sap_new.append(row)
        self.record("sap", lifnr, s["true_supplier_id"], name1 + (" " + name2 if name2 else ""), methods)
        if rng.random() < 0.03:  # duplicate vendor created years later by another buyer
            dup = dict(row, lifnr=self.new_lifnr("DUP"), erdat=min(erdat + timedelta(days=rng.randint(300, 1100)),
                                                                     date(2026, 6, 30)))
            dname, dm = T.noisy(name, rng, p_typo=0.3, p_abbrev=0.5, p_depunct=0.6)
            dup["name1"], dup["name2"] = T.sap_truncate(dname), dname[35:].strip() or None
            dup["stcd1"] = None if rng.random() < 0.5 else dup["stcd1"]
            self.sap_new.append(dup)
            self.record("sap", dup["lifnr"], s["true_supplier_id"], dname, dm, dup=True)
            self.defect("within_system_duplicate", "sap", "supplier", dup["lifnr"], None, lifnr, dup["lifnr"])
        return lifnr

    def place_coupa(self, s: dict) -> None:
        rng = self.rng
        core, form = T.split_legal_form(T.strip_quotes(s["canonical_name"]))
        core_l, m1 = T.latinize(core, rng)
        methods = [m1] if m1 != "none" else []
        english = s["origin"] in ("prozorro", "pl_tenders") and rng.random() < 0.6
        name = T.with_legal_form(T.title(core_l) if rng.random() < 0.6 else core_l, form, rng, english=english,
                                 latin=True)
        if english:
            methods.append("legal_form_english")
        name, m2 = T.noisy(name, rng)
        methods += m2
        if T.has_cyrillic(name):  # legal-form surface may have been Cyrillic; group system is Latin-only
            name, _ = T.latinize(name, rng)
        sid = self._coupa_next
        self._coupa_next += 1
        created = datetime.combine(_rnd_date(rng, date(2019, 1, 1), date(2024, 12, 31)), datetime.min.time())
        nid = s["national_id"] if rng.random() < 0.6 else None
        row = dict(id=sid, number=f"SUP-{sid:06d}", name=name, display_name=name, tax_id=nid,
                   country=s["country"], city=T.latinize(s["city"], rng)[0] if s["city"] else None,
                   category=s["category"], status="active", created_at=created,
                   updated_at=created + timedelta(days=rng.randint(0, 600)))
        self.coupa.append(row)
        self.record("coupa", str(sid), s["true_supplier_id"], name, methods)
        if rng.random() < 0.02:
            did = self._coupa_next
            self._coupa_next += 1
            dname, dm = T.noisy(name, rng, p_typo=0.4, p_abbrev=0.5)
            self.coupa.append(dict(row, id=did, number=f"SUP-{did:06d}", name=dname, display_name=dname,
                                   tax_id=None, created_at=row["created_at"] + timedelta(days=200),
                                   updated_at=row["updated_at"] + timedelta(days=200)))
            self.record("coupa", str(did), s["true_supplier_id"], dname, dm, dup=True)
            self.defect("within_system_duplicate", "coupa", "supplier", str(did), None, sid, did)

    def place_hicx(self, s: dict) -> None:
        rng = self.rng
        hid = f"HX-{self._hicx_next}"
        self._hicx_next += 1
        local = s["canonical_name"]
        latin = local
        if T.has_cyrillic(local):
            core, form = T.split_legal_form(T.strip_quotes(local))
            latin = f"{T.translit_ua(core, 'official').upper()}, {T.LEGAL_FORMS[form][3].upper() if form else ''}".rstrip(", ")
        nid = self.maybe_bad_id("hicx", hid, s["national_id_type"] or "", s["national_id"], 0.02, 0.01)
        city = s["city"]
        if city and rng.random() < 0.04:
            new_city = T.typo(city, rng) if rng.random() < 0.5 else (city.upper() if city != city.upper() else city.title())
            self.defect("conflicting_address", "hicx", "supplier", hid, "city", city, new_city)
            city = new_city
        onboarded = _rnd_date(rng, date(2018, 1, 1), date(2025, 12, 31))
        self.hicx.append(dict(hicx_id=hid, legal_name=latin, legal_name_local=local if local != latin else None,
                              national_id=nid, national_id_type=s["national_id_type"], country=s["country"],
                              city=city, category=s["category"], status="approved" if rng.random() < 0.93 else "pending",
                              onboarding_date=onboarded, iban=T.fake_iban(s["country"], rng)))
        self.record("hicx", hid, s["true_supplier_id"], latin, ["hicx_legal_name"])

    # ------------------------------------------------------------ run
    def run(self) -> None:
        rng = self.rng
        self.sap_demo()
        demo_by_id = {s["true_supplier_id"]: s for s in self.suppliers}
        # direct SAP vendors also appear in group systems (contracts in Coupa, onboarding in HICX)
        for d in self.sap_direct:
            s = demo_by_id[d["true_supplier_id"]]
            if rng.random() < 0.3:
                self.place_coupa(s)
            if rng.random() < 0.5:
                self.place_hicx(s)
        drift_pool = [d for d in self.sap_direct if d["rank"] <= 25]
        for d in rng.sample(drift_pool, C.N_DRIFT_SUPPLIERS - C.N_DRIFT_SUPPLIERS // 3):
            demo_by_id[d["true_supplier_id"]]["storyline"] = "delivery_drift"

        externals = self.worldbank() + self.poland() + self.ukraine() + self.fictional()
        for s in externals:
            self.suppliers.append(s)
            o = s["origin"]
            if o == "pl_tenders":
                self.place_sap(s, "PL01")
                p_coupa, p_hicx = 0.45, 0.55
            elif o == "prozorro":
                self.place_sap(s, "UA01")
                p_coupa, p_hicx = 0.45, 0.5
            elif o == "worldbank":
                if rng.random() < 0.4:
                    self.place_sap(s, "US01" if s["country"] == "US" else "NL01")
                p_coupa, p_hicx = 1.0, 0.6
            else:
                if rng.random() < 0.5:
                    self.place_sap(s, "US01" if s["country"] == "US" else "NL01")
                p_coupa, p_hicx = 1.0, 0.7
            if rng.random() < p_coupa:
                self.place_coupa(s)
            if rng.random() < p_hicx:
                self.place_hicx(s)
        for s in self.suppliers:
            if s["storyline"] == "delivery_drift":
                self.defect("delivery_drift", "universe", "supplier", s["true_supplier_id"], "on_time_rate", None, None,
                            f"on-time rate degrades over the last {C.DRIFT_WINDOW_DAYS} days")
        self.write()

    def write(self) -> None:
        C.STAGE.mkdir(parents=True, exist_ok=True)
        for name, rows in [("gt_supplier", self.suppliers), ("gt_source_record", self.records),
                           ("gt_defect", self.defects), ("sys_sap_vendor", self.sap_new),
                           ("sys_sap_direct", self.sap_direct), ("sys_coupa_supplier", self.coupa),
                           ("sys_hicx_supplier", self.hicx)]:
            write_parquet(self.db, rows, C.STAGE / f"{name}.parquet")
        summary = {
            "suppliers_by_origin": dict(self.db.sql(
                f"select origin, count(*) from '{C.STAGE / 'gt_supplier.parquet'}' group by 1").fetchall()),
            "records_by_system": dict(self.db.sql(
                f"select source_system, count(*) from '{C.STAGE / 'gt_source_record.parquet'}' group by 1").fetchall()),
            "defects_by_type": dict(self.db.sql(
                f"select defect_type, count(*) from '{C.STAGE / 'gt_defect.parquet'}' group by 1").fetchall()),
        }
        print(json.dumps(summary, indent=2))


def write_parquet(db: duckdb.DuckDBPyConnection, rows: list[dict], path) -> None:
    """Write a list of dicts to parquet via a temporary JSON file (keeps dependencies small)."""
    tmp = path.with_suffix(".jsonl")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    if rows:
        src = f"read_json('{tmp}', format='newline_delimited', sample_size=-1)"
        cols = db.sql(f"describe select * from {src}").fetchall()
        # columns that were null in every row come back as JSON; store them as VARCHAR
        sel = ", ".join(f'cast(null as varchar) as "{c[0]}"' if c[1] == "JSON" else f'"{c[0]}"' for c in cols)
        db.execute(f"copy (select {sel} from {src}) to '{path}' (format parquet)")
    tmp.unlink()


def build() -> None:
    Builder().run()


def refine_sap_identity() -> None:
    """Answer-key correction (no data changes): SAP demo vendors were grouped into true suppliers by NAME1 + city,
    which ignored NAME2. SAP training copies ("... Gr. 01", "... Gr. 02") are distinct vendors, but when the group
    label overflowed into NAME2 they were merged. Re-group SAP demo records by full name (NAME1 + NAME2) + city.

    Safe because the canonical (lowest) LIFNR of every old group is also the lowest of its new sub-group, so ids
    referenced elsewhere (Coupa/HICX placements, direct-spend vendors, storylines) stay valid."""
    db = duckdb.connect()
    rows = db.sql(f"""
        select lifnr, name1, name2, land1, ort01 from {SAP_CSV.format(f=C.RAW / 'sap/lfa1.csv')}
        qualify row_number() over (partition by lifnr order by mandt desc) = 1""").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(norm_key(r[1]) + norm_key(r[2]) + "|" + norm_key(r[4]), []).append(r)
    new_id, new_sup = {}, {}
    for ms in groups.values():
        ms.sort(key=lambda r: r[0])
        ts = f"TS-SAP-{ms[0][0]}"
        new_sup[ts] = ms[0]
        for m in ms:
            new_id[m[0]] = ts

    rel = db.sql(f"select * from '{C.STAGE / 'gt_source_record.parquet'}'")
    recs = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    changed = 0
    for r in recs:
        if r["source_system"] == "sap" and r["true_supplier_id"].startswith("TS-SAP-") and r["source_id"] in new_id:
            if r["true_supplier_id"] != new_id[r["source_id"]]:
                r["true_supplier_id"] = new_id[r["source_id"]]
                changed += 1
    rel = db.sql(f"select * from '{C.STAGE / 'gt_supplier.parquet'}'")
    sups = [dict(zip(rel.columns, r)) for r in rel.fetchall()]
    have = {s["true_supplier_id"] for s in sups}
    used = {r["true_supplier_id"] for r in recs}
    missing = [ts for ts in new_sup if ts not in have]
    for ts in missing:
        m = new_sup[ts]
        sups.append(dict(true_supplier_id=ts, canonical_name=m[1], country=m[3], origin="sap_demo",
                         national_id_type=None, national_id=None, category=None, real_identity=False, city=m[4],
                         storyline=None))
    sups = [s for s in sups if s["true_supplier_id"] in used]
    write_parquet(db, recs, C.STAGE / "gt_source_record.parquet")
    write_parquet(db, sups, C.STAGE / "gt_supplier.parquet")
    print(f"gt-refine: {changed} SAP records re-assigned, {len(missing)} true suppliers added, "
          f"{len(sups)} true suppliers in total")

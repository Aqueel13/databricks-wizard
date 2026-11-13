"""Sanctions screening with the entity-resolution normalization (ADR 0004: one matching stack, two uses).

Every golden supplier name (Latin and local script) is compared with every OFAC SDN name and alias that shares
a 3-character skeleton prefix; hits above the threshold are returned with their score as ALERTS for compliance
review (an alert is not a finding: a name match alone never establishes that a supplier is sanctioned).
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from .normalize import normalize_name

THRESHOLD = 0.92
# A country conflict (the supplier is registered in none of the countries on the entity's OFAC addresses) clears
# an alert automatically only when the name is not a near-exact match. Near-exact names stay open whatever the
# country: front companies are often registered abroad.
CLEAR_BELOW = 0.97
OFAC_COUNTRIES = json.loads((Path(__file__).parent / "ofac_countries.json").read_text())


def sdn_countries(addresses: pd.DataFrame) -> dict[int, list[str]]:
    """addresses: OFAC add.csv rows with ent_num and country (as written by OFAC) -> ISO codes per entity."""
    out: dict[int, set[str]] = {}
    for ent, country in zip(addresses["ent_num"], addresses["country"], strict=True):
        iso = OFAC_COUNTRIES.get(str(country or "").strip())
        if iso:
            out.setdefault(int(ent), set()).add(iso)
    return {k: sorted(v) for k, v in out.items()}


def corroborate(hits: pd.DataFrame, supplier_country: dict[str, str], countries: dict[int, list[str]],
                clear_below: float = CLEAR_BELOW) -> pd.DataFrame:
    """Adds sdn_countries, country_check (match / conflict / unknown) and rule_status: 'auto_cleared' for a country
    conflict below clear_below, else 'open'. Stewards' verdicts are applied on top by the caller."""
    h = hits.copy()
    h["sdn_countries"] = [",".join(countries.get(int(e), [])) or None for e in h["ent_num"]]

    def check(sid, ent):
        cs, sc = countries.get(int(ent)), supplier_country.get(sid)
        return "unknown" if not cs or not sc else ("match" if sc in cs else "conflict")
    h["country_check"] = [check(s, e) for s, e in zip(h["supplier_id"], h["ent_num"], strict=True)]
    h["rule_status"] = ["auto_cleared" if c == "conflict" and sc < clear_below else "open"
                        for c, sc in zip(h["country_check"], h["score"], strict=True)]
    return h


def screen(golden: pd.DataFrame, sanctions: pd.DataFrame, threshold: float = THRESHOLD) -> pd.DataFrame:
    """golden: supplier_id, legal_name, legal_name_local, country
    sanctions: ent_num, name, name_type, program, sdn_type"""
    names = pd.concat([golden[["supplier_id", "legal_name"]].rename(columns={"legal_name": "screened_name"}),
                       golden[["supplier_id", "legal_name_local"]].dropna().rename(
                           columns={"legal_name_local": "screened_name"})]).dropna().drop_duplicates()
    norm = names["screened_name"].map(normalize_name)
    names["skel"] = norm.map(lambda x: x["skeleton"])
    # Only distinctive names can alert on name similarity alone: at least two meaningful tokens, or a long single
    # token. One-word generic names ("Titan", "Avtodom") need more evidence (country, address, ID) to screen.
    names["distinctive"] = norm.map(lambda x: len(x["name_core"].split()) >= 2 or len(x["skeleton"]) >= 8)
    s = sanctions.dropna(subset=["name"]).copy()
    s["skel"] = s["name"].map(lambda n: normalize_name(n)["skeleton"])
    names, s = names[names["distinctive"] & (names["skel"].str.len() >= 5)], s[s["skel"].str.len() >= 5]
    con = duckdb.connect()
    con.register("n", names)
    con.register("s", s)
    hits = con.sql(f"""
        select n.supplier_id, n.screened_name, s.ent_num, s.name as sanctioned_name, s.name_type, s.program, s.sdn_type,
               round(jaro_winkler_similarity(n.skel, s.skel), 4) as score
        from n join s on substr(n.skel, 1, 3) = substr(s.skel, 1, 3)
        where jaro_winkler_similarity(n.skel, s.skel) >= {threshold}
        qualify row_number() over (partition by n.supplier_id, s.ent_num order by jaro_winkler_similarity(n.skel, s.skel) desc) = 1
    """).df()
    return hits.sort_values("score", ascending=False)

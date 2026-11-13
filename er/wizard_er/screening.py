"""Sanctions screening with the entity-resolution normalization (ADR 0004: one matching stack, two uses).

Every golden supplier name (Latin and local script) is compared with every OFAC SDN name and alias that shares
a 3-character skeleton prefix; hits above the threshold are returned with their score as ALERTS for compliance
review (an alert is not a finding: a name match alone never establishes that a supplier is sanctioned).
"""
from __future__ import annotations

import duckdb
import pandas as pd

from .normalize import normalize_name

THRESHOLD = 0.92


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

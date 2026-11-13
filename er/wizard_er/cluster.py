"""Link decisions and clustering (ADR 0004, layers 1, 3-5).

decide()    turns scored pairs into accepted links, each with the method that justified it:
              steward > deterministic (equal valid national ID) > exact_name (same normalized name and country,
              nothing contradicting) > probabilistic (p >= t_auto) > llm (t_llm <= p < t_auto)
            pairs in the LLM band that the model is unsure about go to the steward review queue.
cluster()   connected components over accepted links (union-find).
"""
from __future__ import annotations

import hashlib

import pandas as pd

T_AUTO = 0.95
T_LLM = 0.50


def _plain(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Columns as plain Python objects with None for missing (pandas NA can't be used in truth tests)."""
    x = df[["unique_id", *cols]].astype(object)
    return x.where(x.notna(), None)


def decide(pairs: pd.DataFrame, df: pd.DataFrame, llm: pd.DataFrame | None = None,
           steward: pd.DataFrame | None = None, t_auto: float = T_AUTO, t_llm: float = T_LLM) -> pd.DataFrame:
    plain = _plain(df, ["nid", "name_core", "country", "city_norm", "jurisdiction"])
    nid = dict(zip(plain["unique_id"], plain["nid"]))
    rec = plain.set_index("unique_id")[["name_core", "country", "city_norm", "jurisdiction"]].to_dict("index")
    p = pairs.copy()
    p["pair_key"] = [f"{min(a, b)}|{max(a, b)}" for a, b in zip(p["unique_id_l"], p["unique_id_r"])]
    p["same_valid_id"] = [nid.get(a) is not None and nid.get(a) == nid.get(b)
                          for a, b in zip(p["unique_id_l"], p["unique_id_r"])]

    def exact_name(a, b):
        """Same normalized name, same country, nothing contradicting it (city, valid national IDs)."""
        x, y = rec[a], rec[b]
        if not x["name_core"] or x["name_core"] != y["name_core"] or len(x["name_core"]) < 4:
            return False
        if x["country"] != y["country"]:
            return False
        if x["city_norm"] and y["city_norm"] and x["city_norm"] != y["city_norm"]:
            return False
        if x["jurisdiction"] and y["jurisdiction"] and x["jurisdiction"] != y["jurisdiction"]:
            return False
        return not (nid.get(a) and nid.get(b) and nid.get(a) != nid.get(b))

    p["exact_name"] = [exact_name(a, b) for a, b in zip(p["unique_id_l"], p["unique_id_r"])]
    verdict = dict(zip(llm["pair_key"], llm["verdict"])) if llm is not None and len(llm) else {}
    manual = dict(zip(steward["pair_key"], steward["decision"])) if steward is not None and len(steward) else {}

    def one(r):
        if r.pair_key in manual:
            return ("steward", manual[r.pair_key] == "match")
        if r.same_valid_id:
            return ("deterministic", True)
        if r.exact_name:
            return ("exact_name", True)
        if r.match_probability >= t_auto:
            return ("probabilistic", True)
        if r.match_probability >= t_llm:
            v = verdict.get(r.pair_key)
            if v is None:
                return ("llm_pending", False)
            if v == "unsure":
                return ("review", False)
            return ("llm", v == "match")
        return ("below_threshold", False)

    res = [one(r) for r in p.itertuples()]
    p["method"] = [m for m, _ in res]
    p["linked"] = [x for _, x in res]
    return p


METHOD_RANK = {"steward": 0, "deterministic": 1, "exact_name": 2, "probabilistic": 3, "llm": 4}


def jaro_winkler(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    l1, l2 = len(s1), len(s2)
    if not l1 or not l2:
        return 0.0
    win = max(l1, l2) // 2 - 1
    m1, m2 = [False] * l1, [False] * l2
    m = 0
    for i, c in enumerate(s1):
        for j in range(max(0, i - win), min(l2, i + win + 1)):
            if not m2[j] and s2[j] == c:
                m1[i] = m2[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t, k = 0, 0
    for i in range(l1):
        if m1[i]:
            while not m2[k]:
                k += 1
            t += s1[i] != s2[k]
            k += 1
    jaro = (m / l1 + m / l2 + (m - t / 2) / m) / 3
    prefix = 0
    for a, b in zip(s1[:4], s2[:4]):
        if a != b:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def cluster(unique_ids, links: pd.DataFrame, df: pd.DataFrame) -> tuple[dict[str, str], pd.DataFrame]:
    """Union-find with cluster-level constraints. Links are applied strongest first; a link that would put
    conflicting facts into one cluster (two different valid national IDs, clearly different cities in the same
    country, or two different legal jurisdictions) is refused and returned for steward review."""
    parent = {u: u for u in unique_ids}
    info = _plain(df, ["nid", "city_norm", "jurisdiction"]).set_index("unique_id").to_dict("index")
    facts = {u: {"nid": {info[u]["nid"]} - {None}, "city": {info[u]["city_norm"]} - {None, ""},
                 "juris": {info[u]["jurisdiction"]} - {None}} for u in unique_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def conflict(fa, fb, same_street: bool = False) -> str | None:
        if fa["nid"] and fb["nid"] and not (fa["nid"] & fb["nid"]):
            return "different national IDs"
        if fa["juris"] and fb["juris"] and not (fa["juris"] & fb["juris"]):
            return "different legal jurisdictions"
        # the same street address (numbers + street) outweighs a city mismatch, which is often a parsing artefact
        # ("New Delhi, West Delhi" vs "Delhi"); IDs and jurisdictions above still block
        if same_street:
            return None
        if fa["city"] and fb["city"] and max(jaro_winkler(x, y) for x in fa["city"] for y in fb["city"]) < 0.85:
            return "different cities"
        return None

    refused = []

    def refuse(r, reason: str) -> None:
        refused.append({"pair_key": r.pair_key, "unique_id_l": r.unique_id_l, "unique_id_r": r.unique_id_r,
                        "method": r.method, "match_probability": r.match_probability, "reason": reason})

    def union(r) -> None:
        ra, rb = find(r.unique_id_l), find(r.unique_id_r)
        if ra == rb:
            return
        same_street = getattr(r, "gamma_address", None) == 2   # "same address numbers, similar street"
        why = None if r.method == "steward" else conflict(facts[ra], facts[rb], same_street)
        if why:
            refuse(r, why)
            return
        keep, drop = min(ra, rb), max(ra, rb)
        parent[drop] = keep
        for k in ("nid", "city", "juris"):
            facts[keep][k] |= facts[drop][k]

    order = links.assign(_r=links["method"].map(METHOD_RANK).fillna(9)).sort_values(
        ["_r", "match_probability"], ascending=[True, False])
    # pass 1: steward, deterministic, exact-name and probabilistic links
    for r in order[order["method"] != "llm"].itertuples():
        union(r)
    # pass 2: LLM links. An LLM "match" is evidence about one pair. If it matches one record to several clusters
    # that the stronger evidence kept apart ("Acme Ltd" vs "Acme Ltd Gr. 01", "Acme Ltd Gr. 02", ...), that is
    # ambiguity, not identity: those links go to review instead of chaining the clusters together.
    llm = order[order["method"] == "llm"]
    targets: dict[str, set] = {}
    for r in llm.itertuples():
        targets.setdefault(r.unique_id_l, set()).add(find(r.unique_id_r))
        targets.setdefault(r.unique_id_r, set()).add(find(r.unique_id_l))
    for r in llm.itertuples():
        if len(targets[r.unique_id_l]) > 1 or len(targets[r.unique_id_r]) > 1:
            refuse(r, "LLM matched a record to several distinct clusters")
        else:
            union(r)
    members: dict[str, list] = {}
    for u in unique_ids:
        members.setdefault(find(u), []).append(u)
    out = {}
    for ms in members.values():
        # stable id: hash of the smallest member key, so ids survive re-runs while clusters don't change
        sid = "SUP-" + hashlib.sha1(min(ms).encode()).hexdigest()[:10].upper()
        for m in ms:
            out[m] = sid
    return out, pd.DataFrame(refused)

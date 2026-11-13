"""Candidate generation and probabilistic scoring (ADR 0004, layers 1-2).

prepare()  pandas records (+ embeddings) -> matching frame with normalized fields and embedding neighbours
score()    Splink (DuckDB backend) Fellegi-Sunter model, trained unsupervised (EM), returns scored pairs
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import splink.comparison_level_library as cll
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from splink.blocking_rule_library import CustomRule

from .normalize import normalize_address, normalize_city, normalize_id, normalize_name

NN_K = 6
NN_MIN_COS = 0.80


def prepare(records: pd.DataFrame, embeddings: pd.DataFrame) -> pd.DataFrame:
    """records: source, source_id, name, name_local, country, city, national_id, national_id_type, national_id_valid
    embeddings: source, source_id, name, embedding (one row per embedded name)."""
    df = records.copy()
    df["unique_id"] = df["source"] + ":" + df["source_id"].astype(str)
    norm = df["name"].map(normalize_name).apply(pd.Series)
    df = pd.concat([df, norm], axis=1)
    local = df["name_local"].fillna("").map(lambda n: normalize_name(n)["skeleton"] if n else None)
    df["skeleton_local"] = local
    df["nid_any"] = [normalize_id(t, v) for t, v in zip(df["national_id_type"], df["national_id"])]
    # national_id_valid arrives as a numpy/pandas boolean (or NA): compare by value, not identity
    df["nid"] = [n if (ok is not None and not pd.isna(ok) and bool(ok)) else None
                 for n, ok in zip(df["nid_any"], df["national_id_valid"])]
    df["city_norm"] = df["city"].map(normalize_city)
    # street address (optional input): numbers + distinctive words, for data without registration IDs
    addr = (df["address"] if "address" in df else pd.Series([None] * len(df), index=df.index))
    df = pd.concat([df, addr.map(normalize_address).apply(pd.Series)], axis=1)
    for c in ("name_core", "skeleton", "name_sorted", "first_token", "city_norm", "legal_form", "country"):
        df[c] = df[c].replace("", None)
    # explicit string dtype: an all-null column (e.g. no local-script names in a dataset) would otherwise be
    # inferred as INTEGER by DuckDB and break the string comparisons
    for c in ("name_core", "skeleton", "skeleton_local", "name_sorted", "first_token", "city_norm", "legal_form",
              "jurisdiction", "name_numbers", "country", "nid", "nid_any", "cskel", "addr_numbers", "addr_skel"):
        df[c] = df[c].astype("string")

    # embedding of the name in its original script (local name when the record has one)
    emb = embeddings.copy()
    emb["unique_id"] = emb["source"] + ":" + emb["source_id"].astype(str)
    local_names = dict(zip(df["unique_id"], df["name_local"].fillna(df["name"])))
    emb["is_local"] = [local_names.get(u) == n for u, n in zip(emb["unique_id"], emb["name"])]
    emb = emb.sort_values("is_local", ascending=False).drop_duplicates("unique_id")
    df = df.merge(emb[["unique_id", "embedding"]], on="unique_id", how="left")

    # nearest neighbours by cosine similarity (catches translated names that share no characters)
    has = df["embedding"].notna()
    ids = df.loc[has, "unique_id"].to_numpy()
    m = np.vstack(df.loc[has, "embedding"].map(np.asarray).to_numpy()).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    sims = m @ m.T
    np.fill_diagonal(sims, -1)
    top = np.argpartition(-sims, NN_K, axis=1)[:, :NN_K]
    nn = {ids[i]: [ids[j] for j in top[i] if sims[i, j] >= NN_MIN_COS] for i in range(len(ids))}
    nn90 = {ids[i]: [ids[j] for j in top[i] if sims[i, j] >= 0.90] for i in range(len(ids))}
    df["nn"] = df["unique_id"].map(lambda u: nn.get(u, []))
    df["nn90"] = df["unique_id"].map(lambda u: nn90.get(u, []))
    # embeddings stay out of Splink: neighbour lists carry the similarity signal at a fraction of the memory
    return df.drop(columns=["embedding"])


def _settings(indic: bool = False, addresses: bool = False) -> SettingsCreator:
    """indic / addresses switch on the comparisons that only make sense when the data has Indic-script names or
    street addresses, so the supplier model (neither) is unchanged by their existence."""
    nn90 = "(list_contains(nn90_l, unique_id_r) OR list_contains(nn90_r, unique_id_l))"
    nn80 = "(list_contains(nn_l, unique_id_r) OR list_contains(nn_r, unique_id_l))"
    jw_skel = "jaro_winkler_similarity(skeleton_l, skeleton_r)"
    name = cl.CustomComparison(
        output_column_name="name",
        comparison_levels=[
            cll.NullLevel("name_core"),
            cll.ExactMatchLevel("name_core"),
            cll.ExactMatchLevel("skeleton"),
            # written with list_contains so Splink doesn't mistake it for a same-column exact-match level
            cll.CustomLevel("list_contains([skeleton_r], skeleton_local_l) OR list_contains([skeleton_l], skeleton_local_r)",
                            "local-script skeleton equal"),
            cll.CustomLevel(f"{jw_skel} >= 0.95", "skeleton JW >= 0.95"),
            cll.CustomLevel("jaro_winkler_similarity(name_sorted_l, name_sorted_r) >= 0.92", "sorted tokens JW >= 0.92"),
            cll.CustomLevel(nn90, "embedding neighbours, cos >= 0.90"),
            *([cll.CustomLevel("(has_indic_l OR has_indic_r) AND jaro_winkler_similarity(cskel_l, cskel_r) >= 0.92",
                               "Indic script: consonant skeleton JW >= 0.92")] if indic else []),
            cll.CustomLevel(f"{jw_skel} >= 0.85", "skeleton JW >= 0.85"),
            cll.CustomLevel(nn80, "embedding neighbours, cos >= 0.80"),
            cll.ElseLevel(),
        ])
    nid = cl.CustomComparison(
        output_column_name="national_id",
        comparison_levels=[
            cll.NullLevel("nid_any"),
            cll.ExactMatchLevel("nid", "valid ID equal"),
            cll.ExactMatchLevel("nid_any", "ID equal (unvalidated)"),
            cll.CustomLevel("levenshtein(nid_any_l, nid_any_r) <= 1", "ID one character apart"),
            cll.ElseLevel(),
        ])
    city = cl.CustomComparison(
        output_column_name="city",
        comparison_levels=[cll.NullLevel("city_norm"), cll.ExactMatchLevel("city_norm"),
                           cll.CustomLevel("jaro_winkler_similarity(city_norm_l, city_norm_r) >= 0.88", "city JW >= 0.88"),
                           cll.ElseLevel()])
    # Expert prior (EM can't learn it: SAP training copies make "same name, different number" look like a match):
    # for true matches, numbers in the name differ only through typos (~2%).
    numbers = cl.CustomComparison(
        output_column_name="name_numbers",
        comparison_levels=[cll.NullLevel("name_numbers"), cll.ExactMatchLevel("name_numbers"),
                           cll.ElseLevel().configure(m_probability=0.02, fix_m_probability=True)])
    address = cl.CustomComparison(
        output_column_name="address",
        comparison_levels=[
            cll.NullLevel("addr_numbers"),
            cll.CustomLevel("addr_numbers_l = addr_numbers_r AND jaro_winkler_similarity(addr_skel_l, addr_skel_r) >= 0.85",
                            "same address numbers, similar street"),
            cll.ExactMatchLevel("addr_numbers", "same address numbers"),
            cll.ElseLevel()])
    extra_blocking = []
    if indic:
        extra_blocking.append(CustomRule("(l.has_indic OR r.has_indic) AND l.country = r.country "
                                         "AND substr(l.cskel, 1, 4) = substr(r.cskel, 1, 4)"))
    if addresses:
        extra_blocking.append(CustomRule("l.addr_numbers = r.addr_numbers AND length(l.addr_numbers) >= 3 "
                                         "AND l.country = r.country"))
    return SettingsCreator(
        link_type="dedupe_only",
        unique_id_column_name="unique_id",
        comparisons=[name, nid, cl.ExactMatch("country"), city, cl.ExactMatch("legal_form"),
                     numbers, *([address] if addresses else [])],
        blocking_rules_to_generate_predictions=[
            block_on("nid"),
            block_on("substr(nid_any, 1, length(nid_any) - 1)"),
            block_on("country", "substr(skeleton, 1, 4)"),
            block_on("first_token"),
            block_on("name_sorted"),
            CustomRule("l.skeleton_local = r.skeleton OR l.skeleton = r.skeleton_local"),
            CustomRule("list_contains(l.nn, r.unique_id) OR list_contains(r.nn, l.unique_id)"),
            *extra_blocking,
        ],
        retain_intermediate_calculation_columns=True,
        retain_matching_columns=True,
    )


MATCH_COLS = ["unique_id", "source", "source_id", "name", "name_local", "country", "city", "national_id",
              "name_core", "name_sorted", "skeleton", "skeleton_local", "first_token", "legal_form", "nid", "nid_any",
              "city_norm", "jurisdiction", "name_numbers", "nn", "nn90", "has_indic", "cskel", "addr_numbers",
              "addr_skel"]


def score(df: pd.DataFrame, threshold: float = 0.2) -> tuple[pd.DataFrame, dict]:
    """Train the model without labels and score candidate pairs. Returns (pairs, model json)."""
    indic, addresses = bool(df["has_indic"].any()), bool(df["addr_numbers"].notna().any())
    linker = Linker(df[MATCH_COLS], _settings(indic, addresses), db_api=DuckDBAPI())
    # Train on the strongest evidence the data has: national IDs when there are enough of them; otherwise
    # (e.g. sources without registration numbers) exact name + city.
    has_ids = df["nid"].notna().sum() >= 50
    deterministic = block_on("nid") if has_ids else block_on("name_core", "city_norm")
    linker.training.estimate_probability_two_random_records_match([deterministic], recall=0.7)
    linker.training.estimate_u_using_random_sampling(max_pairs=2e6, seed=42)
    linker.training.estimate_parameters_using_expectation_maximisation(
        deterministic if has_ids else block_on("first_token", "city_norm"))
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("country", "substr(skeleton, 1, 4)"))
    pairs = linker.inference.predict(threshold_match_probability=threshold).as_pandas_dataframe()
    keep = ["unique_id_l", "unique_id_r", "match_probability", "match_weight"] + \
           [c for c in pairs.columns if c.startswith("gamma_") or c.startswith("bf_")]
    return pairs[keep], linker.misc.save_model_to_json()

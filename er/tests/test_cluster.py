import pandas as pd
from wizard_er.cluster import cluster, decide, jaro_winkler


def records(*rows):
    cols = ["unique_id", "name_core", "country", "city_norm", "jurisdiction", "nid"]
    return pd.DataFrame(rows, columns=cols)


def pairs(*rows):
    return pd.DataFrame(rows, columns=["unique_id_l", "unique_id_r", "match_probability"])


def test_jaro_winkler_basics():
    assert jaro_winkler("martha", "marhta") > 0.95
    assert jaro_winkler("abc", "abc") == 1.0
    assert jaro_winkler("abc", "xyz") == 0.0


def test_decision_tiers():
    df = records(("a", "acme", "US", None, None, "EIN:1"), ("b", "acme", "US", None, None, "EIN:1"),
                 ("c", "beta", "US", None, None, None), ("d", "beta", "US", None, None, None),
                 ("e", "gamma", "US", None, None, None), ("f", "gamma x", "US", None, None, None))
    d = decide(pairs(("a", "b", 0.1), ("c", "d", 0.1), ("e", "f", 0.97)), df)
    assert list(d["method"]) == ["deterministic", "exact_name", "probabilistic"]
    assert d["linked"].all()


def test_exact_name_refused_on_contradiction():
    df = records(("a", "acme", "US", "chicago", None, None), ("b", "acme", "US", "boston", None, None))
    assert decide(pairs(("a", "b", 0.1)), df)["method"].iloc[0] == "below_threshold"


def test_llm_band_uses_verdicts_and_unsure_goes_to_review():
    df = records(("a", "x", "US", None, None, None), ("b", "y", "US", None, None, None),
                 ("c", "z", "US", None, None, None))
    llm = pd.DataFrame({"pair_key": ["a|b", "a|c"], "verdict": ["match", "unsure"]})
    d = decide(pairs(("a", "b", 0.7), ("a", "c", 0.7)), df, llm=llm)
    assert list(d["method"]) == ["llm", "review"] and list(d["linked"]) == [True, False]


def test_steward_overrides_everything():
    df = records(("a", "acme", "US", None, None, "EIN:1"), ("b", "acme", "US", None, None, "EIN:1"))
    d = decide(pairs(("a", "b", 0.99)), df, steward=pd.DataFrame({"pair_key": ["a|b"], "decision": ["no_match"]}))
    assert d["method"].iloc[0] == "steward" and not d["linked"].iloc[0]


def test_cluster_refuses_merges_that_join_conflicting_ids():
    # b has no ID and matches both a and c, whose valid IDs differ: only one merge may happen
    df = records(("a", "osm", "PL", None, None, "NIP:1"), ("b", "osm", "PL", None, None, None),
                 ("c", "osm", "PL", None, None, "NIP:2"))
    links = decide(pairs(("a", "b", 0.99), ("b", "c", 0.98)), df)
    assign, refused = cluster(df["unique_id"].tolist(), links[links["linked"]], df)
    assert assign["a"] != assign["c"]
    assert list(refused["reason"]) == ["different national IDs"]


def test_cluster_refuses_different_jurisdictions():
    df = records(("a", "brightwell", "NL", None, "DE", None), ("b", "brightwell", "NL", None, "NL", None))
    d = decide(pairs(("a", "b", 0.99)), df)
    assign, refused = cluster(["a", "b"], d[d["linked"]], df)
    assert assign["a"] != assign["b"] and refused["reason"].iloc[0] == "different legal jurisdictions"


def test_llm_ambiguity_guard():
    # the LLM matches one generic record to two clusters that stronger evidence kept apart
    df = records(("base", "sapsota", "US", None, None, None), ("g1", "sapsota gr 01", "US", None, None, None),
                 ("g2", "sapsota gr 02", "US", None, None, None))
    llm = pd.DataFrame({"pair_key": ["base|g1", "base|g2"], "verdict": ["match", "match"]})
    d = decide(pairs(("base", "g1", 0.8), ("base", "g2", 0.8)), df, llm=llm)
    assign, refused = cluster(df["unique_id"].tolist(), d[d["linked"]], df)
    assert len(set(assign.values())) == 3
    assert set(refused["reason"]) == {"LLM matched a record to several distinct clusters"}


def test_cluster_ids_are_stable():
    df = records(("a", "x", "US", None, None, None), ("b", "x", "US", None, None, None))
    d = decide(pairs(("a", "b", 0.99)), df)
    first, _ = cluster(["a", "b"], d[d["linked"]], df)
    second, _ = cluster(["b", "a"], d[d["linked"]], df)
    assert first == second and first["a"] == first["b"]

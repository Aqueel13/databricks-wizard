import pandas as pd
from wizard_er.screening import corroborate, sdn_countries


def test_sdn_countries_maps_ofac_names_to_iso():
    addr = pd.DataFrame({"ent_num": [1, 1, 2, 3], "country": ["Russia", "Cyprus", "Korea, North", "-0-"]})
    assert sdn_countries(addr) == {1: ["CY", "RU"], 2: ["KP"]}


def test_country_rule_clears_only_conflicting_non_exact_names():
    hits = pd.DataFrame({"supplier_id": ["a", "b", "c", "d"], "ent_num": [1, 1, 1, 9],
                         "score": [0.93, 0.99, 0.93, 0.93]})
    out = corroborate(hits, {"a": "US", "b": "US", "c": "RU", "d": "US"}, {1: ["CY", "RU"]})
    assert list(out["country_check"]) == ["conflict", "conflict", "match", "unknown"]
    # conflict + weak name -> cleared; near-exact name stays open despite the conflict; match / unknown stay open
    assert list(out["rule_status"]) == ["auto_cleared", "open", "open", "open"]
    assert out.loc[0, "sdn_countries"] == "CY,RU" and out.loc[3, "sdn_countries"] is None

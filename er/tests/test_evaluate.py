import pytest
from wizard_er.evaluate import pairwise, script


def test_script_classes():
    assert script("ТОВ Ромашка") == "cyrillic"
    assert script("Łódź Sp. z o.o.") == "polish"
    assert script("Acme") == "latin"


def test_pairwise_and_bcubed_on_a_known_case():
    truth = {"a": "T1", "b": "T1", "c": "T1", "d": "T2"}
    pred = {"a": "P1", "b": "P1", "c": "P2", "d": "P2"}   # c wrongly split off and merged with d
    names = {k: "x" for k in truth}
    res, fps, fns = pairwise(pred, truth, names)
    o = res["overall"]
    assert (o["tp"], o["fp"], o["fn"]) == (1, 1, 2)       # TP a-b; FP c-d; FN a-c, b-c
    assert fps == [("c", "d")] and fns == [("a", "c"), ("b", "c")]
    # B-cubed: precision per record a=1, b=1, c=1/2, d=1/2; recall a=2/3, b=2/3, c=1/3, d=1
    assert res["bcubed"]["precision"] == pytest.approx(0.75)
    assert res["bcubed"]["recall"] == pytest.approx(round((2/3 + 2/3 + 1/3 + 1) / 4, 4))

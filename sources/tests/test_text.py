import ast
import random
from pathlib import Path

import pytest
from wizard_sources import text as T

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("nip,ok", [("6760107416", True), ("6760107417", False), ("123", False)])
def test_nip(nip, ok):
    assert T.nip_valid(nip) is ok


@pytest.mark.parametrize("code,ok", [("36501757", True), ("36936719", True), ("36501758", False)])
def test_edrpou(code, ok):
    assert T.edrpou_valid(code) is ok


def test_corruption_always_breaks_checksums():
    rng = random.Random(1)
    for _ in range(50):
        assert not T.nip_valid(T.corrupt_check_digit("6760107416", rng))
        assert not T.edrpou_valid(T.corrupt_check_digit("36501757", rng))


def test_ein_format_defect_is_opt_in():
    rng = random.Random(1)
    assert len(T.corrupt_check_digit("12-3456789", rng)) == 10              # v1: still well-formed
    assert T.corrupt_check_digit("12-3456789", rng, ein_format=True) == "12-345678"


def test_transliteration_styles_differ_but_are_deterministic():
    assert T.translit_ua("Юг-Агрос") == "Yuh-Ahros"
    assert T.translit_ua("Юг-Агрос", "legacy") == "Yug-Agros"


def test_legal_form_split():
    assert T.split_legal_form("HAKO Sp. z o.o.") == ("HAKO", "PL_SPZOO")
    assert T.split_legal_form("ТОВ Медтехніка")[1] == "UA_TOV"


def test_fake_iban_is_mod97_valid():
    iban = T.fake_iban("PL", random.Random(3))
    num = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    assert int(num) % 97 == 1


def _silver_validator():
    """Load national_id_valid from pipelines/silver.py without importing Databricks (dlt, Spark)."""
    tree = ast.parse((ROOT / "pipelines" / "silver.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "national_id_valid")
    fn.decorator_list = []
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "silver.py", "exec"), ns)
    return ns["national_id_valid"]


def test_silver_validation_agrees_with_generator():
    v = _silver_validator()
    rng = random.Random(7)
    for good in ("6760107416", "6330005392", "6080108960"):
        assert v("NIP", good) is True and v("NIP", T.corrupt_check_digit(good, rng)) is False
    for good in ("36501757", "36936719", "38501628"):
        assert v("EDRPOU", good) is True and v("EDRPOU", T.corrupt_check_digit(good, rng)) is False
    assert v("EIN", "12-3456789") is True and v("EIN", "12-345678") is False
    assert v(None, "x") is None

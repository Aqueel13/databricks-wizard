from wizard_er.normalize import normalize_city, normalize_id, normalize_name


def test_cyrillic_and_latin_variants_share_a_skeleton():
    variants = ["ТОВ «Юг-Агрос»", "YUH-AHROS LLC", "YUH-AHROS, TOV", "Yug-Agros TOV"]
    assert len({normalize_name(v)["skeleton"] for v in variants}) == 1


def test_full_ukrainian_legal_phrase_is_stripped():
    r = normalize_name('Товариство з обмеженою відповідальністю "Херсонський завод кондитерських виробів"')
    assert r["legal_form"] == "LLC" and r["jurisdiction"] == "UA"
    assert r["skeleton"] == normalize_name("Khersonskyi zavod kondyterskykh vyrobiv LLC")["skeleton"]


def test_polish_legal_forms_and_diacritics():
    a, b = normalize_name("HAKO Sp. z o.o."), normalize_name("HAKO Sp z oo")
    assert a["name_core"] == b["name_core"] == "hako"
    assert a["jurisdiction"] == "PL"


def test_abbreviations_fold_together():
    assert normalize_name("International Business Machines Corp")["name_core"] == \
           normalize_name("Intl Business Machines")["name_core"]


def test_generic_forms_carry_no_jurisdiction_but_native_ones_do():
    assert normalize_name("Acme LLC")["jurisdiction"] is None
    assert normalize_name("Acme GmbH")["jurisdiction"] == "DE"
    assert normalize_name("Acme B.V.")["jurisdiction"] == "NL"


def test_numbers_in_names_are_captured():
    assert normalize_name("Elektroblitz GmbH Gr.01")["name_numbers"] == "1"
    assert normalize_name("Plant 1500")["name_numbers"] == "1500"
    assert normalize_name("Acme Ltd")["name_numbers"] is None


def test_soft_sign_does_not_split_words():
    assert " " not in normalize_name("Херсонський")["name_core"]


def test_ids_and_cities():
    assert normalize_id("NIP", "676-010-74-16") == "NIP:6760107416"
    assert normalize_id("NIP", None) is None
    assert normalize_city("Kyiv") == normalize_city("KYIV")


def test_mcp_app_copy_of_normalize_is_identical():
    """The MCP app ships its own copy (an app only receives its own folder); it must not drift from the ER one."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert (root / "apps/mcp/wizard_mcp/normalize.py").read_text() == (root / "er/wizard_er/normalize.py").read_text()

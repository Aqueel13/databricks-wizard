import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cuad_score import FIELDS, label_date, label_days, norm_law, response_format, score_field, summarize  # noqa: E402


def test_label_days():
    assert label_days("successive 1 year") == 365
    assert label_days("90 days") == 90
    assert label_days("6 months") == 180
    assert label_days(None) is None and label_days(float("nan")) is None
    assert label_days("perpetual") == "skip"


def test_label_date_unknown_parts():
    assert label_date("5/8/2014") == (5, 8, 14)
    assert label_date("[]/[]/2020") == (None, None, 20)
    assert label_date("3/20/20") == (3, 20, 20)


def test_dates():
    assert score_field("agreement_date", "5/8/2014", "2014-05-08")
    assert not score_field("agreement_date", "5/8/2014", "2014-08-05")
    assert score_field("agreement_date", "[]/[]/2020", "2020-01-01")
    assert score_field("agreement_date", None, None)
    assert not score_field("agreement_date", None, "2020-01-01")


def test_governing_law():
    assert norm_law("State of New York") == "new york"
    assert score_field("governing_law", "New York", "the laws of the State of New York, USA")
    assert score_field("governing_law", "Delaware", "Delaware")
    assert not score_field("governing_law", "Delaware", "California")
    assert not score_field("governing_law", None, "Texas")


def test_days_tolerance_and_skip():
    assert score_field("renewal_term_days", "6 months", 182)
    assert not score_field("renewal_term_days", "1 year", 30)
    assert score_field("renewal_term_days", "perpetual", 365) is None
    assert score_field("notice_to_terminate_renewal_days", None, None)


def test_yes_no_and_summary():
    assert score_field("audit_rights", "Yes", True) and score_field("audit_rights", "No", False)
    rows = [dict(model="m", field="audit_rights", ok=True, label_yes=True, pred_yes=True),
            dict(model="m", field="audit_rights", ok=False, label_yes=True, pred_yes=False),
            dict(model="m", field="insurance", ok=False, label_yes=False, pred_yes=True),
            dict(model="m", field="renewal_term_days", ok=None, label_yes=False, pred_yes=False)]
    s = summarize(rows)["m"]
    assert s["accuracy"] == 1 / 3 and s["yes_precision"] == 0.5 and s["yes_recall"] == 0.5


def test_schema_covers_every_field():
    schema = json.loads(response_format())["json_schema"]["schema"]
    assert set(schema["required"]) == set(FIELDS)

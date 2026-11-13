"""CUAD benchmark: field definitions and scoring (pure functions, no Databricks; tested in scripts/tests).

CUAD (Atticus Project, CC BY 4.0) has 510 real commercial contracts with expert answers for 41 categories. We use
the ones a procurement team acts on. Answers come from master_clauses.csv ("<Category>-Answer" columns).
"""
from __future__ import annotations

import json
import re

# CUAD category descriptions (CUAD_v1_README.txt), reworded only to name the JSON field.
YES_NO = {
    "anti_assignment": ("Anti-Assignment-Answer",
                        "Is consent or notice required of a party if the contract is assigned to a third party?"),
    "change_of_control": ("Change Of Control-Answer",
                          "Does one party have the right to terminate, or is consent or notice required of the "
                          "counterparty, if such party undergoes a change of control (merger, stock sale, transfer of "
                          "all or substantially all of its assets, assignment by operation of law)?"),
    "termination_for_convenience": ("Termination For Convenience-Answer",
                                    "Can a party terminate this contract without cause (solely by giving a notice "
                                    "and allowing a waiting period to expire)?"),
    "cap_on_liability": ("Cap On Liability-Answer",
                         "Does the contract cap liability upon the breach of a party's obligation? This includes a "
                         "time limit for the counterparty to bring claims or a maximum amount for recovery."),
    "audit_rights": ("Audit Rights-Answer",
                     "Does a party have the right to audit the books, records, or physical locations of the "
                     "counterparty to ensure compliance with the contract?"),
    "insurance": ("Insurance-Answer",
                  "Is there a requirement for insurance that must be maintained by one party for the benefit of the "
                  "counterparty?"),
    "minimum_commitment": ("Minimum Commitment-Answer",
                           "Is there a minimum order size or minimum amount or units per time period that one party "
                           "must buy from the counterparty?"),
    "warranty_duration": ("Warranty Duration-Answer",
                          "Does the contract state a duration for a warranty against defects or errors in the "
                          "technology, products or services provided?"),
}
VALUES = {
    "agreement_date": ("Agreement Date-Answer", "The date of the contract, as YYYY-MM-DD; null if not stated."),
    "governing_law": ("Governing Law-Answer",
                      "Which US state or country's law governs the contract (e.g. 'New York', 'England'); null if "
                      "the contract names none."),
    "renewal_term_days": ("Renewal Term-Answer",
                          "Length in days of each renewal after the initial term expires (automatic or unilateral "
                          "extensions); a year is 365 days, a month 30. Null if there is no renewal."),
    "notice_to_terminate_renewal_days": ("Notice Period To Terminate Renewal- Answer",
                                         "Notice in days required to stop a renewal; null if none is stated."),
}
FIELDS = list(VALUES) + list(YES_NO)

PROMPT = ("You are reviewing a commercial contract for a procurement team. Answer each field from the contract text "
          "only. Field definitions:\n" + "\n".join(f"- {k}: {d}" for k, (_, d) in {**VALUES, **YES_NO}.items()))


def response_format() -> str:
    s, i, b = {"type": ["string", "null"]}, {"type": ["integer", "null"]}, {"type": "boolean"}
    props = {"agreement_date": s, "governing_law": s, "renewal_term_days": i, "notice_to_terminate_renewal_days": i,
             **{k: b for k in YES_NO}}
    return json.dumps({"type": "json_schema", "json_schema": {"name": "cuad", "strict": True, "schema": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}})


# ------------------------------------------------------------------ label normalization
def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() in ("", "<NA>", "nan")


def label_days(v) -> int | None | str:
    """'successive 1 year' -> 365, '90 days' -> 90, '6 months' -> 180; blank -> None; anything else -> 'skip'."""
    if _blank(v):
        return None
    m = re.fullmatch(r"(?:successive\s+)?(\d+(?:\.\d+)?)\s*(day|month|year|week)s?", str(v).strip().lower())
    if not m:
        return "skip"  # perpetual, ranges, mixed answers: not scored
    n, unit = float(m.group(1)), m.group(2)
    return round(n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit])


def label_date(v) -> tuple | None | str:
    """CUAD dates are m/d/yy or m/d/yyyy with '[]' for unknown parts -> (month, day, yy) with None for unknown."""
    if _blank(v):
        return None
    m = re.fullmatch(r"(\d{1,2}|\[\])/(\d{1,2}|\[\])/(\d{2,4}|\[\])", str(v).strip())
    if not m:
        return "skip"
    p = [None if x == "[]" else int(x) for x in m.groups()]
    if p[2] is not None:
        p[2] %= 100
    return tuple(p)


_LAW_NOISE = re.compile(r"\b(the|state|commonwealth|province|of|laws?|united states|usa|u\.s\.a?\.?)\b")


def norm_law(v) -> str | None:
    if _blank(v):
        return None
    s = _LAW_NOISE.sub(" ", str(v).lower().replace(",", " "))
    return re.sub(r"[^a-z ]", "", re.sub(r"\s+", " ", s)).strip() or None


# ------------------------------------------------------------------ scoring
def score_field(field: str, label, pred) -> bool | None:
    """True/False = correct/wrong; None = not scored (label in a form we don't compare)."""
    if field in YES_NO:
        if _blank(label):
            return None
        return bool(pred) == (str(label).strip().lower() == "yes")
    if field == "agreement_date":
        lab = label_date(label)
        if lab == "skip":
            return None
        if lab is None or pred in (None, ""):
            return lab is None and pred in (None, "")
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(pred))
        if not m:
            return False
        got = (int(m.group(2)), int(m.group(3)), int(m.group(1)) % 100)
        return all(want is None or want == g for want, g in zip(lab, got))
    if field == "governing_law":
        a, b = norm_law(label), norm_law(pred)
        if a is None or b is None:
            return a is None and b is None
        return a == b or a in b or b in a
    if field in ("renewal_term_days", "notice_to_terminate_renewal_days"):
        lab = label_days(label)
        if lab == "skip":
            return None
        if lab is None or pred is None:
            return lab is None and pred is None
        return abs(int(pred) - lab) <= max(2, 0.03 * lab)  # 6 months = 180 vs 182/183
    raise KeyError(field)


def summarize(rows: list[dict]) -> dict:
    """rows: {model, field, ok, label_yes (yes/no fields), pred_yes}. Returns per model: accuracy per field, overall,
    and precision/recall of 'Yes' over the yes/no fields (a reviewer cares about missed clauses)."""
    out: dict = {}
    for r in rows:
        if r["ok"] is None:
            continue
        m = out.setdefault(r["model"], {"fields": {}, "tp": 0, "fp": 0, "fn": 0})
        f = m["fields"].setdefault(r["field"], [0, 0])
        f[0] += bool(r["ok"])
        f[1] += 1
        if r["field"] in YES_NO:
            m["tp"] += r["label_yes"] and r["pred_yes"]
            m["fp"] += (not r["label_yes"]) and r["pred_yes"]
            m["fn"] += r["label_yes"] and not r["pred_yes"]
    for m in out.values():
        right = sum(v[0] for v in m["fields"].values())
        total = sum(v[1] for v in m["fields"].values())
        m["accuracy"] = right / total if total else None
        m["fields"] = {k: v[0] / v[1] for k, v in m["fields"].items()}
        p = m["tp"] / (m["tp"] + m["fp"]) if m["tp"] + m["fp"] else None
        rc = m["tp"] / (m["tp"] + m["fn"]) if m["tp"] + m["fn"] else None
        m["yes_precision"], m["yes_recall"] = p, rc
    return out

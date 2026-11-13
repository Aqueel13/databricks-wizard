"""Shared definitions for document field extraction (used by the gold pipeline and the benchmark).

One JSON schema per document type. Every field is required but nullable, so a model cannot silently drop a field
(an early test showed a model omitting the certificate expiry date when only two fields were required).
"""
import json

COMMON = "Use ISO dates (YYYY-MM-DD). Copy names exactly as written. Use null only when the document does not state the value."

PROMPTS = {
    "certificate": "Extract the fields of this management-system certificate. The holder is the organisation that "
                   "was certified (not the certification body). " + COMMON,
    "contract": "Extract the fields of this agreement. The supplier is the party labelled \"Supplier\"; the customer "
                "is the party labelled \"Customer\". Payment terms and notice periods are numbers of days. " + COMMON,
}

_S, _I, _N = {"type": ["string", "null"]}, {"type": ["integer", "null"]}, {"type": ["number", "null"]}
FIELDS = {
    "certificate": {"holder": _S, "certificate_number": _S, "certificate_type": _S, "issuer": _S, "scope": _S,
                    "issue_date": _S, "expiry_date": _S},
    "contract": {"agreement_number": _S, "customer": _S, "supplier": _S, "agreement_type": _S, "start_date": _S,
                 "end_date": _S, "contract_value": _N, "currency": _S, "payment_terms_days": _I, "notice_days": _I,
                 "liability_cap": _S, "governing_law": _S},
}


def response_format(doc_type: str) -> str:
    return json.dumps({"type": "json_schema", "json_schema": {
        "name": doc_type, "strict": True,
        "schema": {"type": "object", "properties": FIELDS[doc_type], "required": list(FIELDS[doc_type]),
                   "additionalProperties": False}}})


def ai_query_sql(model: str, doc_type: str, text_col: str = "text") -> str:
    """SQL expression extracting fields from a parsed document of one type."""
    rf = response_format(doc_type).replace("'", "''")
    prompt = PROMPTS[doc_type].replace("'", "''")
    return f"ai_query('{model}', concat('{prompt}\\n\\n', {text_col}), responseFormat => '{rf}')"

"""Multilingual supplier-name normalization (ADR 0004, layer 0).

Every record keeps its original name and gains:
  name_latin   transliterated, lower-case, punctuation-free (unidecode; independent of the data generator)
  legal_form   canonical legal-form class found in the name (LLC, CORP, PARTNERSHIP, PE, SE) or None
  name_core    name_latin without legal forms and noise words, abbreviations folded
  name_sorted  name_core tokens in alphabetical order (word-order transpositions)
  skeleton     phonetic fold of name_core that collapses transliteration variants (kh/h, g/h, yu/iu/u, y/i, ...)
"""
from __future__ import annotations

import re
import unicodedata

from unidecode import unidecode

# Cyrillic legal forms are removed BEFORE transliteration (long phrases first).
UA_FORMS = [
    ("товариство з обмеженою відповідальністю", "LLC"), ("приватне акціонерне товариство", "CORP"),
    ("публічне акціонерне товариство", "CORP"), ("акціонерне товариство", "CORP"),
    ("приватне підприємство", "PE"), ("державне підприємство", "SE"), ("комунальне підприємство", "SE"),
    ("тзов", "LLC"), ("тов", "LLC"), ("прат", "CORP"), ("пат", "CORP"), ("ат", "CORP"), ("пп", "PE"),
    ("дп", "SE"), ("кп", "SE"),
]
# Latin legal forms, matched on the transliterated, punctuation-free text.
LATIN_FORMS = [
    ("spolka z ograniczona odpowiedzialnoscia", "LLC"), ("sp z o o", "LLC"), ("sp z oo", "LLC"),
    ("spolka akcyjna", "CORP"), ("spolka jawna", "PARTNERSHIP"), ("spolka komandytowa", "PARTNERSHIP"),
    ("sp j", "PARTNERSHIP"), ("sp k", "PARTNERSHIP"),
    ("gesellschaft mit beschrankter haftung", "LLC"), ("besloten vennootschap", "LLC"),
    ("private limited", "LLC"), ("pte ltd", "LLC"), ("private enterprise", "PE"), ("state enterprise", "SE"),
    ("private jsc", "CORP"), ("public jsc", "CORP"), ("aktiengesellschaft", "CORP"), ("incorporated", "CORP"),
    ("corporation", "CORP"), ("limited", "LLC"), ("l l c", "LLC"), ("l l p", "PARTNERSHIP"),
    ("llc", "LLC"), ("ltd", "LLC"), ("gmbh", "LLC"), ("bv", "LLC"), ("b v", "LLC"), ("tov", "LLC"),
    ("tzov", "LLC"), ("inc", "CORP"), ("corp", "CORP"), ("pjsc", "CORP"), ("jsc", "CORP"), ("prat", "CORP"),
    ("pat", "CORP"), ("sa", "CORP"), ("s a", "CORP"), ("ag", "CORP"), ("plc", "CORP"), ("kgaa", "PARTNERSHIP"),
    ("llp", "PARTNERSHIP"), ("lp", "PARTNERSHIP"), ("pp", "PE"), ("dp", "SE"), ("pte", "LLC"),
]
NOISE = {"the", "and", "of", "co", "company", "group"}
# Native legal forms that tie a name to one jurisdiction. Two different jurisdictions = different legal entities.
# Generic or translated forms (llc, ltd, inc, corp, jsc) carry no jurisdiction and never contradict.
JURISDICTION = {"gmbh": "DE", "gesellschaft mit beschrankter haftung": "DE", "ag": "DE", "aktiengesellschaft": "DE",
                "kgaa": "DE", "bv": "NL", "b v": "NL", "besloten vennootschap": "NL",
                "sp z o o": "PL", "sp z oo": "PL", "spolka z ograniczona odpowiedzialnoscia": "PL",
                "spolka akcyjna": "PL", "spolka jawna": "PL", "spolka komandytowa": "PL", "sp j": "PL", "sp k": "PL",
                "tov": "UA", "tzov": "UA", "prat": "UA", "pte ltd": "SG", "pte": "SG"}
UA_JURIS = {"тов", "тзов", "прат", "пат", "товариство з обмеженою відповідальністю", "приватне акціонерне товариство",
            "публічне акціонерне товариство"}
ABBREV = {  # fold long and short spellings to one token
    "international": "intl", "corporation": "corp", "services": "svcs", "service": "svc",
    "technologies": "tech", "technology": "tech", "solutions": "sol", "manufacturing": "mfg",
    "associates": "assoc", "management": "mgmt", "logistics": "log", "consulting": "cons",
    "industries": "ind", "systems": "sys", "university": "univ", "department": "dept",
}
SKELETON_RULES = [("shch", "s"), ("sch", "s"), ("kh", "h"), ("zh", "z"), ("ts", "c"), ("ch", "c"), ("sh", "s"),
                  ("yu", "u"), ("iu", "u"), ("ya", "a"), ("ia", "a"), ("ye", "e"), ("ie", "e"), ("yi", "i"),
                  ("g", "h"), ("y", "i"), ("j", "i"), ("w", "v"), ("q", "k"), ("x", "ks"), ("ph", "f")]
_QUOTES = re.compile(r"[\"'«»„“”‘’`]")
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _strip_forms(text: str, forms: list[tuple[str, str]]) -> tuple[str, str | None, str | None]:
    found = juris = None
    for phrase, cls in forms:  # whole-word matches anywhere (legal forms appear first or last)
        pat = re.compile(rf"(?<![\w]){re.escape(phrase)}(?![\w])")
        if pat.search(text):
            text = pat.sub(" ", text)
            found = found or cls
            juris = juris or JURISDICTION.get(phrase) or ("UA" if phrase in UA_JURIS else None)
    return re.sub(r"\s+", " ", text).strip(), found, juris


def latin(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _QUOTES.sub(" ", t)
    t = unidecode(t).replace("'", "")  # unidecode renders the Ukrainian soft sign as an apostrophe
    return _NONALNUM.sub(" ", t.lower().replace("&", " and ")).strip()


def skeleton(text: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", text)
    for a, b in SKELETON_RULES:
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)  # collapse doubled letters


def normalize_name(name: str | None) -> dict:
    raw = name or ""
    low = _QUOTES.sub(" ", unicodedata.normalize("NFKC", raw)).lower()
    low, form_ua, juris_ua = _strip_forms(low, UA_FORMS)
    lat = latin(low)
    lat, form_lat, juris_lat = _strip_forms(lat, LATIN_FORMS)
    tokens = [ABBREV.get(t, t) for t in lat.split() if t not in NOISE]
    if not tokens:  # the name was only a legal form / noise word: keep what we had
        tokens = lat.split() or latin(raw).split()
    core = " ".join(tokens)
    return {
        "name_latin": latin(raw),
        "legal_form": form_ua or form_lat,
        "jurisdiction": juris_ua or juris_lat,
        "name_core": core,
        "name_sorted": " ".join(sorted(tokens)),
        "skeleton": skeleton(core),
        "first_token": tokens[0] if tokens else "",
        # numbers in a name usually identify the entity (Plant 1500 vs Plant 1600, Gr. 00 vs Gr. 01)
        "name_numbers": " ".join(sorted({str(int(n)) for n in re.findall(r"\d+", latin(raw))})) or None,
        "has_cyrillic": bool(re.search("[Ѐ-ӿ]", raw)),
    }


def normalize_id(id_type: str | None, value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return f"{id_type or 'ID'}:{digits}" if digits else None


def normalize_city(city: str | None) -> str:
    return skeleton(latin(city)) if city else ""


if __name__ == "__main__":
    for n in ["ТОВ «Юг-Агрос»", "YUH-AHROS LLC", "YUH-AHROS, TOV", "Yug-Agros TOV",
              "Товариство з обмеженою відповідальністю \"Херсонський завод кондитерських виробів\"",
              "Khersonskyi zavod kondyterskykh vyrobiv LLC", "HAKO Sp z oo", "HAKO Sp. z o.o.",
              "Robert Tyrawa \"TYRAWA\" Usługi autokarowe", "International Business Machines Corp", "Intl Business Machines"]:
        r = normalize_name(n)
        print(f"{n[:60]:60s} -> core={r['name_core']!r:45s} skel={r['skeleton']!r} form={r['legal_form']}")

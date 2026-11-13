"""Name variants, transliteration, legal forms and national-ID helpers.

Everything here is used by the *generator* to create realistic, recorded variants of
real supplier names. The matching side (Databricks) has its own normalization code.
"""
from __future__ import annotations

import random
import re
import string

from unidecode import unidecode

# ---------------------------------------------------------------- transliteration
# Ukrainian official (KMU 2010) romanization
_UA_OFFICIAL = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh",
    "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "’": "", "ʼ": "",
}
# Word-initial forms in KMU 2010
_UA_OFFICIAL_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}
# Older / Russian-influenced romanization still common in vendor masters
_UA_LEGACY = dict(_UA_OFFICIAL, **{
    "г": "g", "и": "i", "і": "i", "ї": "yi", "й": "y", "х": "h", "щ": "sch", "є": "ye",
    "ю": "yu", "я": "ya", "ц": "c", "ж": "j",
})


def translit_ua(text: str, style: str = "official") -> str:
    table = _UA_OFFICIAL if style == "official" else _UA_LEGACY
    out, prev_alpha = [], False
    for ch in text:
        low = ch.lower()
        if low in table:
            rep = table[low]
            if style == "official" and not prev_alpha and low in _UA_OFFICIAL_INITIAL:
                rep = _UA_OFFICIAL_INITIAL[low]
            if ch.isupper() and rep:
                rep = rep.upper() if (len(rep) == 1 or text.isupper()) else rep.capitalize()
            out.append(rep)
            prev_alpha = True
        else:
            out.append(ch)
            prev_alpha = ch.isalpha()
    return "".join(out)


def has_cyrillic(s: str) -> bool:
    return bool(re.search("[Ѐ-ӿ]", s or ""))


# ---------------------------------------------------------------- legal forms
# canonical key -> surface forms seen in the wild
LEGAL_FORMS = {
    "UA_TOV": ["ТОВ", "ТзОВ", "Товариство з обмеженою відповідальністю", "TOV", "TzOV", "LLC", "Ltd"],
    "UA_PRAT": ["ПрАТ", "Приватне акціонерне товариство", "PrAT", "PJSC", "Private JSC"],
    "UA_PAT": ["ПАТ", "Публічне акціонерне товариство", "PAT", "PJSC", "Public JSC"],
    "UA_AT": ["АТ", "Акціонерне товариство", "AT", "JSC"],
    "UA_PP": ["ПП", "Приватне підприємство", "PP", "Private Enterprise", "PE"],
    "UA_DP": ["ДП", "Державне підприємство", "DP", "State Enterprise", "SE"],
    "PL_SPZOO": ["Sp. z o.o.", "sp. z o.o.", "SP. Z O.O.", "Sp z oo", "spółka z ograniczoną odpowiedzialnością",
                 "SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "LLC", "Ltd"],
    "PL_SA": ["S.A.", "SA", "Spółka Akcyjna", "SPÓŁKA AKCYJNA", "JSC"],
    "PL_SPJ": ["Sp. j.", "sp.j.", "Spółka Jawna", "SPÓŁKA JAWNA", "General Partnership"],
    "PL_SPK": ["Sp. k.", "sp.k.", "Spółka Komandytowa", "SPÓŁKA KOMANDYTOWA", "LP"],
    "INC": ["Inc.", "Inc", "Incorporated", "INC"],
    "CORP": ["Corp.", "Corp", "Corporation", "CORPORATION"],
    "LLC": ["LLC", "L.L.C.", "Llc"],
    "LTD": ["Ltd.", "Ltd", "Limited", "LTD"],
    "LLP": ["LLP", "L.L.P."],
    "GMBH": ["GmbH", "GMBH", "Gesellschaft mit beschränkter Haftung"],
    "BV": ["B.V.", "BV", "Besloten Vennootschap"],
    "SA": ["S.A.", "SA"],
    "AG": ["AG", "Aktiengesellschaft"],
    "PTE": ["Pte Ltd", "Pte. Ltd.", "Private Limited"],
}
# English rendering used by group systems
LEGAL_FORM_EN = {
    "UA_TOV": "LLC", "UA_PRAT": "PJSC", "UA_PAT": "PJSC", "UA_AT": "JSC", "UA_PP": "Private Enterprise",
    "UA_DP": "State Enterprise", "PL_SPZOO": "Sp. z o.o.", "PL_SA": "S.A.", "PL_SPJ": "Sp. j.", "PL_SPK": "Sp. k.",
}
_SURFACE_TO_KEY = sorted(((s, k) for k, ss in LEGAL_FORMS.items() for s in ss), key=lambda x: -len(x[0]))


def split_legal_form(name: str) -> tuple[str, str | None]:
    """Return (core name, legal form key) by finding a known legal form at either end."""
    n = name.strip()
    for surface, key in _SURFACE_TO_KEY:
        pat_end = re.compile(r"[\s,]+" + re.escape(surface) + r"\.?$", re.IGNORECASE)
        pat_start = re.compile(r"^" + re.escape(surface) + r"[\s,]+", re.IGNORECASE)
        if pat_end.search(n):
            return pat_end.sub("", n).strip(" ,"), key
        if pat_start.search(n):
            return pat_start.sub("", n).strip(" ,"), key
    return n, None


def strip_quotes(s: str) -> str:
    return re.sub(r"[\"'«»„“”]", "", s).strip()


# ---------------------------------------------------------------- variants
_ABBREV = [
    ("International", "Intl"), ("Corporation", "Corp"), ("Company", "Co"), ("Services", "Svcs"),
    ("Technologies", "Tech"), ("Technology", "Tech"), ("Solutions", "Sol"), ("Manufacturing", "Mfg"),
    ("Associates", "Assoc"), ("Management", "Mgmt"), ("Logistics", "Log"), (" and ", " & "),
    ("Consulting", "Cons"), ("Industries", "Ind"), ("Systems", "Sys"),
]


def typo(s: str, rng: random.Random) -> str:
    letters = [i for i, c in enumerate(s) if c.isalpha()]
    if len(letters) < 4:
        return s
    i = rng.choice(letters[1:-1])
    op = rng.choice(["swap", "drop", "double"])
    if op == "swap" and i + 1 < len(s):
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "drop":
        return s[:i] + s[i + 1:]
    return s[:i] + s[i] + s[i:]


def abbreviate(s: str, rng: random.Random) -> str:
    opts = [(a, b) for a, b in _ABBREV if a.lower() in s.lower()]
    if not opts:
        return s
    a, b = rng.choice(opts)
    return re.sub(re.escape(a), b, s, count=1, flags=re.IGNORECASE)


def depunct(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(str.maketrans("", "", ".,'\"«»„“”"))).strip()


def title(s: str) -> str:
    return " ".join(w if (w.isupper() and len(w) <= 4) else w.capitalize() for w in s.split())


def with_legal_form(core: str, key: str | None, rng: random.Random, english: bool = False,
                    latin: bool = False) -> str:
    if not key:
        return core
    if english and key in LEGAL_FORM_EN:
        form = LEGAL_FORM_EN[key]
    else:
        forms = LEGAL_FORMS[key]
        if latin:
            forms = [f for f in forms if not has_cyrillic(f)] or forms
        form = rng.choice(forms[:4])
    if key.startswith("UA_") and has_cyrillic(form):
        return f"{form} «{core}»" if rng.random() < 0.6 else f'{form} "{core}"'
    if key.startswith("UA_") and rng.random() < 0.5:
        return f"{core.upper()}, {form.upper()}"
    return f"{core} {form}"


def noisy(s: str, rng: random.Random, p_typo=0.08, p_abbrev=0.25, p_depunct=0.3) -> tuple[str, list[str]]:
    applied = []
    if rng.random() < p_abbrev:
        t = abbreviate(s, rng)
        if t != s:
            s, applied = t, applied + ["abbreviation"]
    if rng.random() < p_depunct:
        t = depunct(s)
        if t != s:
            s, applied = t, applied + ["punctuation"]
    if rng.random() < p_typo:
        s, applied = typo(s, rng), applied + ["typo"]
    return s, applied


def latinize(name: str, rng: random.Random) -> tuple[str, str]:
    """Latin-script rendering of a possibly Cyrillic/diacritic name. Returns (text, method)."""
    if has_cyrillic(name):
        style = "official" if rng.random() < 0.7 else "legacy"
        return translit_ua(name, style), f"translit_{style}"
    if name != unidecode(name) and rng.random() < 0.5:
        return unidecode(name), "diacritics_stripped"
    return name, "none"


def sap_truncate(s: str) -> str:
    return s[:35].rstrip()


# ---------------------------------------------------------------- national IDs
def nip_valid(nip: str) -> bool:
    d = re.sub(r"\D", "", nip or "")
    if len(d) != 10:
        return False
    w = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    c = sum(int(a) * b for a, b in zip(d, w)) % 11
    return c != 10 and c == int(d[9])


def edrpou_valid(code: str) -> bool:
    d = re.sub(r"\D", "", code or "")
    if len(d) != 8:
        return False
    n = int(d)
    w = [1, 2, 3, 4, 5, 6, 7] if (n < 30000000 or n > 60000000) else [7, 1, 2, 3, 4, 5, 6]
    s = sum(int(a) * b for a, b in zip(d, w)) % 11
    if s == 10:
        s = sum(int(a) * (b + 2) for a, b in zip(d, w)) % 11
        s = 0 if s == 10 else s
    return s == int(d[7])


def corrupt_check_digit(code: str, rng: random.Random) -> str:
    """Change the last digit so the checksum fails (recorded as an injected defect)."""
    last = code[-1]
    new = rng.choice([c for c in string.digits if c != last])
    return code[:-1] + new


def fake_ein(rng: random.Random) -> str:
    return f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"


_IBAN_BBAN = {"NL": ("AAAA", 10), "DE": ("", 18), "PL": ("", 24), "UA": ("", 25), "FR": ("", 23), "GB": ("AAAA", 14)}


def fake_iban(country: str, rng: random.Random) -> str | None:
    """Syntactically valid IBAN with random digits (fictional account)."""
    spec = _IBAN_BBAN.get(country)
    if not spec:
        return None
    letters, n = spec
    bban = "".join(rng.choice(string.ascii_uppercase) for _ in letters) + "".join(rng.choice(string.digits) for _ in range(n))
    rearranged = bban + country + "00"
    num = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - int(num) % 97
    return f"{country}{check:02d}{bban}"


# ---------------------------------------------------------------- fictional names
_FIC_A = ["Granite", "Cedar", "Harbor", "Summit", "Bluewater", "Ironwood", "Silverline", "Northgate", "Redfern",
          "Oakridge", "Pinecrest", "Stonebridge", "Brightwell", "Clearpath", "Kestrel", "Halcyon", "Meridian",
          "Copperfield", "Westbrook", "Ashford"]
_FIC_B = ["Logistics", "Facility Services", "IT Solutions", "Industrial Supply", "Consulting", "Maintenance",
          "Freight", "Cleaning Services", "Technologies", "Engineering", "Packaging", "Advisory"]
_FIC_FORM = ["LLC", "Inc.", "Ltd", "Group", "Co.", "GmbH", "B.V."]


def fictional_name(rng: random.Random) -> str:
    return f"{rng.choice(_FIC_A)} {rng.choice(_FIC_B)} {rng.choice(_FIC_FORM)}"

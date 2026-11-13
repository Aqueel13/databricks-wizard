"""Shared settings. Everything is deterministic for a given SEED."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(os.environ.get("WIZARD_ROOT", Path(__file__).resolve().parents[2]))
RAW = ROOT / "data" / "raw"
STAGE = ROOT / "data" / "stage"          # generator outputs (parquet), incl. ground truth
EXPORTS = ROOT / "data" / "exports"      # file-based sources (HICX exports, documents)

SEED = int(os.environ.get("WIZARD_SEED", "42"))

# Fictional enterprise
ENTERPRISE = "Larkspur Foods"
COMPANY_CODES = {
    # code: (name, country, currency, city)
    "US01": ("Larkspur Foods Inc.", "US", "USD", "Chicago"),
    "NL01": ("Larkspur Foods B.V.", "NL", "EUR", "Rotterdam"),
    "PL01": ("Larkspur Foods Polska Sp. z o.o.", "PL", "PLN", "Poznań"),
    "UA01": ("ТОВ «Ларкспур Фудс Україна»", "UA", "UAH", "Київ"),
}
# Source SAP company codes -> ours (small codes C003/C005 are dropped)
SAP_BUKRS_MAP = {"USA1": "US01", "C001": "US01", "EU01": "NL01", "C004": "NL01"}

# All SAP dates are shifted so the newest PO lands here.
HISTORY_END = date(2026, 10, 31)

# Indirect spend categories (ADR 0003)
INDIRECT_CATEGORIES = ["IT_SERVICES", "MRO", "LOGISTICS", "PROFESSIONAL_SERVICES", "FACILITIES"]

# Universe sizes
N_WORLDBANK = 450
N_POLAND = 350
N_UKRAINE = 300
N_FICTIONAL = 60
N_SAP_DIRECT_US = 220      # SAP demo vendors that receive the real US01 direct-spend POs
N_SAP_DIRECT_NL = 140
PL_PO_SHARE = 0.08         # share of NL01 POs cloned into PL01
UA_PO_SHARE = 0.05         # share of NL01 POs cloned into UA01
N_DRIFT_SUPPLIERS = 12     # direct suppliers whose delivery performance degrades
DRIFT_WINDOW_DAYS = 270

FX_FROM_EUR = {"EUR": 1.0, "USD": 1.08, "PLN": 4.30, "UAH": 45.0}

PG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5433")),
    "user": os.environ.get("PGUSER", "wizard"),
    "password": os.environ.get("PGPASSWORD", "wizard"),
}


def pg_dsn(db: str) -> str:
    return f"host={PG['host']} port={PG['port']} user={PG['user']} password={PG['password']} dbname={db}"

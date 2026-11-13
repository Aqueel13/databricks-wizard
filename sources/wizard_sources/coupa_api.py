"""Coupa-style REST API over the `coupa` database.

Mimics the real Coupa API's shape:
  * kebab-case JSON keys (`po-number`, `updated-at`), nested `supplier` objects
  * offset paging, max 50 records per call
  * incremental filter `updated-at[gt]=<ISO timestamp>`
  * API key header `X-COUPA-API-KEY`
  * schema drift: `diversity-class` appears only on suppliers updated on/after 2025-06-01
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row

from . import config as C

API_KEY = os.environ.get("COUPA_API_KEY", "wizard-dev-key")
MAX_LIMIT = 50

app = FastAPI(title="Coupa mock (Databricks Wizard)", version="1.0")


def auth(x_coupa_api_key: str | None = Header(default=None)) -> None:
    if x_coupa_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-COUPA-API-KEY")


def _kebab(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, datetime):
            v = v.isoformat() + "Z"
        elif isinstance(v, date):
            v = v.isoformat()
        out[k.replace("_", "-")] = v
    return out


def _page(table: str, updated_gt: str | None, offset: int, limit: int) -> list[dict]:
    sql = f"select * from {table}"
    params: list = []
    if updated_gt:
        sql += " where updated_at > %s"
        params.append(updated_gt.rstrip("Z"))
    sql += " order by updated_at, id offset %s limit %s"
    params += [offset, min(limit, MAX_LIMIT)]
    with psycopg.connect(C.pg_dsn("coupa"), row_factory=dict_row) as pg:
        return list(pg.execute(sql, params))


Updated = Query(default=None, alias="updated-at[gt]", description="ISO timestamp; return records updated after it")
Offset = Query(default=0, ge=0)
Limit = Query(default=MAX_LIMIT, ge=1, description="capped at 50, like Coupa")


@app.get("/api/suppliers", dependencies=[Depends(auth)])
def suppliers(updated: str | None = Updated, offset: int = Offset, limit: int = Limit):
    rows = _page("suppliers", updated, offset, limit)
    for r in rows:
        if r["diversity_class"] is None:
            del r["diversity_class"]
    return [_kebab(r) for r in rows]


@app.get("/api/purchase_orders", dependencies=[Depends(auth)])
def purchase_orders(updated: str | None = Updated, offset: int = Offset, limit: int = Limit):
    rows = _page("purchase_orders", updated, offset, limit)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    with psycopg.connect(C.pg_dsn("coupa"), row_factory=dict_row) as pg:
        lines = list(pg.execute("select * from order_lines where po_id = any(%s) order by po_id, line_num", [ids]))
        sups = {s["id"]: s for s in pg.execute("select id, number, name from suppliers where id = any(%s)",
                                                [[r["supplier_id"] for r in rows]])}
    by_po: dict[int, list] = {}
    for ln in lines:
        by_po.setdefault(ln.pop("po_id"), []).append(_kebab(ln))
    out = []
    for r in rows:
        sid = r.pop("supplier_id")
        doc = _kebab(r)
        doc["supplier"] = _kebab(sups.get(sid, {"id": sid}))
        doc["order-lines"] = by_po.get(r["id"], [])
        out.append(doc)
    return out


@app.get("/api/receipts", dependencies=[Depends(auth)])
def receipts(updated: str | None = Updated, offset: int = Offset, limit: int = Limit):
    return [_kebab(r) for r in _page("receipts", updated, offset, limit)]


@app.get("/api/invoices", dependencies=[Depends(auth)])
def invoices(updated: str | None = Updated, offset: int = Offset, limit: int = Limit):
    out = []
    for r in _page("invoices", updated, offset, limit):
        sid = r.pop("supplier_id")
        doc = _kebab(r)
        doc["supplier"] = {"id": sid}
        out.append(doc)
    return out


@app.get("/api/contracts", dependencies=[Depends(auth)])
def contracts(updated: str | None = Updated, offset: int = Offset, limit: int = Limit):
    out = []
    for r in _page("contracts", updated, offset, limit):
        sid = r.pop("supplier_id")
        doc = _kebab(r)
        doc["supplier"] = {"id": sid}
        out.append(doc)
    return out


@app.get("/health")
def health():
    return {"status": "ok"}

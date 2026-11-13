"""Warehouse and workspace access for the MCP servers, plus the audit log of every tool call."""
from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
from contextvars import ContextVar
from typing import Any

from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

CAT = os.environ.get("WIZARD_CATALOG", "wizard")
_cfg = Config()
_ws: WorkspaceClient | None = None


def ws() -> WorkspaceClient:
    global _ws
    if _ws is None:
        _ws = WorkspaceClient(config=_cfg)
    return _ws


def _conn():
    wh = os.environ.get("DATABRICKS_WAREHOUSE_ID") or next(iter(ws().warehouses.list())).id
    return sql.connect(server_hostname=_cfg.host, http_path=f"/sql/1.0/warehouses/{wh}",
                       credentials_provider=lambda: _cfg.authenticate)


def query(q: str, params: dict | None = None, limit: int | None = None) -> list[dict]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(q, params or {})
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
    return [{k: _plain(v) for k, v in zip(cols, r, strict=True)} for r in rows]


def execute(q: str, params: dict | None = None) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute(q, params or {})


def _plain(v: Any) -> Any:
    """JSON-friendly values (dates, decimals, numpy arrays from the connector)."""
    if v is None or isinstance(v, str | int | float | bool):
        return v
    if hasattr(v, "tolist"):
        return v.tolist()
    if isinstance(v, list | tuple):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    try:
        return float(v) if v.__class__.__name__ == "Decimal" else str(v)
    except Exception:
        return str(v)


# ------------------------------------------------------------------ audit
# Set per request by the ASGI wrapper in app.py from the identity headers the Databricks Apps proxy adds
# (the tool Context in this SDK version does not expose the HTTP request).
CURRENT_CALLER: ContextVar[str] = ContextVar("current_caller", default="unknown")


def caller(ctx=None) -> str:
    who = CURRENT_CALLER.get()
    if who != "unknown":
        return who
    try:
        h = ctx.request_context.request.headers
        return h.get("x-forwarded-email") or h.get("x-forwarded-preferred-username") or "unknown"
    except Exception:
        return "unknown"


def _log(row: dict) -> None:
    try:
        execute(f"""INSERT INTO {CAT}.ops.agent_tool_calls VALUES
                    (current_timestamp(), :server, :tool, :arguments, :caller, :ok, :rows, :millis, :error)""", row)
    except Exception as e:  # auditing must never break a tool call
        print("audit write failed:", e)


def audited(server: str):
    """Decorator: time the call, count result rows, and write an audit row in the background."""
    def wrap(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def inner(*args, **kwargs):
            ctx = kwargs.get("ctx")
            t, ok, err, rows = time.time(), True, None, None
            try:
                out = fn(*args, **kwargs)
                res = out.get("result") if isinstance(out, dict) else None
                rows = len(res) if isinstance(res, list) else None
                return out
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"[:500]
                raise
            finally:
                bound = {k: v for k, v in kwargs.items() if k != "ctx"}
                threading.Thread(target=_log, daemon=True, args=({
                    "server": server, "tool": fn.__name__, "arguments": json.dumps(bound, default=str)[:4000],
                    "caller": caller(ctx), "ok": ok, "rows": rows, "millis": int((time.time() - t) * 1000),
                    "error": err},)).start()
        inner.__signature__ = sig
        return inner
    return wrap

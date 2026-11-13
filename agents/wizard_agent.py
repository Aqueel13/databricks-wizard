"""Agent harness over MCP servers (Databricks Wizard and, optionally, WatchDog) with a Databricks model endpoint.

  python agents/wizard_agent.py ask "Which suppliers have deteriorating delivery performance?" [--watchdog]
  python agents/wizard_agent.py bench [--models m1,m2]

Wizard servers are reached through the Databricks Apps proxy with an OAuth token from the Databricks CLI.
WatchDog servers are reached through the local SSH tunnel with the auth headers already configured for them in
~/.claude.json (read at runtime, never printed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path

from databricks.sdk import WorkspaceClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard")
DEFAULT_MODEL = "databricks-qwen3-next-80b-a3b-instruct"  # 12/12 and fastest in the tool-calling benchmark (ADR 0005)
w = WorkspaceClient(profile=PROFILE)
# the deployed app's URL, looked up rather than hard-coded (it contains the workspace id)
WIZARD_URL = os.environ.get("WIZARD_MCP_URL") or w.apps.get("wizard-mcp").url


def wizard_servers() -> list[tuple[str, str, dict]]:
    tok = json.loads(subprocess.check_output(["databricks", "auth", "token", "--profile", PROFILE, "-o", "json"]))
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    return [(f"wizard_{s}", f"{WIZARD_URL}/{s}/mcp", h) for s in ("analytics", "knowledge", "stewardship")]


def watchdog_servers() -> list[tuple[str, str, dict]]:
    cfg = json.loads((Path.home() / ".claude.json").read_text())
    servers = cfg.get("mcpServers", {})
    return [(name.replace("-", "_"), s["url"], s.get("headers", {})) for name, s in servers.items()
            if name.startswith("watchdog-")]


# Tools that change state in another system. Never exposed unless explicitly allowed (allow_writes=True).
WRITE_TOOLS = {"propose_action", "execute_action", "propose_decision"}


class Hub:
    """Open MCP sessions to several servers and expose their tools to a function-calling model."""

    def __init__(self, allow_writes: bool = False) -> None:
        self.allow_writes = allow_writes
        self.stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, tuple[str, str]] = {}   # function name -> (server prefix, tool name)
        self.specs: list[dict] = []
        self.instructions: list[str] = []

    async def connect(self, servers) -> None:
        for prefix, url, headers in servers:
            r, wr, _ = await self.stack.enter_async_context(streamablehttp_client(url, headers=headers))
            s = await self.stack.enter_async_context(ClientSession(r, wr))
            init = await s.initialize()
            self.sessions[prefix] = s
            if init.instructions:
                self.instructions.append(f"[{prefix}] {init.instructions}")
            for t in (await s.list_tools()).tools:
                if t.name in WRITE_TOOLS and not self.allow_writes:
                    continue
                fname = f"{prefix}__{t.name}"[:64]
                self.tools[fname] = (prefix, t.name)
                params = dict(t.inputSchema or {"type": "object", "properties": {}})
                params.setdefault("properties", {}).pop("ctx", None)
                self.specs.append({"type": "function", "function": {
                    "name": fname, "description": (t.description or "")[:1000], "parameters": params}})

    async def call(self, fname: str, args: dict) -> tuple[str, bool]:
        prefix, tool = self.tools[fname]
        res = await self.sessions[prefix].call_tool(tool, args)
        text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
        return text[:12000], not res.isError

    async def close(self) -> None:
        await self.stack.aclose()


SYSTEM = """You are a procurement analyst's assistant. Answer using the tools; never invent numbers, names or dates.
Prefer governed tools (metrics, supplier lookups, documents) over exploratory SQL. Cite document passages as given.
A sanctions screening alert is a name similarity pending review: never call a supplier sanctioned unless the
screening status is 'confirmed'. Keep the final answer short and specific.

Server notes:
"""


def _llm(model: str, messages: list, tools: list) -> dict:
    body = {"messages": messages, "tools": tools, "max_tokens": 1500, "temperature": 0.0}
    return w.api_client.do("POST", f"/serving-endpoints/{model}/invocations", body=body)


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return content or ""


def _date_context() -> str:
    """Models don't know today's date (one refused 'May 2026' as being in the future): state it, and the data's as-of."""
    import datetime
    return f"Today is {datetime.date.today().isoformat()}. Supplier data runs through about 2026-11-03.\n\n"


async def run(hub: Hub, question: str, model: str, max_steps: int = 8) -> dict:
    messages = [{"role": "system", "content": _date_context() + SYSTEM + "\n".join(hub.instructions)},
                {"role": "user", "content": question}]
    steps, t0 = [], time.time()
    for _ in range(max_steps):
        resp = await asyncio.to_thread(_llm, model, messages, hub.specs)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": _text(msg.get("content")), "steps": steps, "seconds": round(time.time() - t0, 1)}
        messages.append({"role": "assistant", "content": _text(msg.get("content")) or None, "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            resolved = name
            if name not in hub.tools:  # some models drop the server prefix: resolve if exactly one tool matches
                matches = [f for f in hub.tools if f.split("__", 1)[-1] == name]
                resolved = matches[0] if len(matches) == 1 else None
            if resolved:
                out, ok = await hub.call(resolved, args)
            else:
                out, ok = f"unknown tool {name}", False
            steps.append({"tool": resolved or name, "args": args, "ok": ok, "unprefixed": resolved != name})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    return {"answer": "(no final answer within the step limit)", "steps": steps, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------------------------- benchmark
def sql(q: str) -> list[dict]:
    from databricks.sdk.service.sql import StatementState
    wh = next(iter(w.warehouses.list())).id
    r = w.statement_execution.execute_statement(statement=q, warehouse_id=wh, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = w.statement_execution.get_statement(r.statement_id)
    cols = [c.name for c in r.manifest.schema.columns]
    return [dict(zip(cols, row, strict=True)) for row in (r.result.data_array or [])]


DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def has(answer: str, *needles) -> bool:
    a = re.sub(r"[\s,]", "", answer.lower().translate(DASHES))
    return all(re.sub(r"[\s,]", "", str(n).lower()) in a for n in needles)


def pct_or_ratio(answer: str, x: float) -> bool:
    return any(s in answer for s in (f"{100 * x:.1f}", f"{100 * x:.0f}%", f"{x:.3f}", f"{x:.2f}"))


def cases() -> list[dict]:
    det = sql("""select legal_name from wizard.gold.supplier_360 where deteriorating order by on_time_drop desc limit 1""")[0]
    ot = {r["d"]: float(r["v"]) for r in sql("""SELECT `Deteriorating Supplier` d, MEASURE(`On-Time Rate`) v
          FROM wizard.gold.metrics_delivery WHERE `Receipt Month` >= '2026-05-01' GROUP BY ALL""")}
    yug = sql("""select s.risk_band, s.country from wizard.gold.supplier_name_index n
                 join wizard.gold.supplier_360 s using (supplier_id) where n.name = 'ТОВ Юг-Агрос' limit 1""")[0]
    three = sql("""select supplier_id, legal_name from wizard.gold.supplier_360 where size(source_systems) = 3
                   and legal_name_local is not null order by supplier_id limit 1""")[0]
    conc = sql("select category_name, hhi from wizard.gold.spend_concentration order by hhi desc limit 1")[0]
    pl = sql("""select legal_name from wizard.gold.supplier_360 where country = 'PL' and size(dq_issues) > 0
                order by dq_score asc nulls last, legal_name limit 1""")[0]
    q = sql("select count(*) n from wizard.silver.er_review_queue")[0]
    return [
        dict(id="deteriorating_list", tools={"find_suppliers"},
             q="Which suppliers have deteriorating delivery performance? Name the one with the largest drop.",
             check=lambda a: has(a, det["legal_name"][:12])),
        dict(id="on_time_metric", tools={"query_metric"},
             q="Since May 2026, what is the on-time delivery rate of deteriorating suppliers compared with all others?",
             check=lambda a: pct_or_ratio(a, ot["true"]) and pct_or_ratio(a, ot["false"])),
        dict(id="cross_script_lookup", tools={"search_suppliers"},
             q="Find the supplier written as 'Yug Agros' and tell me its country and risk band.",
             check=lambda a: has(a, yug["risk_band"]) and (has(a, yug["country"]) or has(a, "ukrain"))),
        dict(id="explain_match", tools={"explain_supplier_match"},
             q=f"Why were the source records of supplier {three['supplier_id']} ({three['legal_name']}) merged into one supplier?",
             check=lambda a: any(k in a.lower() for k in ("national id", "edrpou", "deterministic", "same id", "identifier"))),
        dict(id="certificate_vs_system", tools={"get_document", "answer_document_question", "search_documents"},
             q="When does certificate ISO-397951 expire according to the certificate itself, and what does HICX say?",
             check=lambda a: has(a, "2026-06-19") and has(a, "2026-12-16")),
        dict(id="contract_clause", tools={"answer_document_question", "search_documents", "get_document"},
             q="Which law governs agreement CTR-00160?", check=lambda a: has(a, "illinois")),
        dict(id="concentration", tools={"get_spend_concentration"},
             q="Which spend category is the most concentrated in the last 12 months, and what is its HHI?",
             check=lambda a: has(a, conc["category_name"]) and has(a, str(int(float(conc["hhi"]))))),
        dict(id="what_if", tools={"simulate_consolidation"},
             q="If we consolidated all Logistics spend into our largest Logistics supplier, what would the HHI become, "
               "and what warnings apply?",
             check=lambda a: has(a, "10000") and any(k in a.lower() for k in ("single", "more than half", "dependency"))),
        dict(id="lineage", tools={"trace_metric"},
             q="Where does the 'Spend USD' metric come from? Trace it back to the source systems.",
             check=lambda a: any(k in a.lower() for k in ("bronze", "sap", "coupa"))),
        dict(id="review_queue", tools={"list_review_items"},
             q="Are there supplier-record pairs waiting for steward review? What is the most common reason?",
             check=lambda a: has(a, "different national id") or has(a, "nationalid")),
        dict(id="dq_list", tools={"find_suppliers"},
             q="Which Polish supplier has the worst master data quality, and what are its issues?",
             check=lambda a: has(a, pl["legal_name"][:12])),
        dict(id="sanctions_policy", tools={"search_suppliers", "get_supplier_360", "find_suppliers"},
             q="Is the supplier Tropicana Sp. z o.o. sanctioned?",
             check=lambda a: any(k in a.lower() for k in ("pending", "not confirmed", "alert", "not been confirmed",
                                                          "name similarity", "review"))
             and not re.search(r"\bis (a )?sanctioned\b", a.lower().replace("not sanctioned", ""))),
    ], q["n"]


async def bench(models: list[str]) -> None:
    cs, _ = cases()
    results = []
    for model in models:
        hub = Hub()
        await hub.connect(wizard_servers())
        try:
            for c in cs:
                try:
                    r = await run(hub, c["q"], model)
                except Exception as e:
                    r = {"answer": f"(error: {type(e).__name__}: {str(e)[:200]})", "steps": [], "seconds": 0}
                used = {s["tool"].split("__", 1)[-1] for s in r["steps"]}
                row = dict(model=model.replace("databricks-", ""), case=c["id"], tool_ok=bool(used & c["tools"]),
                           answer_ok=bool(c["check"](r["answer"])), tool_errors=sum(not s["ok"] for s in r["steps"]),
                           calls=len(r["steps"]), seconds=r["seconds"], answer=r["answer"][:400])
                results.append(row)
                print(f"{row['model'][:28]:28s} {c['id']:24s} tool={'Y' if row['tool_ok'] else 'n'} "
                      f"answer={'Y' if row['answer_ok'] else 'n'} calls={row['calls']} err={row['tool_errors']} "
                      f"{row['seconds']}s", flush=True)
        finally:
            await hub.close()
    out = Path(__file__).resolve().parents[1] / "data" / "agent_benchmark.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print("\nmodel                         tool_ok  answer_ok  avg_calls  tool_errors  avg_s")
    for m in dict.fromkeys(r["model"] for r in results):
        rs = [r for r in results if r["model"] == m]
        print(f"{m[:28]:28s}  {sum(r['tool_ok'] for r in rs):>3}/{len(rs)}   {sum(r['answer_ok'] for r in rs):>3}/{len(rs)}"
              f"   {sum(r['calls'] for r in rs) / len(rs):>8.1f}  {sum(r['tool_errors'] for r in rs):>10}  "
              f"{sum(r['seconds'] for r in rs) / len(rs):>5.1f}")


async def ask(question: str, model: str, with_watchdog: bool) -> None:
    hub = Hub()
    await hub.connect(wizard_servers() + (watchdog_servers() if with_watchdog else []))
    try:
        print(f"{len(hub.specs)} tools from {len(hub.sessions)} servers: {sorted(hub.sessions)}\n")
        r = await run(hub, question, model, max_steps=10)
        for s in r["steps"]:
            print(f"  -> {s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:160]}){'' if s['ok'] else '  [error]'}")
        print(f"\n{r['answer']}\n({r['seconds']}s)")
    finally:
        await hub.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--model", default=DEFAULT_MODEL)
    a.add_argument("--watchdog", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--models", default="databricks-qwen3-next-80b-a3b-instruct,databricks-meta-llama-3-3-70b-instruct,"
                                       "databricks-llama-4-maverick,databricks-gpt-oss-120b")
    args = ap.parse_args()
    if args.cmd == "ask":
        asyncio.run(ask(args.question, args.model, args.watchdog))
    else:
        asyncio.run(bench(args.models.split(",")))

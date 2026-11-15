"""Smoke-test the Wizard MCP servers with a real MCP client (local uvicorn or the deployed app).

  python scripts/mcp_smoke.py http://localhost:8090            # local
  python scripts/mcp_smoke.py https://<app-url> --token <oauth> # deployed
"""
import argparse
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CALLS = {
    "analytics": [("search_suppliers", {"query_text": "Yug Agros"}),
                  ("query_metric", {"metric": "on_time_rate", "group_by": ["deteriorating_supplier"],
                                    "period_from": "2026-05-01"}),
                  ("trace_metric", {"metric": "spend_usd"}),
                  ("list_delivery_alerts", {"status": "open", "limit": 3})],
    "knowledge": [("answer_document_question", {"question": "Which law governs agreement CTR-00160?"})],
    "stewardship": [("list_review_items", {"kind": "match", "limit": 2})],
}


async def run(base: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    for server, calls in CALLS.items():
        async with streamablehttp_client(f"{base}/{server}/mcp", headers=headers) as (r, w, _), ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"\n== {server} ({init.serverInfo.name}): {len(tools)} tools: {[t.name for t in tools]}")
            for name, args in calls:
                res = await s.call_tool(name, args)
                text = res.content[0].text if res.content else ""
                print(f"-- {name}{' ERROR' if res.isError else ''}: {text[:700]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--token")
    a = ap.parse_args()
    asyncio.run(run(a.base.rstrip("/"), a.token))

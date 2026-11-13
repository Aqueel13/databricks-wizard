"""Databricks Wizard MCP servers, one Databricks App: /analytics/mcp, /knowledge/mcp, /stewardship/mcp."""
import contextlib

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from wizard_mcp import analytics, knowledge, stewardship
from wizard_mcp.db import CURRENT_CALLER

SERVERS = {"analytics": analytics.mcp, "knowledge": knowledge.mcp, "stewardship": stewardship.mcp}
apps = {name: m.streamable_http_app() for name, m in SERVERS.items()}  # creates each session manager


@contextlib.asynccontextmanager
async def lifespan(_):
    async with contextlib.AsyncExitStack() as stack:
        for m in SERVERS.values():
            await stack.enter_async_context(m.session_manager.run())
        yield


async def index(_):
    return JSONResponse({"servers": {n: f"/{n}/mcp" for n in SERVERS}, "project": "Databricks Wizard"})


class ExactMcpPath:
    """Serve /<server>/mcp exactly like /<server>/mcp/. The MCP SDK otherwise answers with a 307 to the
    trailing-slash URL, and behind the Databricks Apps proxy that redirect points at the container's internal
    address (https://localhost:8000/...), which clients cannot reach."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            CURRENT_CALLER.set(h.get("x-forwarded-email") or h.get("x-forwarded-preferred-username") or "unknown")
            if scope["path"].endswith("/mcp"):
                scope = {**scope, "path": scope["path"] + "/", "raw_path": (scope["path"] + "/").encode()}
        await self.inner(scope, receive, send)


app = ExactMcpPath(Starlette(routes=[Route("/", index), *[Mount(f"/{n}", a) for n, a in apps.items()]],
                             lifespan=lifespan))

"""Document Q&A used by the M6 evaluation: a thin wrapper around the MCP knowledge server's own code, so the
evaluation tests exactly what agents use (apps/mcp/wizard_mcp/knowledge.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "mcp"))
from wizard_mcp.db import ws as client  # noqa: E402,F401
from wizard_mcp.knowledge import answer, search  # noqa: E402,F401

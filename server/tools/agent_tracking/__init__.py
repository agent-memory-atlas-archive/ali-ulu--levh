"""MCP Tools: Agent tracking, presence, checkpoints and analytics.

Split out of the single-file ``agent_tracking.py`` (issue #96), which
carried ten tool closures in one ``register()``. The package now has one
module per concern:

- ``presence.py``    connect / heartbeat / disconnect (session lifecycle)
- ``checkpoints.py`` create / list work-state snapshots
- ``analytics.py``   activity, stats, per-agent metrics, billing, collaboration

This ``__init__`` keeps the registry contract: the declarative
``TOOL_MODULES`` table (see ``server.tools.register``) imports
``server.tools.agent_tracking`` and calls ``register(mcp, engine)``, which
delegates to the three concern modules. Tool names, schemas and replies
are unchanged.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.core.memory_engine import MemoryEngine
from server.tools.agent_tracking import analytics, checkpoints, presence

_SUBMODULES = (presence, checkpoints, analytics)


def register(mcp: FastMCP, engine: MemoryEngine) -> None:
    for module in _SUBMODULES:
        module.register(mcp, engine)

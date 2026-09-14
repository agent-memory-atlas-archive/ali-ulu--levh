"""Agent presence tools: connect, heartbeat, disconnect.

Split out of the single-file agent_tracking (issue #96): this module owns
the session-lifecycle conversation between an agent and the tracker.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.core.memory_engine import MemoryEngine


def register(mcp: FastMCP, engine: MemoryEngine) -> None:
    @mcp.tool()
    async def agent_connect(
        agent_name: str = "",
        session_id: str = "",
        project: str = "",
    ) -> str:
        """Connect this agent to LEVH and create a tracking session.

        Call this at the start of your session to register your presence.
        LEVH will track your activity and show you in the Agent Dashboard.

        Args:
            agent_name: Your agent name (e.g. "claude-code", "cursor", "vscode").
            session_id: Optional LEVH session ID to link to.
            project: Optional project/workspace name.
        """
        tracker = engine.agent_tracker
        if not tracker:
            return "Agent tracker not available."

        result = await tracker.agent_connect(
            agent_name=agent_name or "unknown",
            session_id=session_id or None,
            project=project or None,
        )

        return (
            f"Connected to LEVH as {result['display']} {result['icon']}\n"
            f"  Agent Session ID: {result['agent_session_id']}\n"
            f"  Project: {result.get('project') or 'none'}\n"
            f"\nUse this ID for heartbeats and disconnect."
        )

    @mcp.tool()
    async def agent_heartbeat(agent_session_id: str = "") -> str:
        """Send a heartbeat to keep your agent connection alive.

        Call this periodically (e.g. every 60 seconds) to show you're active.
        If no heartbeat is received for 2 minutes, you'll be marked offline.

        Args:
            agent_session_id: Your agent session ID from agent_connect.
        """
        tracker = engine.agent_tracker
        if not tracker:
            return "Agent tracker not available."

        if not agent_session_id:
            return "agent_session_id is required. Call agent_connect first."

        result = await tracker.heartbeat(agent_session_id)
        return f"Heartbeat sent at {result['last_heartbeat']}"

    @mcp.tool()
    async def agent_disconnect(agent_session_id: str = "") -> str:
        """Disconnect this agent from LEVH.

        Call this when your session ends to cleanly disconnect.

        Args:
            agent_session_id: Your agent session ID from agent_connect.
        """
        tracker = engine.agent_tracker
        if not tracker:
            return "Agent tracker not available."

        if not agent_session_id:
            return "agent_session_id is required."

        result = await tracker.agent_disconnect(agent_session_id)
        return f"Disconnected at {result['disconnected_at']}"

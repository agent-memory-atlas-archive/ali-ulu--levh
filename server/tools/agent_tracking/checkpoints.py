"""Checkpoint tools: save and list work-state snapshots.

Split out of the single-file agent_tracking (issue #96).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.core.memory_engine import MemoryEngine


def register(mcp: FastMCP, engine: MemoryEngine) -> None:
    @mcp.tool()
    async def create_checkpoint(
        title: str = "",
        summary: str = "",
        project: str = "",
        checkpoint_type: str = "manual",
    ) -> str:
        """Save a checkpoint of your current work state.

        Use this during important tasks to create a snapshot that can be
        resumed later. Checkpoints are visible in the Agent Dashboard.

        Args:
            title: Short description of what you're working on.
            summary: Detailed summary of progress so far.
            project: Project/workspace name.
            checkpoint_type: "manual" (you decided) or "auto" (periodic).
        """
        tracker = engine.agent_tracker
        if not tracker:
            return "Agent tracker not available."

        if not title:
            title = "Work checkpoint"

        # Collect recent memories as context
        recent = await engine.episodic.search(limit=10)
        memory_ids = [m.id for m in recent]

        result = await tracker.create_checkpoint(
            agent_name="unknown",  # Will be enriched if agent is connected
            title=title,
            summary=summary,
            session_id=None,
            project=project or None,
            checkpoint_type=checkpoint_type,
            memory_ids=memory_ids,
        )

        return (
            f"Checkpoint saved: {result['checkpoint_id'][:8]}...\n"
            f"  Title: {result['title']}\n"
            f"  Time: {result['created_at']}"
        )

    @mcp.tool()
    async def list_checkpoints(
        agent_name: str = "",
        project: str = "",
        limit: int = 20,
    ) -> str:
        """List recent checkpoints from agent sessions.

        Args:
            agent_name: Filter by agent name. Empty = all agents.
            project: Filter by project. Empty = all projects.
            limit: Maximum results (1-100). Default 20.
        """
        tracker = engine.agent_tracker
        if not tracker:
            return "Agent tracker not available."

        checkpoints = await tracker.list_checkpoints(
            agent_name=agent_name or None,
            project=project or None,
            limit=min(max(limit, 1), 100),
        )

        if not checkpoints:
            return "No checkpoints found."

        lines = [f"{len(checkpoints)} checkpoints:\n"]
        for cp in checkpoints:
            lines.append(
                f"  [{cp['checkpoint_type']}] {cp['title']}\n"
                f"    Agent: {cp['agent_name']} | {cp['created_at'][:16]}"
            )
            if cp.get("project"):
                lines.append(f"    Project: {cp['project']}")
            lines.append("")

        return "\n".join(lines)

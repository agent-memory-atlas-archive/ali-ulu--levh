"""Code-backed inventory of continuity delivery channels per MCP client.

A0 of the continuity proof exists to stop three independent lists from drifting:
MCP config platforms, native hook installers, and the documentation that tells
operators what happens by default. This module derives the table from those
registries; it does not claim that a client consumes stderr or obeys an MCP
instruction, only what LEVH emits or advertises.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.commands.universal_hooks import SUPPORTED_AGENTS
from server.configs import PLATFORMS


@dataclass(frozen=True)
class ContinuityChannelRow:
    platform: str
    client: str
    stderr_bridge: str
    native_hook: str
    mcp_tool: str


_HOOK_PLATFORM_KEYS = {
    "claude_code": "claude-code",
    "claude_desktop": "claude-desktop",
    "cursor": "cursor",
    "vscode": "vscode",
    "cline": "vscode",
    "windsurf": "windsurf",
}


def _native_hook(platform: str) -> str:
    hook_key = _HOOK_PLATFORM_KEYS.get(platform)
    config = SUPPORTED_AGENTS.get(hook_key or "")
    if config is not None and config.supports_session_start:
        return "opt-in SessionStart"
    return "none"


def continuity_inventory() -> tuple[ContinuityChannelRow, ...]:
    """Every public MCP config target and the channels LEVH provides for it."""
    rows = [
        ContinuityChannelRow(
            platform=platform,
            client=str(meta["description"]),
            stderr_bridge="server emits by default",
            native_hook=_native_hook(platform),
            mcp_tool="tool + start directive by default",
        )
        for platform, meta in PLATFORMS.items()
    ]
    # generic is a public CLI target that normalizes to the standard mcpServers
    # JSON shape rather than owning a duplicate PLATFORMS row.
    rows.append(
        ContinuityChannelRow(
            platform="generic",
            client="Generic MCP client",
            stderr_bridge="server emits by default",
            native_hook="none",
            mcp_tool="tool + start directive by default",
        )
    )
    return tuple(rows)


def render_continuity_inventory() -> str:
    """Markdown table used verbatim by docs/mcp-client-config.md."""
    lines = [
        "| Client | Stderr bridge | Native session-start hook | MCP continuity tool/directive |",
        "| --- | --- | --- | --- |",
    ]
    for row in continuity_inventory():
        lines.append(
            f"| {row.client} | {row.stderr_bridge} | {row.native_hook} | {row.mcp_tool} |"
        )
    return "\n".join(lines)

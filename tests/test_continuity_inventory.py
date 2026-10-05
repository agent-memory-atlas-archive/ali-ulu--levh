"""A0: continuity transport inventory cannot drift from installer registries."""

from pathlib import Path

from server.commands.universal_hooks import SUPPORTED_AGENTS
from server.configs import PLATFORMS, generate_config
from server.continuity_inventory import (
    _HOOK_PLATFORM_KEYS,
    continuity_inventory,
    render_continuity_inventory,
)
from server.core.continuity_instructions import MCP_CONTINUITY_INSTRUCTIONS
from server.tools.profiles import TOOL_TIERS


def _stdio_command(config: dict) -> tuple[str, list[str]]:
    if "mcpServers" in config:
        entry = config["mcpServers"]["levh"]
        return entry["command"], entry["args"]
    if "mcp_servers" in config:
        entry = config["mcp_servers"]["levh"]
        return entry["command"], entry["args"]
    entry = config["mcp"]["levh"]
    command = entry["command"]
    return command[0], command[1:]


def test_inventory_covers_every_public_config_target():
    rows = continuity_inventory()
    assert [row.platform for row in rows] == [*PLATFORMS.keys(), "generic"]

    for platform in PLATFORMS:
        command, args = _stdio_command(generate_config(platform))
        assert command == "levh"
        assert args == ["mcp", "stdio"]


def test_native_hook_column_is_derived_from_real_session_start_support():
    by_platform = {row.platform: row for row in continuity_inventory()}

    assert set(_HOOK_PLATFORM_KEYS) == set(PLATFORMS)
    assert {key for key in _HOOK_PLATFORM_KEYS.values() if key is not None} == set(
        SUPPORTED_AGENTS
    )

    assert SUPPORTED_AGENTS["claude-code"].supports_session_start is True
    assert by_platform["claude_code"].native_hook == "opt-in SessionStart"

    for platform, row in by_platform.items():
        if platform != "claude_code":
            assert row.native_hook == "none"


def test_continuity_tool_and_start_directive_are_in_the_default_surface():
    assert TOOL_TIERS["get_continuity_brief"] == "minimal"
    assert "get_continuity_brief" in MCP_CONTINUITY_INSTRUCTIONS
    assert all(
        row.mcp_tool == "tool + start directive by default"
        for row in continuity_inventory()
    )


def test_documented_inventory_is_generated_from_the_same_registries():
    root = Path(__file__).resolve().parent.parent
    docs = (root / "docs" / "mcp-client-config.md").read_text(encoding="utf-8")
    start = "<!-- continuity-inventory:start -->"
    end = "<!-- continuity-inventory:end -->"
    documented = docs.split(start, 1)[1].split(end, 1)[0].strip()
    assert documented == render_continuity_inventory()

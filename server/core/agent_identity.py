"""Agent identity helpers: known clients, canonical name normalization,
display names and emoji icons.

Split out of ``agent_tracker.py`` (#99) so identity knowledge is a single
source of truth that presence/checkpoint/usage services and tool layers can
all import without pulling in the tracker class.
"""

from __future__ import annotations

# Known agent types (clients) and their display/icon metadata.
KNOWN_AGENTS: dict[str, dict[str, str]] = {
    "claude-code": {"display": "Claude Code", "icon": "🤖"},
    "claude-desktop": {"display": "Claude Desktop", "icon": "🧠"},
    "cursor": {"display": "Cursor", "icon": "⚡"},
    "vscode": {"display": "VS Code", "icon": "💻"},
    "windsurf": {"display": "Windsurf", "icon": "🌊"},
    "cline": {"display": "Cline", "icon": "🔧"},
    "jcode": {"display": "jcode", "icon": "📝"},
    "omp": {"display": "oh-my-pi", "icon": "🥧"},
    "opencode": {"display": "opencode", "icon": "🔓"},
    "codex": {"display": "Codex", "icon": "🤖"},
    "hermes": {"display": "Hermes", "icon": "🏛️"},
    "connector": {"display": "Connector", "icon": "🔗"},
    "dashboard": {"display": "Dashboard", "icon": "📊"},
    "cli": {"display": "CLI", "icon": "⌨️"},
    "mcp-client": {"display": "MCP Client", "icon": "🔌"},
    "auto-connect": {"display": "Auto-Connect", "icon": "🔀"},
    "api": {"display": "REST API", "icon": "🌐"},
    "unknown": {"display": "Unknown Agent", "icon": "❓"},
}


def normalize_agent(agent_name: str) -> str:
    """Normalize agent name to a canonical key."""
    name = (agent_name or "").strip().lower()
    # Aliases
    aliases = {
        "claude": "claude-code",
        "claude_desktop": "claude-desktop",
        "claudecode": "claude-code",
        "claudedesktop": "claude-desktop",
        "oh_my_pi": "omp",
        "ohmypi": "omp",
        "vs_code": "vscode",
        "visualstudiocode": "vscode",
    }
    return aliases.get(name, name) if name else "unknown"


def agent_display(agent_name: str) -> str:
    """Human-readable display name for an agent."""
    key = normalize_agent(agent_name)
    info = KNOWN_AGENTS.get(key, {})
    return info.get("display", agent_name or "Unknown")


def agent_icon(agent_name: str) -> str:
    """Emoji icon for an agent."""
    key = normalize_agent(agent_name)
    return KNOWN_AGENTS.get(key, {}).get("icon", "❓")
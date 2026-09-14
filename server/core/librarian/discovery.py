"""Agent registry and discovery.

Split out of the single-file librarian (issue #98): this module owns the
known-agent table, config-file inspection and the "is levh connected?"
answer. No I/O beyond reading config files and PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def _has_levh(text: str) -> bool:
    t = text.lower()
    return '"levh"' in t or "[mcp_servers.levh]" in t or "levh.exe" in t or "levh mcp" in t


# Makineye kurulu / yapılandırılmış tüm ajanlar. value: (CLI adı, [config yolları]).
_ALL_AGENTS = {
    "cline": ("cline", [Path.home() / ".cline" / "mcp.json"]),
    "claude-code": ("claude", [Path.home() / ".claude.json",
                               Path.home() / ".claude-code" / "mcp.json"]),
    "codex": ("codex", [Path.home() / ".codex" / "config.toml"]),
    "opencode": ("opencode", [Path.home() / ".opencode" / "mcp.json"]),
    "opencodex": ("opencodex", [Path.home() / ".opencodex" / "mcp.json"]),
    "jcode": ("jcode", [Path.home() / ".jcode" / "mcp.json"]),
    # kilo'nun OKUDUĞU dosya ~/.config/kilo/kilo.json; diğer ikisi eski
    # şemadan kalma ve kilo onlara bakmıyor, yani oradaki bir levh girdisi
    # "bağlı" demek değil. Sıralama bilerek böyle: gerçek config önce.
    "kilo-code": ("kilocode", [Path.home() / ".config" / "kilo" / "kilo.json",
                               Path.home() / ".kilocode" / "mcp.json",
                               Path.home() / ".kilo" / "kilo.json"]),
    "oh-my-cli": ("oh-my-cli", [Path.home() / ".oh-my-cli" / "mcp.json"]),
    "gemini": ("gemini", [Path.home() / ".gemini" / "config" / "mcp_config.json"]),
    # hermes ve aider'ın MCP config şeması bilinmiyor / henüz config'i yok.
    "hermes": ("hermes", []),
    "aider": ("aider", []),
}


def discover_agents() -> list[dict]:
    """Makinedeki tüm ajan konfigürasyonlarını tara; levh bağlantısı var mı?"""
    return [describe_agent(name) for name in _ALL_AGENTS]


def discover_installed() -> list[dict]:
    """PATH'te kurulu + yapılandırılmış tüm ajanları bul; levh bağlantısı var mı?"""
    out = []
    for name, (exe, _configs) in _ALL_AGENTS.items():
        path = shutil.which(exe)
        out.append({"agent": name, "installed": bool(path), "path": path,
                    "levh_connected": describe_agent(name)["levh_connected"]})
    return out


def describe_agent(agent: str) -> dict:
    """Bir ajanın config dosyalarında levh bağlantısı var mı?"""
    entry = _ALL_AGENTS.get(agent)
    if not entry:
        return {"agent": agent, "levh_connected": False, "configs": []}
    _, configs = entry
    configs_read = []
    for cfg in configs:
        if cfg.is_file():
            try:
                text = cfg.read_text(encoding="utf-8-sig", errors="ignore")
                configs_read.append({"config": str(cfg),
                                     "levh_connected": _has_levh(text)})
            except OSError:
                configs_read.append({"config": str(cfg), "levh_connected": False})
        else:
            configs_read.append({"config": str(cfg), "levh_connected": False})
    any_connected = any(c["levh_connected"] for c in configs_read)
    return {"agent": agent, "levh_connected": any_connected,
            "configs": configs_read}

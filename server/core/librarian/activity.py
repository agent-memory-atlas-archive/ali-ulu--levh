"""Activity monitoring: per-source memory counts, silent agents, scan.

Split out of the single-file librarian (issue #98). A scan is the one-turn
read-only report the background loop and the chat context are built from.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from server.core.librarian.db import ro_conn

logger = logging.getLogger("levh.librarian")

# Ajanlar kendi adlarını tek biçimde yazmıyor: aynı Cline "cline",
# "cline-session" ve "Cline" olarak, Claude Code "claude-code" ve "Claude Code"
# olarak kaydediyor. Normalize edilmezse yazan bir ajan "sessiz" görünür —
# bekçinin tek işi buysa, yanlış alarm en pahalı çıktısıdır.
_SOURCE_ALIASES = {
    "cline-session": "cline",
    "claude code": "claude-code",
    "claudecode": "claude-code",
    "kilo": "kilo-code",
    "kilocode": "kilo-code",
    "oh-my-cli": "oh-my-cli",
}


def _normalize_source(source: str | None) -> str:
    key = (source or "").strip().lower()
    return _SOURCE_ALIASES.get(key, key)


def _silent_agents(per_source: dict) -> list[str]:
    """levh'e BAĞLI olduğu hâlde pencerede hiç yazmayan ajanlar.

    Bağlı olmayan bir ajanın sessizliği haber değil — o zaten "levh MCP yok"
    bulgusunun konusu. Haber, bağlanmış ama kullanılmayan ajan.

    ``describe_agent`` paket yüzeyinden (``server.core.librarian``) dinamik
    çözümlenir: testler orayı yamalıyor ve yama gerçek kullanım noktasına
    etki etmeli.
    """
    from server.core.librarian import discovery as _discovery
    from server.core import librarian as _pkg

    written = {
        _normalize_source(name)
        for name, count in per_source.items()
        if count
    }
    return [
        agent
        for agent in _discovery._ALL_AGENTS
        if _pkg.describe_agent(agent)["levh_connected"] and agent not in written
    ]


def _activity_report() -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        conn = ro_conn()
        try:
            per_source = dict(conn.execute(
                "SELECT source, COUNT(*) FROM memories WHERE created_at > ? GROUP BY source",
                (cutoff,),
            ).fetchall())
            held = conn.execute(
                "SELECT COUNT(*) FROM held_memories WHERE status='held'"
            ).fetchone()[0]
            last_mem = conn.execute("SELECT MAX(created_at) FROM memories").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("librarian activity query failed: %s", exc)
        return {"error": str(exc)}

    return {
        "window_hours": 24,
        "memories_per_source": per_source,
        "silent_agents": _silent_agents(per_source),
        "held_memories": held,
        "last_memory_at": last_mem,
    }


def scan() -> dict:
    """Tek tur keşif + izleme. Saf okuma: hiçbir şey yazmaz.

    Yazma işi çağırana ait (``record_findings``) ve çağıranın event loop'unda
    olur. Bu ayrım kasıtlı: tarama senkron olduğu için bir thread'de koşuyor,
    ve motorun paylaşılan SQLite bağlantısını o thread'den ikinci bir loop
    açarak sürmek — ``asyncio.run`` ile olsa bile — kaçınılması gereken şeydi.
    """
    from server.core.librarian.discovery import discover_agents

    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "agents": discover_agents(),
        "activity": _activity_report(),
    }

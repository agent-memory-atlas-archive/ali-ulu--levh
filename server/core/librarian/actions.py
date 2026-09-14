"""Action execution and reply parsing.

Split out of the single-file librarian (issue #98): running the model's
proposed actions (whitelist only), the memory-quality analysis those
actions read from, and the JSON action-block parser the chat loop uses.

Terminal authority lives nowhere in this package — a ``shell``-type action
is refused and the refusal itself is recorded as a finding.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from server.core.librarian.config import add_levh_mcp
from server.core.librarian.db import ro_conn
from server.core.librarian.findings import _record_one
from server.core.librarian.activity import scan

# Modelin önerebileceği aksiyonların TAMAMI. Beyaz liste, kara liste değil:
# tanınmayan her tip reddedilir, dolayısıyla yeni bir yetenek ancak buraya
# bilerek eklenerek doğar — modelin bir tip adı uydurmasıyla değil.
_ALLOWED_ACTIONS = {"add_levh_mcp", "report_finding", "none",
                    "analyze_memory", "suggest_cleanup", "memory_report", "check_connections"}


async def _memory_analysis() -> dict:
    """Hafıza kalitesi analizi — duplicate, importance dağılımı, tip dengesi."""
    try:
        conn = ro_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            type_dist = dict(conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall())
            importance_dist = dict(conn.execute(
                "SELECT CASE WHEN importance >= 0.7 THEN 'high' "
                "WHEN importance >= 0.4 THEN 'medium' ELSE 'low' END as tier, "
                "COUNT(*) FROM memories GROUP BY tier"
            ).fetchall())
            pinned = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE pinned = 1"
            ).fetchone()[0]
            held = conn.execute(
                "SELECT COUNT(*) FROM held_memories WHERE status='held'"
            ).fetchone()[0]
            dup_candidates = conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT content FROM memories "
                "  GROUP BY content HAVING COUNT(*) > 1"
                ")"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    return {"total": total, "type_distribution": type_dist,
            "importance_distribution": importance_dist,
            "pinned": pinned, "held": held,
            "duplicate_candidates": dup_candidates}


async def execute_action(action: dict) -> dict:
    """LLM'in önerdiği aksiyonu çalıştırır ve sonucu döner.

    Terminal yetkisi yoktur; ``shell`` gibi bir tip gelirse çalıştırılmaz,
    reddedilir ve reddin kendisi gelen kutusuna bulgu olarak düşer — modelin
    komut çalıştırmaya çalışması, kullanıcının görmesi gereken bir olaydır.
    """
    a_type = str(action.get("type", "none"))

    if a_type not in _ALLOWED_ACTIONS:
        await _record_one(
            title=f"Librarian izinsiz aksiyon denedi: {a_type}",
            detail=(
                f"Model '{a_type}' tipinde bir aksiyon onerdi; bu tip beyaz "
                f"listede degil, calistirilmadi.\nOneri: {json.dumps(action, ensure_ascii=False)[:1000]}"
            ),
            category="agent",
            severity="high",
        )
        return {"ok": False, "msg": f"izin verilmeyen aksiyon tipi: {a_type}"}

    if a_type == "add_levh_mcp":
        agent = str(action.get("agent", ""))
        result = await asyncio.to_thread(add_levh_mcp, agent)
        await _record_one(
            title=f"{agent}: levh MCP kaydi eklendi",
            detail=f"Sonuc: {result.get('msg', '')}",
            category="config",
            severity="low",
        )
        return result

    if a_type == "report_finding":
        return await _record_one(
            title=str(action.get("title", "")),
            detail=str(action.get("detail", "")),
            category=str(action.get("category", "other")),
            severity=str(action.get("severity", "medium")),
        )

    if a_type == "analyze_memory":
        return {"ok": True, "analysis": await _memory_analysis()}

    if a_type == "suggest_cleanup":
        data = await _memory_analysis()
        suggestions = []
        if isinstance(data, dict) and not data.get("error"):
            dups = data.get("duplicate_candidates", 0)
            if dups:
                suggestions.append(f"{dups} duplicate aday var")
            low_imp = data.get("importance_distribution", {}).get("low", 0)
            if low_imp:
                suggestions.append(f"{low_imp} düşük önemli kayıt")
            if not suggestions:
                suggestions.append("Temizlik gerekmiyor")
        return {"ok": True, "suggestions": suggestions, "analysis": data}

    if a_type == "memory_report":
        return {"ok": True, "report": await _memory_analysis()}

    if a_type == "check_connections":
        report = await asyncio.to_thread(scan)
        agents = report.get("agents", [])
        return {"ok": True,
                "connected": [a["agent"] for a in agents if a.get("levh_connected")],
                "disconnected": [a["agent"] for a in agents if not a.get("levh_connected") and a.get("configs")]}

    return {"ok": True, "msg": "aksiyon gerekmedi"}


_ACTION_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
# Bazen model sadece ham bir JSON nesnesi döner (fence yok). Hem bütün cevabın
# JSON'dan oluştuğu hem içinde JSON bloğu geçtiği durumları yakala.
_BARE_ACTION_RE = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)


def _parse_json_block(text: str) -> tuple[str, dict | None]:
    """Dönen metinden aksiyon JSON'unu ayıklar. Fenced ya da ham JSON.

    Sistem promptu modele açıklamayı JSON'un ``reply`` alanına yazmasını
    söylüyor; blok dışında metin kalmadığında kullanıcıya gösterilecek yanıt
    oradan alınır — yoksa modele tam uyan bir cevap boş baloncuk olarak
    görünüyordu.
    """
    match = _ACTION_RE.search(text)
    if not match:
        if _BARE_ACTION_RE.match(text):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "action" in parsed:
                    return str(parsed.get("reply", "") or "").strip(), parsed.get("action")
            except json.JSONDecodeError:
                pass
        return text.strip(), None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return text.strip(), None
    reply = (text[: match.start()] + text[match.end():]).strip()
    if not reply:
        reply = str(parsed.get("reply", "") or "").strip()
    return reply, parsed.get("action")


def _split_reply_and_action(text: str) -> tuple[str, dict | None]:
    return _parse_json_block(text)

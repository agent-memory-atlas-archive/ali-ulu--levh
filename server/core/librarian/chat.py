"""Librarian chat: question + live context → LLM → (run proposed action) → answer.

Split out of the single-file librarian (issue #98).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx

from server.core import llm_endpoint
from server.core.librarian.activity import scan
from server.core.librarian.actions import _split_reply_and_action, execute_action
from server.core.librarian.discovery import discover_installed

logger = logging.getLogger("levh.librarian")

# Prompt'a taşınan önceki mesaj sayısı (soru + cevap = 2 mesaj).
CHAT_HISTORY_TURNS = 20
_CHAT_HISTORY: list[dict] = []

_SYSTEM_PROMPT = (
    "Sen LEVH'in 'hafıza operatörüsün' — AI ajanlarının hafıza katmanını "
    "izleyen, analiz eden ve iyileştiren otonom bekçi ajanısın.\n\n"
    "TEMEL PRENSİPLER:\n"
    "- Kullanıcıya TÜRKÇE, KISA, NET, EYLEM ODAKLI yanıt ver.\n"
    "- SOR-SOR-VER değil, KENDİN ANALİZ YAP ve ÖNER.\n"
    "- Soruyu anlamadan generic cevap verme; anlamadıysa soruyu netleştir.\n"
    "- Veriyi olduğu gibi sun; uydurma bilgi ekleme.\n\n"
    "YETENEKLERİN (ne yapabileceğin):\n"
    "1. ANALİZ: Hafıza kalitesi (duplicate, fading, importance dağılımı, tip dengesi)\n"
    "2. TESPİT: Bozuk bağlantı, sessiz ajan, duplicate memory, unutulmuş önemli bilgi\n"
    "3. RAPOR: Kullanıcıya anlaşılır özet hazırla (JSON değil, düz metin)\n"
    "4. YÖNLENDİR: 'Şunu yapmalısın' de, adım adım açıkla\n"
    "5. İLETİŞİM: Kullanıcıyı findings'e yönet, karar ondan iste\n\n"
    "KOMUTLARI ANALİZ ET:\n"
    "- 'hafızamı analiz et' → duplicate, fading, importance dağılımını raporla\n"
    "- 'bağlantı sorunlarını göster' → hangi ajan bağlı/değil, neden\n"
    "- 'temizlik öner' → hangi memory'ler silinebilir/birleştirilebilir\n"
    "- 'rapor ver' → genel hafıza durumu özeti\n"
    "- 'X ajanını bağla' → add_levh_mcp aksiyonu öner\n\n"
    "AKSİYON (aksiyon gerekiyorsa yanıtının SONUNA ekle, yoksa sadece yaz):\n"
    "```json\n{\"action\": {\"type\": \"...\", ...}, \"reply\": \"Türkçe özet\"}\n```\n"
    "Geçerli tipler:\n"
    "- `add_levh_mcp` — agent: cline|codex|claude-code|opencode|opencodex|jcode|kilo-code|oh-my-cli|gemini\n"
    "- `report_finding` — title, detail, category: bug|config|memory|agent|other, severity: critical|high|medium|low\n"
    "- `none` — aksiyon gerekmiyorsa\n"
    "TERMINAL KOMUTU ÇALIŞTIRMA. 'shell', 'run', 'exec' önerme; reddedilir.\n"
    "reply alanını HER ZAMAN yaz; boş bırakma."
)


def _context_block() -> str:
    report = scan()
    report["installed_agents"] = discover_installed()
    return "CONTEXT:\n" + json.dumps(report, ensure_ascii=False, indent=1)


def _reset_clock(response) -> str:
    """Kotanın ne zaman sıfırlanacağı — sağlayıcının söylediği biçimden okunur."""
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:  # OpenRouter epoch'u milisaniye verir, bazıları saniye
            value = float(reset)
            if value > 1e11:
                value /= 1000.0
            local = datetime.fromtimestamp(value).strftime("%H:%M")
            return f" Sifirlanma: {local}."
        except (TypeError, ValueError):
            pass
    retry_after = response.headers.get("retry-after")
    if retry_after:
        return f" {retry_after} saniye sonra tekrar denenebilir."
    return ""


def _llm_failure_reply(exc: Exception, context: str) -> str:
    """Sağlayıcı hatasını kullanıcının ne yapacağını bilebileceği bir cümleye çevir.

    429 ham hâliyle ("Client error '429 Too Many Requests'"...) levh'te bir
    arıza varmış gibi görünüyordu; oysa istek sağlayıcıya ulaşıyor ve kota
    dolduğu için geri çevriliyor. Ne olduğu ve ne zaman geçeceği yazılır,
    ardından yine de işe yarayan tek şey — canlı bağlam — verilir.
    """
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("error", {}).get("message", "")).strip()
        except Exception:  # noqa: BLE001 — gövde JSON olmayabilir
            detail = ""
        head = "Model saglayicisi kotayi doldurdugumuzu soyluyor (429)."
        if detail:
            head += f" Saglayici: {detail}"
        return head + _reset_clock(response) + "\n" + context
    return f"LLM'e su an ulasamadim ({exc}).\n" + context


async def chat(question: str) -> dict:
    """Kullanıcı sorusu + canlı bağlam → LLM → (önerilen aksiyonu çalıştır) → yanıt."""
    # Tarama dosya sistemi ve SQLite'a gidiyor; bir thread'e alınmazsa
    # sorunun süresince tüm sunucunun event loop'unu bloke eder.
    context = await asyncio.to_thread(_context_block)
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        # Önceki turlar: sohbetin "hafızası" burada. Liste tutuluyor ama
        # isteğe hiç konmuyordu, yani her soru ilk soruymuş gibi cevaplanıyordu.
        *_CHAT_HISTORY,
        {"role": "user", "content": f"{context}\n\nSORU: {question}"},
    ]

    if not llm_endpoint.api_key():
        return {"answer": "LLM beyin ayarli degil (OPENAI_API_KEY yok).\n" + context,
                "backend": "offline", "actions": []}

    headers = {
        "Authorization": f"Bearer {llm_endpoint.api_key()}",
        "Content-Type": "application/json",
    }
    url = llm_endpoint.chat_completions_url()
    model = llm_endpoint.chat_model()

    executed: list[dict] = []
    reply = ""
    reached_model = True
    for _step in range(3):  # max 3 tur: düşün → aksiyon → sonuç → yanıt
        payload = {"model": model, "messages": messages, "temperature": 0.3}
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                reply = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001 — chat asla 500 dömesin
            logger.warning("librarian chat LLM failed: %s", exc)
            reply = _llm_failure_reply(exc, context)
            reached_model = False
            break

        reply_text, action = _split_reply_and_action(reply)
        if not action or action.get("type") == "none":
            reply = reply_text
            break

        result = await execute_action(action)
        executed.append({"action": action, "result": result})
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": (
            f"AKSIYON SONUCU: {json.dumps(result, ensure_ascii=False)}\n"
            "Bu sonucu kullaniciya Turkce ozetle; baska aksiyon gerekiyorsa "
            "yeni JSON blogunu ekle, gerekmiyorsa 'none' yaz."
        )})
        reply = reply_text

    if not reply.strip():
        # Sohbet penceresine boş baloncuk düşürmektense ne olduğunu yaz.
        if executed:
            reply = "Aksiyon calistirildi: " + json.dumps(
                executed[-1]["result"], ensure_ascii=False
            )
        else:
            reply = "Model bos yanit dondu; soruyu yeniden sorar misin?"

    _CHAT_HISTORY.append({"role": "user", "content": question})
    _CHAT_HISTORY.append({"role": "assistant", "content": reply})
    del _CHAT_HISTORY[:-CHAT_HISTORY_TURNS]
    return {
        "answer": reply,
        "backend": "llm" if reached_model else "offline",
        "actions": executed,
    }

"""Telegram Bot API client (pre-prod item 7).

Uses aiohttp directly — the project's single HTTP client (P0.11), no SDK. Two
jobs: verify a bot token and AUTO-DETECT the chat id (so the owner never types
it), and send alert messages. The owner's flow: create a bot via @BotFather,
send it any message once, paste the token, click Verify — getUpdates then
reveals the chat id from that message.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def verify_and_detect(token: str) -> dict:
    """getMe (validate the token) + getUpdates (find the chat id from the most
    recent message to the bot). Returns
    {ok, bot_username, chat_id} on success, else {ok: False, error, ...}."""
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "empty token"}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(_API.format(token=token, method="getMe")) as r:
                me = await r.json()
            if not me.get("ok"):
                return {"ok": False, "error": "invalid bot token"}
            bot_username = (me.get("result") or {}).get("username", "")
            async with s.get(_API.format(token=token, method="getUpdates")) as r:
                upd = await r.json()
    except Exception as exc:                       # network / DNS / timeout
        log.warning("telegram verify failed: %s", exc)
        return {"ok": False, "error": "could not reach Telegram"}

    chat_id = ""
    if upd.get("ok"):
        for u in reversed(upd.get("result", [])):  # newest message first
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                chat_id = str(chat["id"])
                break
    if not chat_id:
        return {"ok": False, "bot_username": bot_username,
                "error": "token valid, but no chat found — open your bot in "
                         "Telegram and send it any message, then Verify again."}
    return {"ok": True, "bot_username": bot_username, "chat_id": chat_id}


async def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send one HTML message. Never raises — returns False on any failure so a
    failed alert can never break the caller (feed/pipeline)."""
    if not (token and chat_id):
        return False
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.post(
                _API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            ) as r:
                data = await r.json()
        if not data.get("ok"):
            log.warning("telegram sendMessage rejected: %s", data.get("description"))
        return bool(data.get("ok"))
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
        return False

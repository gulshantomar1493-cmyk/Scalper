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


def chat_id_from_update(u: dict) -> str:
    """The chat id carried by ANY update type, not just a plain message.

    Only looking at `message`/`edited_message` was the reason a brand-new bot
    kept reporting "no chat found": pressing Start in some Telegram clients
    produces a `my_chat_member` update and nothing else, and a bot added to a
    group or channel produces `my_chat_member` / `channel_post`. The chat is
    right there in the response — it was just being ignored.
    """
    for key in ("message", "edited_message", "channel_post",
                "edited_channel_post", "my_chat_member", "chat_member"):
        chat = (u.get(key) or {}).get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    cb = u.get("callback_query") or {}
    chat = ((cb.get("message") or {}).get("chat")) or {}
    if chat.get("id") is not None:
        return str(chat["id"])
    return ""


async def verify_and_detect(token: str, chat_id: str = "") -> dict:
    """getMe (validate the token) + getUpdates (find the chat id).

    `chat_id`, when supplied, skips detection entirely — the escape hatch for
    the cases auto-detection genuinely cannot cover (a webhook is set, the
    message is older than Telegram's 24-hour update retention, or the target is
    a group the owner would rather name explicitly).

    Returns {ok, bot_username, chat_id} on success, else {ok: False, error}.
    """
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token:
        return {"ok": False, "error": "empty token"}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(_API.format(token=token, method="getMe")) as r:
                me = await r.json()
            if not me.get("ok"):
                return {"ok": False, "error": "invalid bot token"}
            bot_username = (me.get("result") or {}).get("username", "")
            if chat_id:                            # owner named the chat
                return {"ok": True, "bot_username": bot_username,
                        "chat_id": chat_id}
            async with s.get(_API.format(token=token, method="getUpdates")) as r:
                upd = await r.json()
            hook = {}
            if not upd.get("ok") or not upd.get("result"):
                # A set webhook makes getUpdates fail with 409, or a bot whose
                # updates go elsewhere simply returns nothing. Telling the owner
                # to "send a message" when the real problem is a webhook sends
                # them round a loop that can never succeed.
                async with s.get(_API.format(token=token,
                                             method="getWebhookInfo")) as r:
                    hook = (await r.json()).get("result") or {}
    except Exception as exc:                       # network / DNS / timeout
        log.warning("telegram verify failed: %s", exc)
        return {"ok": False, "error": "could not reach Telegram"}

    if not upd.get("ok"):
        why = upd.get("description") or "getUpdates failed"
        log.warning("telegram getUpdates rejected for @%s: %s", bot_username, why)
        return {"ok": False, "bot_username": bot_username,
                "error": f"Telegram ne getUpdates mana kar diya: {why}"}

    for u in reversed(upd.get("result") or []):    # newest first
        found = chat_id_from_update(u)
        if found:
            return {"ok": True, "bot_username": bot_username, "chat_id": found}

    if hook.get("url"):
        return {"ok": False, "bot_username": bot_username,
                "error": f"Is bot pe webhook laga hai ({hook['url']}) — saare "
                         "updates wahan ja rahe hain, isliye chat detect nahi "
                         "ho sakta. Webhook hatao, ya chat id neeche daal do."}
    return {"ok": False, "bot_username": bot_username,
            "error": "Token sahi hai, par is bot ko koi message nahi mila. "
                     "Telegram mein @" + (bot_username or "bot") + " kholo, "
                     "START dabao ya koi bhi message bhejo, phir Verify karo. "
                     "(Telegram 24 ghante se purane message nahi dikhata — "
                     "purana message bheja tha to naya bhejo.)"}


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

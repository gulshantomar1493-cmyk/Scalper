"""Telegram bot verification — the chat-detection path.

Pure: aiohttp is replaced by a fake that serves canned Bot API responses, so
these run with no network and no real token.

The bug these exist to prevent: verification only ever looked at `message` and
`edited_message` updates and swallowed whatever Telegram actually said, so a
brand-new bot reported "no chat found — send it a message" whether the owner
had sent one or not. Following that advice could never fix a webhook, and a
`my_chat_member` update carrying the chat id right there was thrown away.
"""

from __future__ import annotations

import pytest

from marketscalper import telegram
from marketscalper.telegram import chat_id_from_update, verify_and_detect


# ------------------------------------------------------------ the fake API --

class _Resp:
    def __init__(self, payload):
        self._p = payload

    async def json(self):
        return self._p

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Serves a canned payload per Bot API method and records what was called."""

    def __init__(self, by_method):
        self._by = by_method
        self.calls = []

    def _route(self, url):
        method = url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append(method)
        return _Resp(self._by.get(method, {"ok": False, "description": "unmocked"}))

    def get(self, url, **kw):
        return self._route(url)

    def post(self, url, **kw):
        return self._route(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def api(monkeypatch):
    """Install a fake Bot API. Returns a setter the test fills in."""
    holder = {}

    def session(*a, **kw):
        holder["session"] = _Session(holder["by_method"])
        return holder["session"]

    monkeypatch.setattr(telegram.aiohttp, "ClientSession", session)

    def configure(**by_method):
        holder["by_method"] = by_method
        return holder

    return configure


_ME = {"ok": True, "result": {"username": "TradeOsQ_bot"}}


def _update(kind, chat_id):
    return {"update_id": 1, kind: {"chat": {"id": chat_id, "type": "private"}}}


# ---------------------------------------------------- what carries a chat --

@pytest.mark.parametrize("kind", [
    "message", "edited_message", "channel_post", "edited_channel_post",
    "my_chat_member", "chat_member",
])
def test_every_update_type_that_carries_a_chat_is_recognised(kind):
    """Pressing Start on a fresh bot can produce ONLY a my_chat_member update.
    Ignoring it was the difference between "connected" and an error telling the
    owner to do the thing they had just done."""
    assert chat_id_from_update(_update(kind, 12345)) == "12345"


def test_a_callback_query_carries_its_message_chat():
    assert chat_id_from_update({
        "callback_query": {"message": {"chat": {"id": -100200}}}}) == "-100200"


def test_an_update_with_no_chat_yields_nothing():
    assert chat_id_from_update({"poll": {"id": "x"}}) == ""
    assert chat_id_from_update({}) == ""


# ------------------------------------------------------------ verification --

async def test_a_my_chat_member_update_is_enough_to_connect(api):
    api(getMe=_ME,
        getUpdates={"ok": True, "result": [_update("my_chat_member", 555)]})
    out = await verify_and_detect("t")
    assert out["ok"] is True
    assert out["chat_id"] == "555" and out["bot_username"] == "TradeOsQ_bot"


async def test_the_newest_chat_wins(api):
    api(getMe=_ME, getUpdates={"ok": True, "result": [
        _update("message", 111), _update("message", 222)]})
    assert (await verify_and_detect("t"))["chat_id"] == "222"


async def test_a_bad_token_is_reported_as_a_bad_token(api):
    api(getMe={"ok": False, "description": "Unauthorized"})
    out = await verify_and_detect("nope")
    assert out["ok"] is False and out["error"] == "invalid bot token"


async def test_a_webhook_is_named_instead_of_blaming_the_owner(api):
    """getUpdates cannot work while a webhook is set. Telling the owner to send
    a message sends them round a loop that can never succeed."""
    api(getMe=_ME, getUpdates={"ok": True, "result": []},
        getWebhookInfo={"ok": True, "result": {"url": "https://example.com/hook"}})
    out = await verify_and_detect("t")
    assert out["ok"] is False
    assert "webhook" in out["error"].lower()
    assert "example.com/hook" in out["error"]


async def test_telegrams_own_rejection_is_surfaced_not_swallowed(api):
    api(getMe=_ME,
        getUpdates={"ok": False, "description": "Conflict: terminated by other getUpdates"},
        getWebhookInfo={"ok": True, "result": {}})
    out = await verify_and_detect("t")
    assert out["ok"] is False
    assert "Conflict" in out["error"], "the real reason must reach the screen"


async def test_no_messages_says_what_to_do_and_names_the_bot(api):
    api(getMe=_ME, getUpdates={"ok": True, "result": []},
        getWebhookInfo={"ok": True, "result": {}})
    out = await verify_and_detect("t")
    assert out["ok"] is False
    assert "TradeOsQ_bot" in out["error"]
    assert "24" in out["error"], "Telegram forgets older updates — say so"


async def test_a_supplied_chat_id_skips_detection_entirely(api):
    """The escape hatch for the cases detection cannot cover. It must not even
    call getUpdates — a webhook would make that fail for no reason."""
    api(getMe=_ME)
    out = await verify_and_detect("t", "  -1001234567890 ")
    assert out["ok"] is True and out["chat_id"] == "-1001234567890"


async def test_a_supplied_chat_id_still_requires_a_valid_token(api):
    api(getMe={"ok": False})
    assert (await verify_and_detect("t", "123"))["ok"] is False


async def test_a_network_failure_never_raises(api, monkeypatch):
    def boom(*a, **kw):
        raise OSError("dns")
    monkeypatch.setattr(telegram.aiohttp, "ClientSession", boom)
    out = await verify_and_detect("t")
    assert out["ok"] is False and "reach Telegram" in out["error"]


async def test_an_empty_token_is_rejected_without_a_request(api):
    api(getMe=_ME)
    assert (await verify_and_detect("   "))["ok"] is False


# ------------------------------------------- the webhook, named and fixable --

async def test_a_webhook_explains_a_409_not_just_telegrams_english(api):
    """Telegram's own words are correct but incomplete: "use deleteWebhook" does
    not mention that the webhook belongs to some other service."""
    api(getMe=_ME,
        getUpdates={"ok": False, "error_code": 409,
                    "description": "Conflict: can't use getUpdates method while "
                                   "webhook is active; use deleteWebhook to delete "
                                   "the webhook first"},
        getWebhookInfo={"ok": True, "result": {"url": "https://other.app/hook"}})
    out = await verify_and_detect("t")
    assert out["ok"] is False
    assert out["webhook"] == "https://other.app/hook"      # the UI keys its fix off this
    assert "other.app/hook" in out["error"]
    assert "chat id neeche khud daal do" in out["error"]   # the safe way out, first
    assert "band ho jaayegi" in out["error"]               # and what deleting costs


async def test_the_webhook_wins_the_explanation_over_an_empty_result(api):
    api(getMe=_ME, getUpdates={"ok": True, "result": []},
        getWebhookInfo={"ok": True, "result": {"url": "https://x.test/h"}})
    out = await verify_and_detect("t")
    assert out.get("webhook") == "https://x.test/h"


async def test_no_webhook_means_no_fix_button(api):
    """The offer must not appear when deleting a webhook would fix nothing."""
    api(getMe=_ME, getUpdates={"ok": True, "result": []},
        getWebhookInfo={"ok": True, "result": {}})
    out = await verify_and_detect("t")
    assert "webhook" not in out


async def test_delete_webhook_reports_success_and_failure_honestly(api):
    from marketscalper.telegram import delete_webhook
    api(deleteWebhook={"ok": True, "result": True})
    assert (await delete_webhook("t"))["ok"] is True

    api(deleteWebhook={"ok": False, "description": "Unauthorized"})
    out = await delete_webhook("t")
    assert out["ok"] is False and out["error"] == "Unauthorized"


async def test_delete_webhook_never_raises_on_a_network_failure(monkeypatch):
    from marketscalper.telegram import delete_webhook
    def boom(*a, **kw):
        raise OSError("dns")
    monkeypatch.setattr(telegram.aiohttp, "ClientSession", boom)
    out = await delete_webhook("t")
    assert out["ok"] is False and "reach Telegram" in out["error"]

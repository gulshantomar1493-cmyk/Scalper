"""Unit tests for the alert router (pre-prod items 6/7/8).

Pure — a settings double + a monkeypatched Telegram send. No DB, no network.
Verifies the gating: an alert fires only when the channel is on, the bot is
verified, and that alert type is enabled.
"""

from __future__ import annotations

import asyncio

from marketscalper import telegram
from marketscalper.alerts import Alerter


class _Settings:
    def __init__(self, notifications: dict, tg: dict) -> None:
        self._n, self._t = notifications, tg

    def notifications(self) -> dict:
        return dict(self._n)

    def telegram(self) -> dict:
        return dict(self._t)

    def telegram_targets(self) -> list:
        t = self._t
        if t.get("verified") and t.get("token") and t.get("chat_id"):
            return [(t["token"], t["chat_id"])]
        return []


def _configured(n_over=None, t_over=None) -> _Settings:
    n = {"telegram": True, "trade_alerts": True, "system_alerts": True,
         "desktop": True, "push": False}
    t = {"token": "T", "chat_id": "C", "bot_username": "b", "verified": True}
    n.update(n_over or {})
    t.update(t_over or {})
    return _Settings(n, t)


async def _capture(monkeypatch):
    sent = []

    async def fake(token, chat_id, text):
        sent.append((token, chat_id, text))
        return True

    monkeypatch.setattr(telegram, "send_message", fake)
    return sent


_REC = {"verdict": "A_PLUS", "direction": "LONG", "strategy": "S1",
        "score": 88, "entry": 100, "sl": 90, "tp1": 120}


async def test_trade_setup_sends_when_configured(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured()).trade_setup("BTCUSDT", _REC)
    await asyncio.sleep(0.02)
    assert len(sent) == 1
    tok, chat, text = sent[0]
    assert tok == "T" and chat == "C"
    assert "BTCUSDT" in text and "LONG" in text and "88/100" in text
    assert "HIGH-CONVICTION" in text                     # A_PLUS wording


async def test_no_send_when_telegram_channel_off(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(n_over={"telegram": False})).trade_setup("BTCUSDT", _REC)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_no_send_when_not_verified(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(t_over={"verified": False})).trade_setup("BTCUSDT", _REC)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_no_send_when_token_or_chat_missing(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(t_over={"chat_id": ""})).trade_setup("BTCUSDT", _REC)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_trade_toggle_gates_setups_but_not_system(monkeypatch):
    sent = await _capture(monkeypatch)
    a = Alerter(_configured(n_over={"trade_alerts": False}))  # trade off, system on
    a.trade_setup("BTCUSDT", _REC)
    a.feed_down()
    await asyncio.sleep(0.02)
    assert len(sent) == 1 and "Feed disconnected" in sent[0][2]


async def test_system_toggle_gates_feed_alerts(monkeypatch):
    sent = await _capture(monkeypatch)
    a = Alerter(_configured(n_over={"system_alerts": False}))
    a.feed_down()
    a.feed_up()
    await asyncio.sleep(0.02)
    assert sent == []

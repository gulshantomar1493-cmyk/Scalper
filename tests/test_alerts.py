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


_SETUP = {"strategy_id": "eth_4h_core", "symbol": "BTCUSDT",
          "direction_label": "LONG", "filters_passed": 3,
          "entry": 67200.0, "stop": 66100.0, "target": 78200.0, "rr": 9.62}


async def test_trade_setup_sends_when_configured(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured()).trade_setup("BTCUSDT", _SETUP)
    await asyncio.sleep(0.02)
    assert len(sent) == 1
    tok, chat, text = sent[0]
    assert tok == "T" and chat == "C"
    assert "BTCUSDT" in text and "LONG" in text and "eth_4h_core" in text
    assert "3/3" in text                                 # named rule count
    assert "HIGH-CONVICTION" in text                     # all filters passed
    assert "%" not in text                               # never a fake confidence


async def test_no_send_when_telegram_channel_off(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(n_over={"telegram": False})).trade_setup("BTCUSDT", _SETUP)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_no_send_when_not_verified(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(t_over={"verified": False})).trade_setup("BTCUSDT", _SETUP)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_no_send_when_token_or_chat_missing(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured(t_over={"chat_id": ""})).trade_setup("BTCUSDT", _SETUP)
    await asyncio.sleep(0.02)
    assert sent == []


async def test_trade_toggle_gates_setups_but_not_system(monkeypatch):
    sent = await _capture(monkeypatch)
    a = Alerter(_configured(n_over={"trade_alerts": False}))  # trade off, system on
    a.trade_setup("BTCUSDT", _SETUP)
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


# ------------------------------------------------------- what the phone says --
# These arrive on a phone, often at night. "TP" is a status code; "TARGET HIT"
# is what happened. And a bare UTC time on an Indian trader's phone is a puzzle.

def test_a_close_alert_names_the_exit_in_words_not_a_status_code():
    from marketscalper.alerts import Alerter as A
    assert A._EXIT["TP"][1] == "TARGET HIT"
    assert A._EXIT["SL"][1] == "STOP HIT"
    assert "TIME EXIT" in A._EXIT["TIME"][1]


async def test_target_and_stop_alerts_carry_prices_result_and_an_ist_time(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured()).trade_closed("ETHUSDT", {
        "strategy_id": "eth_1h_fast", "direction": 1, "status": "TP",
        "net_r": 9.41, "hold_minutes": 96,
        "fill_price": 3140.5, "exit_price": 3480.0, "closed_ts": 1_753_700_000})
    await asyncio.sleep(0.02)
    text = sent[0][2]
    assert "TARGET HIT" in text
    assert "3,140.50" in text and "3,480.00" in text     # entry -> exit, both shown
    assert "+9.41R" in text
    assert "IST" in text


async def test_a_stop_alert_is_never_dressed_up_as_a_win(monkeypatch):
    sent = await _capture(monkeypatch)
    Alerter(_configured()).trade_closed("BTCUSDT", {
        "strategy_id": "btc_4h_core", "direction": -1, "status": "SL",
        "net_r": -1.09, "hold_minutes": 42})
    await asyncio.sleep(0.02)
    text = sent[0][2]
    assert "STOP HIT" in text and "-1.09R" in text
    assert "TARGET" not in text


async def test_the_trigger_alert_says_the_trade_is_now_active(monkeypatch):
    """This is the message that tells the owner a recommendation has become a
    position — the moment it moves sections in the UI."""
    sent = await _capture(monkeypatch)
    Alerter(_configured()).trade_triggered("ETHUSDT", {
        "strategy_id": "eth_4h_wide", "direction": 1, "entry": 3100.0,
        "fill_price": 3101.5, "stop": 3040.0, "target": 3700.0,
        "filled_ts": 1_753_700_000})
    await asyncio.sleep(0.02)
    text = sent[0][2]
    assert "ACTIVE" in text and "3,101.50" in text
    assert "IST" in text


def test_ist_renders_the_indian_wall_clock_not_utc():
    from marketscalper.alerts import ist
    # 2025-07-28 12:16 UTC -> 17:46 IST (+5:30)
    assert ist(1_753_705_000) == "28 Jul 17:46 IST"
    assert ist(None).endswith("IST")
    assert ist("not a time") == "—"

"""Unit tests for the runtime settings store (pre-prod items 7/8).

Pure — a temp file, no DB, no network.
"""

from __future__ import annotations

from marketscalper.settings_store import SettingsStore


def _store(tmp_path):
    return SettingsStore(path=tmp_path / "settings.json")


def test_defaults_when_no_file(tmp_path):
    s = _store(tmp_path)
    n = s.notifications()
    assert n["desktop"] is True and n["telegram"] is True
    assert n["trade_alerts"] is True and n["system_alerts"] is True
    tg = s.telegram_public()
    assert tg == {"bot_username": "", "chat_id": "", "verified": False, "has_token": False}


def test_set_notifications_persists_and_coerces(tmp_path):
    s = _store(tmp_path)
    out = s.set_notifications({"desktop": False, "trade_alerts": 0, "unknown": True})
    assert out["desktop"] is False and out["trade_alerts"] is False
    assert "unknown" not in out                         # unknown keys ignored
    # reload from disk -> persisted
    assert SettingsStore(path=tmp_path / "settings.json").notifications()["desktop"] is False


def test_telegram_public_masks_token(tmp_path):
    s = _store(tmp_path)
    s.set_telegram(token="123:SECRET", chat_id="999", bot_username="mybot", verified=True)
    pub = s.telegram_public()
    assert pub == {"bot_username": "mybot", "chat_id": "999",
                   "verified": True, "has_token": True}
    assert "token" not in pub                           # never exposed
    assert s.telegram()["token"] == "123:SECRET"        # internal read still has it
    # persisted across reload
    s2 = SettingsStore(path=tmp_path / "settings.json")
    assert s2.telegram()["token"] == "123:SECRET"


def test_clear_telegram(tmp_path):
    s = _store(tmp_path)
    s.set_telegram(token="123:SECRET", chat_id="999", bot_username="mybot", verified=True)
    s.clear_telegram()
    assert s.telegram_public()["has_token"] is False
    assert s.telegram_public()["verified"] is False


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json", encoding="utf-8")
    s = SettingsStore(path=tmp_path / "settings.json")
    assert s.notifications()["desktop"] is True         # graceful default

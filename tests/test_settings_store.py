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
    assert pub["bot_username"] == "mybot" and pub["chat_id"] == "999"
    assert pub["verified"] is True and pub["has_token"] is True
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


def test_multiple_bots_fan_out_and_remove(tmp_path):
    """Multiple verified bots all receive alerts (telegram_targets); each can be
    removed independently by id; a legacy single object migrates into the list."""
    s = _store(tmp_path)
    b1 = s.add_telegram_bot(token="A:1", chat_id="111", bot_username="alpha",
                            verified=True, label="phone")
    b2 = s.add_telegram_bot(token="B:2", chat_id="222", bot_username="beta",
                            verified=True, label="group")
    s.add_telegram_bot(token="C:3", chat_id="333", bot_username="gamma", verified=False)
    # every VERIFIED bot is a target; the unverified one is excluded
    targets = s.telegram_targets()
    assert ("A:1", "111") in targets and ("B:2", "222") in targets
    assert ("C:3", "333") not in targets and len(targets) == 2
    assert b1["id"] != b2["id"]                          # distinct ids
    # re-verify same token updates in place (no duplicate)
    s.add_telegram_bot(token="A:1", chat_id="111b", bot_username="alpha", verified=True)
    assert len(s.telegram_bots_public()) == 3
    # remove one by id; persists across reload
    assert s.remove_telegram_bot(b2["id"]) is True
    assert s.remove_telegram_bot(9999) is False          # unknown id
    ids = [b["id"] for b in SettingsStore(path=tmp_path / "settings.json").telegram_bots_public()]
    assert b2["id"] not in ids and b1["id"] in ids


def test_legacy_single_telegram_migrates(tmp_path):
    import json
    (tmp_path / "settings.json").write_text(json.dumps({
        "telegram": {"token": "OLD:1", "chat_id": "77", "bot_username": "old", "verified": True}
    }), encoding="utf-8")
    s = SettingsStore(path=tmp_path / "settings.json")
    assert s.telegram_targets() == [("OLD:1", "77")]     # migrated into the list
    assert s.telegram_public()["bot_username"] == "old"


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json", encoding="utf-8")
    s = SettingsStore(path=tmp_path / "settings.json")
    assert s.notifications()["desktop"] is True         # graceful default

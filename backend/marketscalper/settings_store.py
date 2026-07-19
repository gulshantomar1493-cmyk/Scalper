"""Runtime settings store (pre-prod items 7/8; multi-bot Telegram).

The owner configures Telegram + notification preferences from the UI at runtime,
so these can't live in the git-committed config chain. They live in one small
JSON file (``MARKETSCALPER_SETTINGS_FILE``, default ``backend/runtime_settings.json``,
git-ignored), read at startup and rewritten atomically when the UI saves.

Multiple Telegram bots are supported: alerts fan out to EVERY verified bot at
the same time (owner request), so the owner can route to several chats/groups /
family devices at once. Each bot's token is a SECRET — kept only in this mode-600
file, never in git and (deliberately) not in the database, so it stays out of
pg_dump backups. A legacy single ``telegram`` object from an older file is
migrated into the bot list transparently.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    # list of {id, token, chat_id, bot_username, verified, label}
    "telegram_bots": [],
    "notifications": {
        "desktop": True, "push": False, "telegram": True,
        "trade_alerts": True, "system_alerts": True,
    },
}


def _default_path() -> Path:
    env = os.environ.get("MARKETSCALPER_SETTINGS_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "runtime_settings.json"


def _norm_bot(b: dict, bot_id: int) -> dict:
    return {
        "id": bot_id,
        "token": str(b.get("token", "")),
        "chat_id": str(b.get("chat_id", "")),
        "bot_username": str(b.get("bot_username", "")),
        "verified": bool(b.get("verified", False)),
        "label": str(b.get("label", "")),
    }


def _public(b: dict) -> dict:
    """A bot WITHOUT its token — safe to expose over GET /settings."""
    return {
        "id": b["id"], "bot_username": b["bot_username"], "chat_id": b["chat_id"],
        "verified": bool(b["verified"]), "has_token": bool(b["token"]),
        "label": b["label"],
    }


_EMPTY_PUBLIC = {"bot_username": "", "chat_id": "", "verified": False, "has_token": False}


class SettingsStore:
    """Load-once, save-on-change settings. Single process, single user — no
    locking beyond atomic file replace."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_path()
        self._data = self._load()

    def _load(self) -> dict:
        merged = copy.deepcopy(DEFAULTS)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return merged
        raw = raw or {}
        n = raw.get("notifications")
        if isinstance(n, dict):
            for k, v in n.items():
                if k in merged["notifications"]:
                    merged["notifications"][k] = v
        bots: list = []
        used: set = set()

        def _add(b: dict) -> None:                      # keep the stored id stable
            bid = b.get("id")
            if not isinstance(bid, int) or bid in used:
                bid = (max(used) + 1) if used else 1
            used.add(bid)
            bots.append(_norm_bot(b, bid))

        for b in (raw.get("telegram_bots") or []):
            if isinstance(b, dict) and b.get("token"):
                _add(b)
        # migrate a legacy single {"telegram": {...}} object from an older file
        legacy = raw.get("telegram")
        if isinstance(legacy, dict) and legacy.get("token"):
            if not any(x["token"] == legacy["token"] for x in bots):
                _add(legacy)
        merged["telegram_bots"] = bots
        return merged

    def _save(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)          # atomic within a filesystem
            try:
                os.chmod(self._path, 0o600)      # token secrecy (POSIX)
            except OSError:
                pass
        except OSError as exc:
            log.error("settings save failed: %s", exc)

    def _next_id(self) -> int:
        ids = [b["id"] for b in self._data["telegram_bots"] if isinstance(b.get("id"), int)]
        return (max(ids) + 1) if ids else 1

    # ---- reads ----
    def notifications(self) -> dict:
        return dict(self._data["notifications"])

    def telegram_bots_public(self) -> list:
        return [_public(b) for b in self._data["telegram_bots"]]

    def telegram_targets(self) -> list:
        """Every (token, chat_id) that should receive alerts — verified bots
        only. Alerts fan out to ALL of these at the same time."""
        return [(b["token"], b["chat_id"]) for b in self._data["telegram_bots"]
                if b["verified"] and b["token"] and b["chat_id"]]

    # legacy single-bot views (the first bot) — kept for backward compatibility
    def telegram(self) -> dict:
        bots = self._data["telegram_bots"]
        if not bots:
            return {"token": "", "chat_id": "", "bot_username": "", "verified": False}
        b = bots[0]
        return {"token": b["token"], "chat_id": b["chat_id"],
                "bot_username": b["bot_username"], "verified": b["verified"]}

    def telegram_public(self) -> dict:
        bots = self._data["telegram_bots"]
        return _public(bots[0]) if bots else dict(_EMPTY_PUBLIC)

    # ---- writes ----
    def set_notifications(self, prefs: dict) -> dict:
        for k in self._data["notifications"]:
            if k in prefs:
                self._data["notifications"][k] = bool(prefs[k])
        self._save()
        return self.notifications()

    def add_telegram_bot(self, *, token: str, chat_id: str,
                         bot_username: str, verified: bool, label: str = "") -> dict:
        """Add a bot (or update the existing one with the same token — a
        re-verify). Returns the bot's public view."""
        for b in self._data["telegram_bots"]:
            if b["token"] == token:                    # re-verify the same bot
                b.update(chat_id=chat_id, bot_username=bot_username,
                         verified=bool(verified))
                if label:
                    b["label"] = label
                self._save()
                return _public(b)
        bot = _norm_bot({"token": token, "chat_id": chat_id,
                         "bot_username": bot_username, "verified": verified,
                         "label": label}, self._next_id())
        self._data["telegram_bots"].append(bot)
        self._save()
        return _public(bot)

    def remove_telegram_bot(self, bot_id: int) -> bool:
        before = len(self._data["telegram_bots"])
        self._data["telegram_bots"] = [
            b for b in self._data["telegram_bots"] if b["id"] != bot_id]
        removed = len(self._data["telegram_bots"]) != before
        if removed:
            self._save()
        return removed

    # legacy single-bot writes — replace the whole list with one bot / clear all
    def set_telegram(self, *, token: str, chat_id: str,
                     bot_username: str, verified: bool) -> None:
        self._data["telegram_bots"] = [_norm_bot(
            {"token": token, "chat_id": chat_id, "bot_username": bot_username,
             "verified": verified}, 1)]
        self._save()

    def clear_telegram(self) -> None:
        self._data["telegram_bots"] = []
        self._save()

"""Runtime settings store (pre-prod items 7/8).

The owner configures Telegram + notification preferences from the UI at runtime,
so these can't live in the git-committed config chain. They live in one small
JSON file (``MARKETSCALPER_SETTINGS_FILE``, default ``backend/runtime_settings.json``,
git-ignored), read at startup and rewritten atomically when the UI saves.

The Telegram bot token is a SECRET — kept only in this mode-600 file, never in
git and (deliberately) not in the database, so it stays out of pg_dump backups.
Plain file, no schema/migration — the minimal thing that works for one user.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    "telegram": {"token": "", "chat_id": "", "bot_username": "", "verified": False},
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
        for section, vals in (raw or {}).items():
            if section in merged and isinstance(vals, dict):
                for k, v in vals.items():
                    if k in merged[section]:
                        merged[section][k] = v
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

    # ---- reads ----
    def notifications(self) -> dict:
        return dict(self._data["notifications"])

    def telegram(self) -> dict:
        return dict(self._data["telegram"])

    def telegram_public(self) -> dict:
        """Telegram config WITHOUT the token (safe for GET /settings)."""
        t = self._data["telegram"]
        return {
            "bot_username": t["bot_username"], "chat_id": t["chat_id"],
            "verified": bool(t["verified"]), "has_token": bool(t["token"]),
        }

    # ---- writes ----
    def set_notifications(self, prefs: dict) -> dict:
        for k in self._data["notifications"]:
            if k in prefs:
                self._data["notifications"][k] = bool(prefs[k])
        self._save()
        return self.notifications()

    def set_telegram(self, *, token: str, chat_id: str,
                     bot_username: str, verified: bool) -> None:
        self._data["telegram"] = {
            "token": token, "chat_id": chat_id,
            "bot_username": bot_username, "verified": bool(verified),
        }
        self._save()

    def clear_telegram(self) -> None:
        self._data["telegram"] = copy.deepcopy(DEFAULTS["telegram"])
        self._save()

"""Alert routing (pre-prod items 6/7/8).

Formats trade-setup / feed / error alerts and sends them to enabled channels.
Telegram is the BACKEND channel — it works even when the browser is closed
(desktop/PWA notifications are the frontend's job, driven from the live stream).

Live-only: composed in main(), never in replay or tests, so it can never affect
determinism. Sends are fire-and-forget (asyncio.create_task) so a slow Telegram
API can never stall the feed or the analysis pipeline. Every gate (channel on?
token verified? this alert type enabled?) is re-read from the settings store, so
UI toggle changes take effect immediately without a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from marketscalper import telegram

log = logging.getLogger(__name__)

#: The owner trades from India and reads these on a phone. A bare UTC time on a
#: message that arrives at 3am is a puzzle, not information.
IST = timezone(timedelta(hours=5, minutes=30))


def ist(ts=None) -> str:
    """A timestamp the reader can act on without doing arithmetic."""
    if ts is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return "—"
    return dt.astimezone(IST).strftime("%d %b %H:%M IST")


class Alerter:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._tasks: set = set()          # keep refs so tasks aren't GC'd early

    def _enabled(self, key: str) -> bool:
        """Per-event switch from the Settings page, re-read every send so a
        toggle takes effect without a restart."""
        try:
            return bool(self._settings.alerts().get(key, True))
        except Exception:
            return True

    def proximity_pct(self) -> float:
        try:
            return float(self._settings.alerts().get("proximity_pct", 0.25))
        except Exception:
            return 0.25

    def _telegram_targets(self, kind: str) -> list:
        """Every (token, chat_id) that should receive this alert kind — ALL
        verified bots, so alerts fan out to every configured chat/device at
        once. kind is 'trade' or 'system'."""
        prefs = self._settings.notifications()
        if not prefs.get("telegram"):
            return []
        gate = "trade_alerts" if kind == "trade" else "system_alerts"
        if not prefs.get(gate, True):
            return []
        return list(self._settings.telegram_targets())

    def _send(self, kind: str, text: str) -> None:
        targets = self._telegram_targets(kind)
        if not targets:
            return
        try:
            for token, chat_id in targets:            # all bots at the same time
                task = asyncio.create_task(telegram.send_message(token, chat_id, text))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except RuntimeError:              # no running loop (shouldn't happen live)
            log.debug("alerter: no running loop, dropped %s alert", kind)

    # ---- alert types ----
    def setup_approaching(self, symbol: str, setup: dict, price: float, away_pct: float) -> None:
        """Price is closing on a resting entry. THE alert that matters: these
        orders sit for hours, so "it is coming" is actionable and "it filled"
        is just news."""
        if not self._enabled("on_approach"):
            return
        long_ = int(setup.get("direction", 1)) > 0
        self._send("trade", "\n".join([
            "<b>⏳ APPROACHING ENTRY</b>",
            f"<b>{symbol}</b> {'LONG' if long_ else 'SHORT'} — {setup.get('strategy_id')}",
            f"Price {price:,.2f} is {away_pct:.2f}% from the entry "
            f"{float(setup['entry']):,.2f}",
            f"Stop {float(setup['stop']):,.2f} · Target {float(setup['target']):,.2f}"
            f" · {setup.get('rr')}R net",
            ist(),
            "",
            "<i>Decision-support only — place any order manually.</i>",
        ]))

    def trade_triggered(self, symbol: str, row: dict) -> None:
        """The level broke and the resting order filled — this is now a LIVE
        position, and it moves out of the setups list into active trades."""
        if not self._enabled("on_trigger"):
            return
        long_ = int(row.get("direction", 1)) > 0
        self._send("trade", "\n".join([
            "<b>▶ ENTRY TRIGGERED — trade ab ACTIVE hai</b>",
            f"<b>{symbol}</b> {'LONG' if long_ else 'SHORT'} — {row.get('strategy_id')}",
            f"Filled {float(row.get('fill_price') or row['entry']):,.2f}",
            f"Stop {float(row['stop']):,.2f} · Target {float(row['target']):,.2f}",
            ist(row.get("filled_ts")),
        ]))

    #: How the exit is described. The status code alone ('TP') does not tell a
    #: half-awake reader whether they made money.
    _EXIT = {
        "TP": ("🎯", "TARGET HIT"),
        "SL": ("🛑", "STOP HIT"),
        "TIME": ("⏱", "TIME EXIT — 3 din ka horizon pura"),
    }

    def trade_closed(self, symbol: str, row: dict) -> None:
        if not self._enabled("on_close"):
            return
        net = row.get("net_r") or 0.0
        status = str(row.get("status", "")).upper()
        icon, headline = self._EXIT.get(status, ("•", f"CLOSED — {status}"))
        hold = row.get("hold_minutes")
        lines = [
            f"<b>{icon} {headline}</b>",
            f"<b>{symbol}</b> {'LONG' if int(row.get('direction', 1)) > 0 else 'SHORT'}"
            f" — {row.get('strategy_id')}",
        ]
        if row.get("fill_price") is not None and row.get("exit_price") is not None:
            lines.append(f"Entry {float(row['fill_price']):,.2f} → "
                         f"exit {float(row['exit_price']):,.2f}")
        lines += [
            f"Result <b>{net:+.2f}R</b> net of fees and funding",
            f"Held {hold} min" if hold is not None else "",
            ist(row.get("closed_ts")),
        ]
        self._send("trade", "\n".join(x for x in lines if x))

    def trade_setup(self, symbol: str, setup: dict) -> None:
        """One alert per V4 setup the recorder accepts. `filters_passed` is a
        named rule count, never a confidence percentage."""
        if not self._enabled("on_new_setup"):
            return
        passed = setup.get("filters_passed")
        high = passed is not None and passed >= 3
        title = "🚀 HIGH-CONVICTION SETUP" if high else "📈 Trade Setup"
        text = (
            f"<b>{title}</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Direction: <b>{setup.get('direction_label')}</b>\n"
            f"Strategy: {setup.get('strategy_id')}\n"
            f"Filters passed: {passed}/3\n"
            f"Entry: {setup.get('entry')}\n"
            f"Stop: {setup.get('stop')}\n"
            f"Target: {setup.get('target')}  (net R:R {setup.get('rr')})\n"
            f"{ist(setup.get('decision_ts'))}\n\n"
            f"<i>Decision-support only — place any order manually on your exchange.</i>"
        )
        self._send("trade", text)

    def feed_down(self) -> None:
        self._send("system", "⚠️ <b>Feed disconnected</b> — MarketScalper lost the "
                             "Binance data feed. Auto-reconnect is running.")

    def feed_up(self) -> None:
        self._send("system", "✅ <b>Feed reconnected</b> — MarketScalper is "
                             "receiving market data again.")

    def error(self, message: str) -> None:
        self._send("system", f"❌ <b>Critical error</b>\n{message}")

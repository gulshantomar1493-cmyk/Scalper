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

from marketscalper import telegram

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._tasks: set = set()          # keep refs so tasks aren't GC'd early

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
    def trade_setup(self, symbol: str, rec: dict) -> None:
        high = rec.get("verdict") == "A_PLUS"
        title = "🚀 HIGH-CONVICTION SETUP" if high else "📈 Trade Setup"
        text = (
            f"<b>{title}</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Direction: <b>{rec.get('direction')}</b>\n"
            f"Strategy: {rec.get('strategy')}\n"
            f"Confidence: {rec.get('score')}/100 ({rec.get('verdict')})\n"
            f"Entry: {rec.get('entry')}\n"
            f"Stop: {rec.get('sl')}\n"
            f"Target: {rec.get('tp1')}\n\n"
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

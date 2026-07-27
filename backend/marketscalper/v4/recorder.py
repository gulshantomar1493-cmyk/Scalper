"""V4 recorder — the live loop that turns setups into a tracked record.

Runs ONLY in live composition (never in replay/tests, so determinism is untouched).
Every cycle:
  1. ask the service for current setups on every enabled strategy
  2. INSERT the new ones (idempotent on setup_key)
  3. advance every OPEN row against 1m candles and persist any terminal outcome

Error doctrine: a DB or data failure is logged and counted; the loop never dies
and never takes the feed or API down with it.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import config as C
from .outcome import advance, TERMINAL
from .service import to_arrays

log = logging.getLogger(__name__)

INTERVAL_S = 60


class V4Recorder:
    def __init__(self, service, store, chart_service, *, alerter=None):
        self._svc = service
        self._store = store
        self._chart = chart_service
        self._alerter = alerter            # live-only; None in replay/tests
        self.cycles = 0
        self.recorded = 0
        self.resolved = 0
        self.errors = 0

    async def _bars_1m(self, symbol: str, since_ts: int) -> dict | None:
        """1m candles from just before `since_ts` to now — the execution tape."""
        try:
            start = datetime.fromtimestamp(since_ts, tz=timezone.utc) - timedelta(minutes=5)
            end = datetime.now(timezone.utc)
            if start >= end:
                return None
            payload = await self._chart.get_chart(symbol, "1m", start, end)
            candles = payload.get("candles", [])
            if len(candles) < 2:
                return None
            return to_arrays(candles, "1m")
        except Exception as exc:
            log.warning("v4 recorder: 1m fetch failed for %s: %s", symbol, exc)
            return None

    async def cycle(self) -> None:
        # --- 1/2. discover and record --------------------------------------
        try:
            fresh = await self._svc.all_setups()
        except Exception as exc:
            self.errors += 1
            log.warning("v4 recorder: setup build failed: %s", exc)
            fresh = []
        if fresh:
            new = await self._store.record(fresh)
            if new:
                self.recorded += len(new)
                log.info("v4: recorded %d new setup(s)", len(new))
                if self._alerter is not None:      # one alert per NEW setup
                    for setup in new:
                        self._alerter.trade_setup(setup["symbol"], setup)

        # --- 3. advance open rows ------------------------------------------
        try:
            open_rows = await self._store.open_setups()
        except Exception as exc:
            self.errors += 1
            log.warning("v4 recorder: open query failed: %s", exc)
            return

        # Fetch ONE tape per symbol, starting at the OLDEST open row for that
        # symbol. open_setups() is ordered newest-first, so keying off the first
        # row would give a window that starts AFTER older rows began — they could
        # then never fill or resolve. Compute the minimum explicitly.
        oldest: dict[str, int] = {}
        for row in open_rows:
            sym = row["symbol"]
            ts = int(row["decision_ts"])
            if sym not in oldest or ts < oldest[sym]:
                oldest[sym] = ts

        tape: dict[str, dict] = {}
        for sym, since in oldest.items():
            b = await self._bars_1m(sym, since)
            if b is not None:
                tape[sym] = b

        for row in open_rows:
            bars = tape.get(row["symbol"])
            if bars is None:
                continue
            setup = _RowSetup(row)
            try:
                out = advance(setup, bars)
            except Exception as exc:
                self.errors += 1
                log.warning("v4 recorder: advance failed for %s: %s", row.get("setup_key"), exc)
                continue
            if out.status in TERMINAL:
                if await self._store.apply_outcome(row["setup_key"], out):
                    self.resolved += 1
                    log.info("v4: %s -> %s (%.3fR)", row["setup_key"], out.status,
                             out.net_r if out.net_r is not None else 0.0)
        self.cycles += 1

    async def run(self) -> None:
        log.info("v4 recorder started (%d strategies, %ds cycle)",
                 sum(1 for s in C.STRATEGIES if self._svc.is_enabled(s)), INTERVAL_S)
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # belt and braces
                self.errors += 1
                log.warning("v4 recorder cycle error: %s", exc)
            await asyncio.sleep(INTERVAL_S)


class _RowSetup:
    """Adapts a stored row to the shape `outcome.advance` expects."""
    __slots__ = ("direction", "entry", "stop", "target", "decision_ts")

    def __init__(self, row: dict):
        self.direction = int(row["direction"])
        self.entry = float(row["entry"])
        self.stop = float(row["stop"])
        self.target = float(row["target"])
        self.decision_ts = int(row["decision_ts"])

"""MarketScalper entrypoint — the composition root (roadmap P0.27).

The ONLY place components are constructed and wired together, with plain
config-driven selection (Architecture Part-D rule; no DI framework, no
plugins). No business logic lives here.

Wiring (all components pre-existing):
    EventBus · StateStore (constructed BEFORE the app so bus ordering makes
    state_diff contain the announced candle) · CandleBuilder · CandleWriter ·
    KlineReconciler (built candles via a bus subscription; reference klines
    via BinanceFeed's explicit callback) · ClockOffsetSampler · FastAPI app
    (ReplayFeed injected as the replay provider) · uvicorn server.

Launch settings come from the environment (config layer 3, per D3):
    MARKETSCALPER_API_TOKEN  required — refuses to start without it
    MARKETSCALPER_API_HOST   default 127.0.0.1
    MARKETSCALPER_API_PORT   default 8000
    MARKETSCALPER_FEED       default "binance" (provider selection, Part D)
plus the existing config chain (symbols, DB DSN, logging).

Lifecycle duties owned by the composition root (Decision D2): ensure candle
partitions at startup and after each UTC midnight.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime, timedelta, timezone
from math import exp

import uvicorn

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.config import Config, load_config
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.candle_writer import CandleWriter
from marketscalper.core.reconciler import KlineReconciler
from marketscalper.core.state import StateStore
from marketscalper.engines.liquidity import LiquidityEngine, SweepEvent
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import (BosDetector, ChochDetector,
                                             PivotDetector, PivotLabeler,
                                             TrendState)
from marketscalper.engines.trendline import TrendlineBook, TrendlineDetector
from marketscalper.logging_setup import setup_logging
from marketscalper.providers.base import Candle
from marketscalper.providers.binance import BinanceFeed, ClockOffsetSampler
from marketscalper.providers.replay import ReplayFeed

log = logging.getLogger(__name__)

_FEEDS = {"binance": BinanceFeed}  # provider selection map (Part D: plain config)


class _StructurePipeline:
    """One symbol's 1m analysis chain (P1.19 composition, frozen engines,
    pinned cadence). Publishes a JSON-ready payload into the StateStore
    after every closed 1m candle; the existing WS diff carries it (§9).
    R1: no pool — engines persist nothing in Phase 1. 5m engine instances
    arrive with their first consumer (P2, A8)."""

    _PIVOTS_SHOWN = 30      # marker history depth in the payload
    _EVENTS_SHOWN = 10      # BOS/CHOCH label history depth

    def __init__(self, symbol: str, store: StateStore) -> None:
        self._symbol = symbol
        self._store = store
        self._atr = IncrementalATR()
        self._detector = PivotDetector(symbol, "1m")
        self._labeler = PivotLabeler()
        self._trend = TrendState()
        self._bos = BosDetector(self._trend, self._atr)
        self._choch = ChochDetector(self._trend)
        self._tl_detector = TrendlineDetector(self._atr)
        self._book = TrendlineBook(self._tl_detector, self._atr)
        self._liq = LiquidityEngine(symbol, self._atr)
        self._detector_5m = PivotDetector(symbol, "5m")   # first 5m consumer:
        self._labeler_5m = PivotLabeler()                 # A8 range (D12.6)
        self._pivots: deque = deque(maxlen=self._PIVOTS_SHOWN)
        self._bos_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._choch_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._sweep_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._shift_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._bar = -1          # positional axis, lockstep with the engines
        # Freeze-audit fix: the reconnect path can emit a stale pre-gap
        # bucket AFTER its backfilled successors (accepted D7 residual).
        # The engines assume chronological candles, so the composition
        # drops out-of-order candles here — one guard for every engine.
        self._last_ts = None
        self._last_ts_5m = None

    def step(self, candle: Candle) -> None:
        """The pinned per-closed-candle cadence, engines in §1 order."""
        if self._last_ts is not None and candle.ts <= self._last_ts:
            log.warning("engines: dropped out-of-order 1m candle %s %s "
                        "(last %s)", self._symbol, candle.ts, self._last_ts)
            return
        self._last_ts = candle.ts
        self._bar += 1
        self._atr.update(candle)
        self._tl_detector.update(candle)
        for pivot in self._detector.update(candle):
            labeled = self._labeler.label(pivot)
            self._pivots.append(labeled)
            self._trend.on_pivot(labeled)
            self._bos.on_pivot(labeled)
            self._choch.on_pivot(labeled)
            self._tl_detector.on_pivot(labeled)
            self._liq.on_pivot(labeled)
        self._trend.update(candle)
        bos_event = self._bos.update(candle)
        if bos_event is not None:
            self._bos_events.append(bos_event)
            self._choch.on_bos(bos_event)
        choch_event = self._choch.update(candle)
        if choch_event is not None:
            self._choch_events.append(choch_event)
        self._book.refresh(candle)
        if choch_event is not None:                # D12.7: CHOCH before liq
            self._liq.on_choch(choch_event)
        for event in self._liq.update(candle):
            if isinstance(event, SweepEvent):
                self._sweep_events.append(event)
            else:
                self._shift_events.append(event)
        self._store.set_structure(self._symbol, self._payload(candle))

    def step_5m(self, candle: Candle) -> None:
        """5m closed candle: pivots feed the A8 external range (D12.6)."""
        if self._last_ts_5m is not None and candle.ts <= self._last_ts_5m:
            log.warning("engines: dropped out-of-order 5m candle %s %s "
                        "(last %s)", self._symbol, candle.ts, self._last_ts_5m)
            return
        self._last_ts_5m = candle.ts
        for pivot in self._detector_5m.update(candle):
            self._liq.on_external_pivot(self._labeler_5m.label(pivot))

    def _payload(self, candle: Candle) -> dict:
        """Everything the overlays draw — pre-serialized, no frontend math
        beyond rendering (line endpoints are projected here)."""
        cur = self._bar
        lines = []
        for line in self._book.active:
            lines.append({
                "side": line.side, "touches": line.touches,
                "x1": line.a_pivot.ts.isoformat(), "y1": line.a_pivot.price,
                "x2": candle.ts.isoformat(),
                "y2": exp(line.intercept + line.slope * (cur - line.a_index)),
            })
        channels = []
        for ch in self._book.channels():
            start_index = max(ch.support.a_index, ch.resistance.a_index)
            start_pivot = (ch.support.a_pivot
                           if ch.support.a_index >= ch.resistance.a_index
                           else ch.resistance.a_pivot)
            channels.append({
                "x1": start_pivot.ts.isoformat(),
                "y1": exp(ch.mid_value(start_index)),
                "x2": candle.ts.isoformat(),
                "y2": exp(ch.mid_value(cur)),
            })
        return {
            "trend": self._trend.state,
            "pivots": [{"ts": p.ts.isoformat(), "kind": p.kind,
                        "price": p.price, "label": p.label}
                       for p in self._pivots],
            "bos": [{"ts": e.ts.isoformat(), "direction": e.direction,
                     "close": e.close, "displacement": e.displacement}
                    for e in self._bos_events],
            "choch": [{"ts": e.ts.isoformat(), "direction": e.direction,
                       "close": e.close}
                      for e in self._choch_events],
            "trendlines": lines,
            "channels": channels,
            "liquidity": {
                "pools": [{"kind": p.kind, "price": p.price, "size": p.size,
                           "strength": p.strength}
                          for p in self._liq.pools],
                "levels": self._liq.key_levels,
                "premium_discount": self._liq.premium_discount,
                "sweeps": [{"ts": e.ts.isoformat(), "side": e.side,
                            "target": e.target, "price": e.target_price}
                           for e in self._sweep_events],
                "shifts": [{"sweep_ts": e.sweep.ts.isoformat(),
                            "ts": e.ts.isoformat()}
                           for e in self._shift_events],
            },
        }


def _wire_structure_engines(bus: EventBus, store: StateStore,
                            symbols) -> None:
    """P1.19: subscribe the per-symbol pipelines to closed 1m candles.
    Must be wired AFTER the StateStore and BEFORE create_app so the WS
    broadcast's diff already contains the just-computed structure."""
    pipelines = {symbol: _StructurePipeline(symbol, store)
                 for symbol in symbols}

    async def on_candle(candle: Candle) -> None:
        pipeline = pipelines.get(candle.symbol)
        if pipeline is None:
            return
        if candle.tf == "1m":
            pipeline.step(candle)
        elif candle.tf == "5m":
            pipeline.step_5m(candle)

    bus.subscribe(Candle, on_candle)


def main() -> int:
    config = load_config()
    setup_logging(level=config.app.log_level, log_dir=config.app.log_dir)

    token = os.environ.get("MARKETSCALPER_API_TOKEN", "")
    if not token:
        log.error("MARKETSCALPER_API_TOKEN is not set — refusing to start (D3)")
        return 2
    if not config.database.dsn:
        log.error("database DSN is not configured — refusing to start")
        return 2
    feed_name = os.environ.get("MARKETSCALPER_FEED", "binance")
    if feed_name not in _FEEDS:
        log.error("unknown feed provider %r (available: %s)",
                  feed_name, ", ".join(_FEEDS))
        return 2

    host = os.environ.get("MARKETSCALPER_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MARKETSCALPER_API_PORT", "8000"))

    log.info(
        "MarketScalper starting — decision support only (never executes trades); "
        "feed=%s symbols=%s api=%s:%d",
        feed_name, ",".join(config.symbols), host, port,
    )
    asyncio.run(_run(config, _FEEDS[feed_name], token, host, port))
    log.info("MarketScalper stopped")
    return 0


async def _run(config: Config, feed_cls, token: str, host: str, port: int) -> None:
    pool = await db.create_pool(config.database.dsn)
    async with pool.acquire() as conn:
        created = await db.ensure_partitions(conn)         # D2: startup
        log.info("partitions ensured at startup (%d created)", created)

    bus = EventBus()
    store = StateStore(bus)                                # before create_app
    CandleBuilder(bus)
    CandleWriter(bus, pool)
    reconciler = KlineReconciler()

    async def to_built(candle: Candle) -> None:            # truth 1m -> reconciler
        if candle.tf == "1m":
            reconciler.on_built(candle)

    bus.subscribe(Candle, to_built)
    _wire_structure_engines(bus, store, config.symbols)    # P1.19

    feed = feed_cls(config.symbols, bus,
                    on_reference_candle=reconciler.on_reference)
    sampler = ClockOffsetSampler()
    app = create_app(bus, store, pool, token, replay_provider=ReplayFeed)

    await feed.start()
    await sampler.start()
    rollover = asyncio.create_task(_midnight_partitions(pool), name="partition-rollover")

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    # The composition root owns process lifecycle: route SIGTERM/SIGINT to a
    # graceful uvicorn stop so serve() returns and cleanup below runs (without
    # this, uvicorn restores the default handler and re-raises the captured
    # signal after shutdown, killing the process before cleanup — exit -15).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, server.handle_exit, sig, None)
        except NotImplementedError:  # Windows dev machine — no asyncio signal
            break                    # handlers; uvicorn falls back to
                                     # signal.signal() itself (Ctrl+C only)
    try:
        await server.serve()                               # until SIGTERM/SIGINT
    finally:
        rollover.cancel()
        await asyncio.gather(rollover, return_exceptions=True)
        await sampler.stop()
        await feed.stop()
        await pool.close()


async def _midnight_partitions(pool) -> None:
    """D2: re-ensure partitions just after each UTC midnight."""
    while True:
        now = datetime.now(tz=timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds() + 60)
        try:
            async with pool.acquire() as conn:
                created = await db.ensure_partitions(conn)
            log.info("partitions ensured at UTC rollover (%d created)", created)
        except Exception as exc:                            # keep the loop alive
            log.error("partition rollover failed: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())

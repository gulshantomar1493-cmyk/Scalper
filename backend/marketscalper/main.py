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
from datetime import datetime, timedelta, timezone

import uvicorn

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.config import Config, load_config
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.candle_writer import CandleWriter
from marketscalper.core.reconciler import KlineReconciler
from marketscalper.core.state import StateStore
from marketscalper.logging_setup import setup_logging
from marketscalper.providers.base import Candle
from marketscalper.providers.binance import BinanceFeed, ClockOffsetSampler
from marketscalper.providers.replay import ReplayFeed

log = logging.getLogger(__name__)

_FEEDS = {"binance": BinanceFeed}  # provider selection map (Part D: plain config)


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

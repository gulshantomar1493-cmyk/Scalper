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
from marketscalper.core.chart_service import ChartService
from marketscalper.core.live_bar import LiveBarTracker
from marketscalper.core.reconciler import KlineReconciler
from marketscalper.core.state import StateStore
from marketscalper.settings_store import SettingsStore
from marketscalper.alerts import Alerter
from marketscalper.ops import (FEED_WATCHDOG_INTERVAL_S, feed_gap_alerts,
                               format_daily_summary)
from marketscalper.logging_setup import setup_logging
from marketscalper.providers.base import Candle
from marketscalper.providers.binance import BinanceFeed, ClockOffsetSampler

log = logging.getLogger(__name__)

_FEEDS = {"binance": BinanceFeed}  # provider selection map (Part D: plain config)


def _row_to_candle(r) -> Candle:
    """Stored candle row -> normalized Candle (D19.2 seed reads)."""
    return Candle(
        symbol=r["symbol"], tf=r["tf"], ts=r["ts"],
        o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
        v=float(r["v"]), qv=float(r["qv"]),
        n_trades=r["n_trades"], taker_buy_v=float(r["taker_buy_v"]),
    )


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
    started_at = datetime.now(tz=timezone.utc)         # uptime for GET /ops
    pool = await db.create_pool(config.database.dsn)
    async with pool.acquire() as conn:
        created = await db.ensure_partitions(conn)         # D2: startup
        log.info("partitions ensured at startup (%d created)", created)

    bus = EventBus()
    store = StateStore(bus)                                # before create_app
    CandleBuilder(bus)
    CandleWriter(bus, pool)
    live_bar = LiveBarTracker(bus)                         # display-only forming
                                                          # candle (live-only; no
                                                          # engine subscribes to it)
    reconciler = KlineReconciler()

    async def to_built(candle: Candle) -> None:            # truth 1m -> reconciler
        if candle.tf == "1m":
            reconciler.on_built(candle)

    bus.subscribe(Candle, to_built)
    sampler = ClockOffsetSampler()                 # clock-drift health log
    # D19.2 (owner-approved): seed the RVOL buckets from the 20 days
    # preceding the stream start — composition owns the read, the engine
    # stays database-unaware. Empty history -> unseeded warm-up.
    seed_end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    seed_start = seed_end - timedelta(days=20)
    seed_candles: dict[str, list[Candle]] = {}
    async with pool.acquire() as conn:
        for symbol in config.symbols:
            rows = await db.select_candles(conn, symbol, "1m",
                                           seed_start, seed_end)
            seed_candles[symbol] = [_row_to_candle(r) for r in rows]
            log.info("volume seed: %s — %d candles [%s .. %s)",
                     symbol, len(rows), seed_start, seed_end)
    settings = SettingsStore()                     # items 7/8 (live-only)
    alerter = Alerter(settings)                    # items 6/7 (Telegram, live-only)
    feed = feed_cls(config.symbols, bus,
                    on_reference_candle=reconciler.on_reference)
    # D33: seed the feed's last-stored 1m candle ts (from the DB) so the first
    # connect backfills the restart teardown gap AND bridges the connect-minute
    # -> the bus stream stays contiguous across a restart (no G1-poisoning gap,
    # no DB hole). Live only; ReplayFeed has no such method.
    if hasattr(feed, "prime_last_closed"):
        feed.prime_last_closed(
            {sym: cs[-1].ts for sym, cs in seed_candles.items() if cs})
    # D26 multi-timeframe ChartService: read-only, isolated from the engine bus.
    # The live feed is injected only as the gap-fill provider (fetches canonical
    # 1m; ChartService itself imports no concrete provider — P0.19).
    chart_service = ChartService(pool, provider=feed)
    from marketscalper.v4.service import V4Service          # validated strategy layer
    from marketscalper.v4.store import V4Store
    from marketscalper.v4.recorder import V4Recorder
    v4_service = V4Service(chart_service, settings=settings)
    v4_store = V4Store(pool)
    v4_recorder = V4Recorder(v4_service, v4_store, chart_service,
                             alerter=alerter)   # items 6/7: notify on new setups
    # Username/password login (single-user tool): credentials live in the env
    # (git-ignored .env). None set -> /login answers 503 and the frontend falls
    # back to a token in the URL (dev). The API token stays the data-route gate.
    auth_user = os.environ.get("MARKETSCALPER_AUTH_USER", "")
    auth_password = os.environ.get("MARKETSCALPER_AUTH_PASSWORD", "")
    app = create_app(bus, store, pool, token,
                     auth_user=auth_user, auth_password=auth_password,
                     chart_service=chart_service,             # D26 (Phase 1)
                     feed_status=lambda: feed.connected,      # GET /ops (items 3/5/9)
                     started_at=started_at, ops_symbols=config.symbols,
                     settings=settings,                       # items 7/8 (live)
                     live_price=live_bar.current_price,       # Paper V2 B4: live fills
                     v4_service=v4_service,                   # V4 validated strategies
                     v4_store=v4_store)                       # V4 history/performance

    await feed.start()
    await sampler.start()
    rollover = asyncio.create_task(_daily_ops(pool, v4_store), name="daily-ops")
    watchdog = asyncio.create_task(                    # P4.13 feed-gap alert
        _feed_gap_watchdog(store, config.symbols), name="feed-gap-watchdog")
    feed_alerts = asyncio.create_task(                 # items 6/7 feed up/down
        _feed_alert_watcher(feed, alerter), name="feed-alerts")
    v4_history = asyncio.create_task(                  # V4 validated-strategy recorder
        v4_recorder.run(), name="v4-recorder")

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
        watchdog.cancel()
        feed_alerts.cancel()
        v4_history.cancel()
        await asyncio.gather(rollover, watchdog, feed_alerts,
                             v4_history, return_exceptions=True)
        await sampler.stop()
        await feed.stop()
        await pool.close()


async def _v4_performance(v4_store) -> dict:
    """The same read-model GET /api/v4/performance serves, for the daily log."""
    from marketscalper.v4.outcome import performance_report
    return performance_report(await v4_store.query(limit=5000))


async def _daily_ops(pool, v4_store) -> None:
    """D2 partition re-ensure + P4.13 daily stats snapshot just after each
    UTC midnight."""
    while True:
        now = datetime.now(tz=timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds() + 60)
        try:
            async with pool.acquire() as conn:
                created = await db.ensure_partitions(conn)
            log.info("partitions ensured at UTC rollover (%d created)", created)
            log.info(format_daily_summary(await _v4_performance(v4_store)))
        except Exception as exc:                            # keep the loop alive
            log.error("daily ops failed: %s", exc)


async def _feed_gap_watchdog(store, symbols) -> None:
    """P4.13: alert when a symbol's closed 1m candle stream stalls (feed
    outage / stale connection). Logs a structured ALERT; never mutates
    state — the reconnect/backfill machinery (P0.10/P0.15) does the healing."""
    while True:
        await asyncio.sleep(FEED_WATCHDOG_INTERVAL_S)
        now = datetime.now(tz=timezone.utc)
        last_seen = {}
        for symbol in symbols:
            state = store.snapshot(symbol)
            candle = state.last_candle_1m if state is not None else None
            last_seen[symbol] = candle.ts if candle is not None else None
        for symbol, gap in feed_gap_alerts(last_seen, now):
            log.warning("ALERT feed gap: %s — no closed 1m candle for %.0fs",
                        symbol, gap)


async def _feed_alert_watcher(feed, alerter) -> None:
    """Items 6/7: notify (Telegram) when the Binance feed transitions down/up —
    works even with no browser open. Polls the feed's connected flag; the
    initial state is seeded so a startup connect never fires a spurious alert."""
    prev = None
    while True:
        await asyncio.sleep(15)
        try:
            now_connected = bool(feed.connected)
        except Exception:
            continue
        if prev is not None and now_connected != prev:
            (alerter.feed_up if now_connected else alerter.feed_down)()
        prev = now_connected


if __name__ == "__main__":
    raise SystemExit(main())

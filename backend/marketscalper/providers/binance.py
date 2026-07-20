"""BinanceFeed — live Binance market-data provider (roadmap P0.10).

One responsibility: receive live Binance market data over one combined
WebSocket stream, normalize every message into the P0.9 dataclasses, publish
them on the EventBus. Raw Binance JSON never leaves this module.

Streams per symbol (combined endpoint, single socket):
    <symbol>@aggTrade   -> Trade
    <symbol>@kline_1m   -> Candle (ONLY closed klines, x == true — the A1
                           reconciliation reference; unclosed updates ignored)
    <symbol>@bookTicker -> BookTicker (payload carries no timestamp; arrival
                           time in UTC is used — live-only stream, never replayed)

Behavioral policy (this implementation's, per the P0.9 boundary):
    reconnect — on any socket error/close, retry with exponential backoff
        (1s doubling to a 30s cap); a connection that lived >= 60s resets
        the backoff to 1s.
    heartbeat — a watchdog force-closes the socket if no message arrives
        for 30s (bookTicker traffic makes real silence abnormal); the closed
        socket routes into the same reconnect path.

Historical fetch (roadmap P0.15): fetch_historical_candles() via Binance
REST /api/v3/klines — the single HTTP client (aiohttp) used directly;
paginated; [start, end) by candle open time; ascending;
supports_historical_data is now honestly True.

Gap-safe reconnect backfill (roadmap P0.15; §4.1 stale-state poison
prevention): on every (re)connect, BEFORE reading live messages, the
missing closed 1m candles since the last closed kline seen per symbol are
fetched via fetch_historical_candles() and published in chronological
order. First connection (no previous candle) -> no backfill (bootstrap is
P0.16). Reconnect within the same minute -> empty gap, no fetch. No
buffered-message assumptions and no deduplication here — duplicates remain
the reconciliation flow's responsibility (P0.14).

Clock-offset sampler (roadmap P0.11, Decision D6/A12): samples local-vs-
Binance wall-clock offset via GET /api/v3/time every 5 minutes using the
project's single HTTP client (aiohttp, used directly — no wrappers).
G1 consumes offset_s / in_sync at P3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import websockets

from marketscalper.core.bus import EventBus
from marketscalper.providers.base import (
    BookTicker,
    Candle,
    Capabilities,
    FeedProvider,
    Trade,
)

log = logging.getLogger(__name__)

WS_BASE = "wss://stream.binance.com:9443/stream?streams="
STREAM_KINDS = ("aggTrade", "kline_1m", "bookTicker")

REST_BASE = "https://api.binance.com"
TIME_ENDPOINT = "/api/v3/time"
KLINES_ENDPOINT = "/api/v3/klines"
KLINES_LIMIT = 1000                        # Binance maximum rows per request
_TF_MS = {"1m": 60_000, "5m": 300_000}     # supported intervals (frozen 1m/5m)
CLOCK_SAMPLE_INTERVAL_S = 300.0   # D6/A12: every 5 minutes
CLOCK_OFFSET_LIMIT_S = 2.0        # D6/A12: G1 threshold |offset| > 2s
CLOCK_FAILURE_LIMIT = 3           # consecutive failures -> offset unknown

BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 30.0
STABLE_RESET_S = 60.0            # connection older than this resets backoff
HEARTBEAT_TIMEOUT_S = 30.0       # silence longer than this forces reconnect
HEARTBEAT_CHECK_INTERVAL_S = 5.0
BRIDGE_SETTLE_S = 2.0            # D33: settle after a minute closes before REST


# ----------------------------------------------------------- pure helpers
# No I/O below — unit-testable normalization and policy math.


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def parse_agg_trade(d: dict) -> Trade:
    """Binance aggTrade payload -> normalized Trade (§4.1 semantics)."""
    return Trade(
        symbol=d["s"],
        price=float(d["p"]),
        qty=float(d["q"]),
        # f/l = first/last raw trade ID aggregated into this event; the raw
        # count keeps candle n_trades matching the official kline "n" (D5).
        n_trades=int(d["l"]) - int(d["f"]) + 1,
        ts=_utc(d["T"]),
        is_buyer_maker=bool(d["m"]),
    )


def parse_book_ticker(d: dict, ts: datetime) -> BookTicker:
    """Binance bookTicker payload -> normalized BookTicker.

    The payload has no timestamp field; the caller supplies arrival time.
    """
    return BookTicker(
        symbol=d["s"],
        bid_px=float(d["b"]),
        bid_qty=float(d["B"]),
        ask_px=float(d["a"]),
        ask_qty=float(d["A"]),
        ts=ts,
    )


def parse_closed_kline(d: dict) -> Candle | None:
    """Binance kline payload -> normalized Candle for CLOSED klines, else None."""
    k = d["k"]
    if not k.get("x"):
        return None
    return Candle(
        symbol=k["s"],
        tf=k["i"],
        ts=_utc(k["t"]),                 # candle open time, UTC
        o=float(k["o"]),
        h=float(k["h"]),
        l=float(k["l"]),
        c=float(k["c"]),
        v=float(k["v"]),                 # base volume
        qv=float(k["q"]),                # quote volume
        n_trades=int(k["n"]),
        taker_buy_v=float(k["V"]),       # taker buy base volume
    )


def normalize_message(msg: dict, now: datetime) -> Trade | Candle | BookTicker | None:
    """Combined-stream envelope {'stream': ..., 'data': ...} -> normalized
    event, or None (unclosed kline / unknown stream)."""
    stream = msg.get("stream", "")
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    kind = stream.partition("@")[2]
    if kind == "aggTrade":
        return parse_agg_trade(data)
    if kind == "bookTicker":
        return parse_book_ticker(data, now)
    if kind.startswith("kline"):
        return parse_closed_kline(data)
    return None


def stream_url(symbols: list[str] | tuple[str, ...]) -> str:
    """Combined-stream URL for all symbols × the three required streams."""
    streams = "/".join(
        f"{s.lower()}@{kind}" for s in symbols for kind in STREAM_KINDS
    )
    return WS_BASE + streams


def next_backoff(current_s: float) -> float:
    """Exponential backoff step: double, capped at BACKOFF_CAP_S."""
    return min(current_s * 2, BACKOFF_CAP_S)


def compute_offset_s(server_time_ms: int, sent_at_s: float, received_at_s: float) -> float:
    """D6/A12 offset math: server time minus the local request midpoint.

    The midpoint of send/receive wall-clock instants neutralizes network
    latency; positive result = server clock ahead of local clock.
    """
    midpoint = (sent_at_s + received_at_s) / 2
    return server_time_ms / 1000.0 - midpoint


def is_stale(last_msg_at: float, now: float, timeout_s: float = HEARTBEAT_TIMEOUT_S) -> bool:
    """Heartbeat staleness decision on monotonic-clock readings."""
    return (now - last_msg_at) > timeout_s


def parse_kline_row(symbol: str, tf: str, row: list) -> Candle:
    """Binance REST kline row (12-element array) -> normalized Candle.

    Index map: 0 open-time ms, 1 o, 2 h, 3 l, 4 c, 5 base volume,
    7 quote volume, 8 trade count, 9 taker-buy base volume. 6/10/11 unused.
    """
    return Candle(
        symbol=symbol,
        tf=tf,
        ts=_utc(row[0]),
        o=float(row[1]),
        h=float(row[2]),
        l=float(row[3]),
        c=float(row[4]),
        v=float(row[5]),
        qv=float(row[7]),
        n_trades=int(row[8]),
        taker_buy_v=float(row[9]),
    )


def compute_gap_range(
    last_closed_ts: datetime | None, now: datetime
) -> tuple[datetime, datetime] | None:
    """Missing closed 1m candles after a reconnect: [last+1m, floor(now)).

    None when there is no previous candle (first connection — bootstrap's
    job, P0.16) or when the range is empty (reconnect within the same
    minute). `end` is floored to the minute so only fully closed candles
    are ever requested.
    """
    if last_closed_ts is None:
        return None
    start = last_closed_ts + timedelta(minutes=1)
    end = now.replace(second=0, microsecond=0)
    if start >= end:
        return None
    return start, end


# ------------------------------------------------------------ the provider


class BinanceFeed(FeedProvider):
    """Live Binance provider: one combined WS -> normalized events on the bus."""

    name = "binance"

    def __init__(
        self,
        symbols: list[str] | tuple[str, ...],
        bus: EventBus,
        rest_base_url: str = REST_BASE,
        on_reference_candle=None,
    ) -> None:
        """on_reference_candle (roadmap P0.17): optional plain callable.
        When provided (composition wires reconciler.on_reference here), live
        closed klines are handed to it INSTEAD of the bus — the bus then
        carries truth candles only. When None, standalone P0.10 behavior is
        preserved (reference klines published on the bus). Backfilled gap
        candles always go to the bus: they are truth (Decision D5)."""
        self._symbols = list(symbols)
        self._bus = bus
        self._rest_base = rest_base_url
        self._on_reference_candle = on_reference_candle
        self._connected = False
        self._stopping = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._runner: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._last_msg_at = 0.0  # monotonic loop time of last received message
        self._last_closed_ts: dict[str, datetime] = {}  # per symbol, 1m closes
        # D33 restart-gap bridge (live only; set by prime_last_closed()):
        self._bridge_pending = False
        self._bridge_task: asyncio.Task | None = None
        self._now = lambda: datetime.now(tz=timezone.utc)   # injectable for tests

    # ------------------------------------------------- interface: contract

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_live_data=True,
            supports_historical_data=True,  # implemented at P0.15
            supports_orderbook=True,
            supports_trades=True,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Open the combined stream and begin publishing normalized events."""
        if self._runner is not None:
            raise RuntimeError("BinanceFeed already started")
        self._stopping = False
        self._runner = asyncio.create_task(self._run(), name="binance-feed")
        self._watchdog = asyncio.create_task(
            self._heartbeat_watchdog(), name="binance-heartbeat"
        )

    async def stop(self) -> None:
        """Stop publishing, cancel internal tasks, close the socket."""
        self._stopping = True
        tasks = [t for t in (self._watchdog, self._runner, self._bridge_task)
                 if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._runner = self._watchdog = self._bridge_task = None
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        self._connected = False

    async def fetch_historical_candles(
        self, symbol: str, tf: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Paginated GET /api/v3/klines -> normalized Candles, [start, end)
        by open time, ascending. Raw Binance JSON stays inside this method."""
        if tf not in _TF_MS:
            raise ValueError(f"unsupported timeframe {tf!r} (supported: 1m, 5m)")
        interval_ms = _TF_MS[tf]
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)  # exclusive
        out: list[Candle] = []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while start_ms < end_ms:
                params = {
                    "symbol": symbol,
                    "interval": tf,
                    "startTime": start_ms,
                    "endTime": end_ms - 1,  # Binance endTime is inclusive
                    "limit": KLINES_LIMIT,
                }
                async with session.get(
                    self._rest_base + KLINES_ENDPOINT, params=params
                ) as resp:
                    resp.raise_for_status()
                    rows = await resp.json()
                if not rows:
                    break
                for row in rows:
                    if row[0] < end_ms:  # [start, end) by open time
                        out.append(parse_kline_row(symbol, tf, row))
                start_ms = rows[-1][0] + interval_ms  # next page
                if len(rows) < KLINES_LIMIT:
                    break
        out.sort(key=lambda c: c.ts)
        return out

    def prime_last_closed(self, last_closed: dict[str, datetime]) -> None:
        """D33: composition seeds the per-symbol last-stored 1m candle ts (from
        the DB) so the FIRST connect backfills the restart teardown gap (the
        frozen first-connection skip only makes sense before the DB era), and
        arms the connect-minute bridge. Live only — replay/tests never call this,
        so their first-connection behavior is byte-identical."""
        self._last_closed_ts.update({s: t for s, t in last_closed.items() if t is not None})
        self._bridge_pending = True

    # ---------------------------------------------------- internal: policy

    async def _run(self) -> None:
        """Connect / read / normalize / publish loop with reconnect backoff."""
        loop = asyncio.get_running_loop()
        backoff = BACKOFF_INITIAL_S
        url = stream_url(self._symbols)
        while not self._stopping:
            connected_at: float | None = None
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    self._connected = True
                    connected_at = loop.time()
                    self._last_msg_at = loop.time()
                    log.info("binance: connected (%d symbols)", len(self._symbols))
                    await self._backfill_gaps()  # BEFORE resuming live flow (§4.1)
                    if self._bridge_pending:     # D33: bridge the restart gap
                        self._bridge_pending = False
                        connect_minute = self._now().replace(second=0, microsecond=0)
                        self._bridge_task = asyncio.create_task(
                            self._bridge_connect_minute(connect_minute),
                            name="binance-bridge")
                    async for raw in ws:
                        self._last_msg_at = loop.time()
                        event = normalize_message(
                            json.loads(raw), datetime.now(tz=timezone.utc)
                        )
                        if event is None:
                            continue
                        if isinstance(event, Candle) and event.tf == "1m":
                            self._last_closed_ts[event.symbol] = event.ts
                            if self._on_reference_candle is not None:
                                # Explicit reference routing (P0.17): live
                                # klines go to reconciliation, NOT the bus.
                                self._on_reference_candle(event)
                                continue
                        await self._bus.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # socket/protocol errors -> reconnect
                log.warning("binance: stream error: %s", exc)
            finally:
                self._connected = False
                self._ws = None
            if self._stopping:
                break
            if connected_at is not None and loop.time() - connected_at >= STABLE_RESET_S:
                backoff = BACKOFF_INITIAL_S
            log.info("binance: reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = next_backoff(backoff)

    async def _backfill_gaps(self) -> None:
        """Fetch and publish the closed 1m candles missed while disconnected,
        in chronological order, before live processing resumes. First
        connection with no last-closed candle -> nothing to do (unless
        prime_last_closed seeded it from the DB, D33)."""
        now = self._now()
        for symbol in self._symbols:
            gap = compute_gap_range(self._last_closed_ts.get(symbol), now)
            if gap is None:
                continue
            start, end = gap
            log.info("binance: backfilling %s gap [%s, %s)", symbol, start, end)
            candles = await self.fetch_historical_candles(symbol, "1m", start, end)
            for candle in candles:
                self._last_closed_ts[candle.symbol] = candle.ts
                await self._bus.publish(candle)
            log.info("binance: backfilled %d candles for %s", len(candles), symbol)

    async def _bridge_connect_minute(self, connect_minute: datetime) -> None:
        """D33: the minute the feed connects in is built partial (D7 discards it)
        and _backfill_gaps at connect can't fetch it (still open) -> a
        restart-boundary gap that otherwise poisons G1 for ~30 min. Once that
        minute closes, fetch it from REST and publish it (truth -> bus, D5), so
        the bus stream is contiguous across the restart AND the DB hole is filled.

        Runs concurrently with the live loop. The builder's first live candle
        lags a full minute (it closes on the next bucket's first trade), so this
        publishes well before it; if it were ever late, the closed candle is just
        dropped by the composition out-of-order guard -> graceful fallback to the
        normal warm-up, never corruption or a duplicate."""
        end = connect_minute + timedelta(minutes=1)
        wait_s = (end - self._now()).total_seconds() + BRIDGE_SETTLE_S
        if wait_s > 0:
            await asyncio.sleep(wait_s)

        async def _fetch(symbol):                          # per symbol, fail-soft
            try:
                return symbol, await self.fetch_historical_candles(
                    symbol, "1m", connect_minute, end)
            except Exception as exc:
                log.warning("binance: connect-minute bridge fetch failed for %s: %s",
                            symbol, exc)
                return symbol, []

        try:
            # fetch symbols CONCURRENTLY so one slow REST call can't delay another
            # past its first live candle (audit M1); publish sequentially in order.
            results = await asyncio.gather(*(_fetch(s) for s in self._symbols))
            for symbol, candles in results:
                for candle in candles:
                    self._last_closed_ts[candle.symbol] = candle.ts
                    await self._bus.publish(candle)
                if candles:
                    log.info("binance: bridged connect-minute %s for %s (%d candle)",
                             connect_minute, symbol, len(candles))
        except Exception as exc:                # detached task — never die unretrieved
            log.warning("binance: connect-minute bridge error: %s", exc)

    async def _heartbeat_watchdog(self) -> None:
        """Force-close a silent socket; the runner loop then reconnects."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL_S)
            if self._connected and is_stale(self._last_msg_at, loop.time()):
                log.warning(
                    "binance: heartbeat timeout (> %.0fs silent) — forcing reconnect",
                    HEARTBEAT_TIMEOUT_S,
                )
                ws = self._ws
                if ws is not None:
                    await ws.close()


# ------------------------------------------------- clock-offset sampler (D6)


class ClockOffsetSampler:
    """Local-vs-Binance clock offset, sampled every 5 minutes (Decision D6/A12).

    State the G1 gate (P3) reads:
      offset_s — latest offset in seconds, or None while unknown (no
                 successful sample yet, or CLOCK_FAILURE_LIMIT consecutive
                 failures).
      in_sync  — True iff offset is known and |offset| <= CLOCK_OFFSET_LIMIT_S.

    No gate logic lives here — this class only measures.
    """

    def __init__(
        self,
        base_url: str = REST_BASE,
        interval_s: float = CLOCK_SAMPLE_INTERVAL_S,
    ) -> None:
        self._url = base_url + TIME_ENDPOINT
        self._interval = interval_s
        self._offset_s: float | None = None
        self._failures = 0
        self._task: asyncio.Task | None = None

    @property
    def offset_s(self) -> float | None:
        return self._offset_s

    @property
    def in_sync(self) -> bool:
        return self._offset_s is not None and abs(self._offset_s) <= CLOCK_OFFSET_LIMIT_S

    async def sample_once(self, session: aiohttp.ClientSession) -> float | None:
        """One measurement. Updates offset_s on success; on failure counts
        toward CLOCK_FAILURE_LIMIT, after which offset_s becomes None."""
        sent_at = time.time()
        try:
            async with session.get(self._url) as resp:
                resp.raise_for_status()
                payload = await resp.json()
            received_at = time.time()
            offset = compute_offset_s(int(payload["serverTime"]), sent_at, received_at)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as exc:
            self._failures += 1
            log.warning(
                "binance: clock sample failed (%d/%d): %s",
                self._failures, CLOCK_FAILURE_LIMIT, exc,
            )
            if self._failures >= CLOCK_FAILURE_LIMIT:
                self._offset_s = None  # unknown -> G1 must fail (D6)
            return None
        self._failures = 0
        self._offset_s = offset
        log.info("binance: clock offset %.3fs (in_sync=%s)", offset, self.in_sync)
        return offset

    async def start(self) -> None:
        """Begin sampling every interval_s seconds in a background task."""
        if self._task is not None:
            raise RuntimeError("ClockOffsetSampler already started")
        self._task = asyncio.create_task(self._run(), name="binance-clock-sampler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                await self.sample_once(session)
                await asyncio.sleep(self._interval)

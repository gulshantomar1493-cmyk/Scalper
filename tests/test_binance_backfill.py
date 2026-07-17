"""Tests for P0.15: historical klines fetch + gap-safe reconnect backfill.

Local test servers only (aiohttp.web for REST, websockets for the stream) —
no real Binance network, no mocks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import websockets
from aiohttp import web

from marketscalper.core.bus import EventBus
from marketscalper.providers import binance
from marketscalper.providers.base import Candle, Trade
from marketscalper.providers.binance import (
    BinanceFeed,
    compute_gap_range,
    parse_kline_row,
)

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _row(open_dt: datetime, o=67000.0, n=42):
    """A 12-element Binance REST kline row for one minute."""
    open_ms = int(open_dt.timestamp() * 1000)
    return [open_ms, str(o), str(o + 30), str(o - 10), str(o + 5), "12.5",
            open_ms + 59_999, "838125.0", n, "7.1", "477105.0", "0"]


# ------------------------------------------------------------ pure helpers


def test_parse_kline_row_exact_mapping():
    c = parse_kline_row("BTCUSDT", "1m", _row(M0, o=67200.0, n=420))
    assert c == Candle(
        symbol="BTCUSDT", tf="1m", ts=M0,
        o=67200.0, h=67230.0, l=67190.0, c=67205.0,
        v=12.5, qv=838125.0, n_trades=420, taker_buy_v=7.1,
    )


def test_gap_range_normal_reconnect():
    now = M0 + timedelta(minutes=10, seconds=42)
    gap = compute_gap_range(M0, now)                       # last closed candle: M0
    assert gap == (M0 + timedelta(minutes=1), M0 + timedelta(minutes=10))
    # end floored to the minute -> only fully closed candles requested


def test_gap_range_same_minute_reconnect_is_empty():
    assert compute_gap_range(M0, M0 + timedelta(seconds=59)) is None
    assert compute_gap_range(M0, M0 + timedelta(minutes=1, seconds=30)) is None


def test_gap_range_first_connection_no_backfill():
    assert compute_gap_range(None, M0 + timedelta(hours=5)) is None


# ------------------------------------------------------------- REST fetch


async def _rest_server(klines, fail_times: int = 0):
    """Local /api/v3/klines honoring startTime/endTime/limit; logs requests.

    klines: flat list (any symbol) or dict {symbol: rows}. fail_times > 0
    makes the first N requests return HTTP 500 (existing-retry verification).
    """
    requests: list[dict] = []
    failures = {"left": fail_times}

    async def handler(request):
        q = request.rel_url.query
        requests.append(dict(q))
        if failures["left"] > 0:
            failures["left"] -= 1
            return web.Response(status=500, text="injected failure")
        source = klines.get(q["symbol"], []) if isinstance(klines, dict) else klines
        start = int(q["startTime"])
        end_incl = int(q["endTime"])
        limit = int(q.get("limit", 500))
        rows = [r for r in source if start <= r[0] <= end_incl][:limit]
        return web.json_response(rows)

    app = web.Application()
    app.router.add_get("/api/v3/klines", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", requests


async def test_fetch_paginates_filters_and_sorts_ascending(monkeypatch):
    monkeypatch.setattr(binance, "KLINES_LIMIT", 2)        # force pagination
    klines = [_row(M0 + timedelta(minutes=i), o=100 + i) for i in range(5)]
    runner, base, requests = await _rest_server(klines)
    try:
        feed = BinanceFeed(["BTCUSDT"], EventBus(), rest_base_url=base)
        got = await feed.fetch_historical_candles(
            "BTCUSDT", "1m", M0, M0 + timedelta(minutes=5))
        assert [c.ts for c in got] == [M0 + timedelta(minutes=i) for i in range(5)]
        assert [c.o for c in got] == [100, 101, 102, 103, 104]  # ascending
        assert len(requests) == 3                               # 2 + 2 + 1 rows
        assert all(r["symbol"] == "BTCUSDT" and r["interval"] == "1m" for r in requests)
    finally:
        await runner.cleanup()


async def test_fetch_half_open_interval_excludes_end():
    klines = [_row(M0 + timedelta(minutes=i)) for i in range(4)]
    runner, base, _ = await _rest_server(klines)
    try:
        feed = BinanceFeed(["BTCUSDT"], EventBus(), rest_base_url=base)
        got = await feed.fetch_historical_candles(
            "BTCUSDT", "1m", M0, M0 + timedelta(minutes=3))
        assert [c.ts for c in got] == [M0 + timedelta(minutes=i) for i in range(3)]
    finally:
        await runner.cleanup()


# ----------------------------------------------- reconnect backfill (flow)


async def test_reconnect_fetches_gap_and_publishes_before_live():
    """disconnect -> reconnect -> backfill published in order -> live resumes."""
    now = datetime.now(tz=UTC)
    m_last = now.replace(second=0, microsecond=0) - timedelta(minutes=10)

    def kline_msg(open_dt):
        open_ms = int(open_dt.timestamp() * 1000)
        return json.dumps({
            "stream": "btcusdt@kline_1m",
            "data": {"e": "kline", "s": "BTCUSDT",
                     "k": {"t": open_ms, "T": open_ms + 59_999, "s": "BTCUSDT",
                           "i": "1m", "o": "1", "c": "1", "h": "1", "l": "1",
                           "v": "1", "n": 1, "x": True, "q": "1", "V": "1",
                           "Q": "1", "B": "0"}},
        })

    def trade_msg(price):
        return json.dumps({
            "stream": "btcusdt@aggTrade",
            "data": {"e": "aggTrade", "s": "BTCUSDT", "p": str(price),
                     "q": "0.1", "f": 1, "l": 1,
                     "T": int(now.timestamp() * 1000), "m": False},
        })

    # gap the REST server can serve: the 4 minutes after m_last
    gap_rows = [_row(m_last + timedelta(minutes=i)) for i in range(1, 5)]
    rest_runner, rest_base, rest_requests = await _rest_server(gap_rows)

    connections = 0
    second_conn_open = asyncio.Event()

    async def ws_handler(ws):
        nonlocal connections
        connections += 1
        if connections == 1:
            await ws.send(kline_msg(m_last))   # feed records last closed = m_last
            await asyncio.sleep(0.2)
            await ws.close()                   # drop -> gap
        else:
            second_conn_open.set()
            await ws.send(trade_msg(67000))    # live traffic after reconnect
            await asyncio.sleep(5)

    server = await websockets.serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    bus = EventBus()
    events: list[object] = []

    async def collect(e):
        events.append(e)

    bus.subscribe(Candle, collect)
    bus.subscribe(Trade, collect)

    orig_ws_base = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], bus, rest_base_url=rest_base)
        await feed.start()

        await asyncio.wait_for(second_conn_open.wait(), timeout=15)  # reconnected
        for _ in range(100):                    # wait for the live trade
            if any(isinstance(e, Trade) for e in events):
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig_ws_base
        server.close()
        await server.wait_closed()
        await rest_runner.cleanup()

    # no backfill on FIRST connection: the only REST calls came after the drop
    assert len(rest_requests) >= 1
    first_req_start = int(rest_requests[0]["startTime"])
    assert first_req_start == int((m_last + timedelta(minutes=1)).timestamp() * 1000)

    # backfilled candles: chronological, and BEFORE any post-reconnect live event
    backfilled = [e for e in events if isinstance(e, Candle) and e.ts > m_last]
    assert [c.ts for c in backfilled] == [m_last + timedelta(minutes=i) for i in range(1, 5)]
    live_trade_idx = next(i for i, e in enumerate(events) if isinstance(e, Trade))
    last_backfill_idx = max(i for i, e in enumerate(events)
                            if isinstance(e, Candle) and e.ts > m_last)
    assert last_backfill_idx < live_trade_idx


# ------------------------------------------------ P0.18 gap-fill coverage


def _kline_msg(symbol: str, open_dt: datetime) -> str:
    open_ms = int(open_dt.timestamp() * 1000)
    return json.dumps({
        "stream": f"{symbol.lower()}@kline_1m",
        "data": {"e": "kline", "s": symbol,
                 "k": {"t": open_ms, "T": open_ms + 59_999, "s": symbol,
                       "i": "1m", "o": "1", "c": "1", "h": "1", "l": "1",
                       "v": "1", "n": 1, "x": True, "q": "1", "V": "1",
                       "Q": "1", "B": "0"}},
    })


def _trade_msg(symbol: str, ts: datetime) -> str:
    return json.dumps({
        "stream": f"{symbol.lower()}@aggTrade",
        "data": {"e": "aggTrade", "s": symbol, "p": "67000", "q": "0.1",
                 "f": 1, "l": 1, "T": int(ts.timestamp() * 1000), "m": False},
    })


async def test_multi_symbol_backfill_all_before_live(caplog):
    """Both symbols' gaps are backfilled chronologically, all before the
    first live event after reconnect."""
    now = datetime.now(tz=UTC)
    m_last = now.replace(second=0, microsecond=0) - timedelta(minutes=10)
    gaps = {
        "BTCUSDT": [_row(m_last + timedelta(minutes=i), o=100 + i) for i in range(1, 4)],
        "ETHUSDT": [_row(m_last + timedelta(minutes=i), o=200 + i) for i in range(1, 4)],
    }
    rest_runner, rest_base, rest_requests = await _rest_server(gaps)

    connections = 0

    async def ws_handler(ws):
        nonlocal connections
        connections += 1
        if connections == 1:
            await ws.send(_kline_msg("BTCUSDT", m_last))
            await ws.send(_kline_msg("ETHUSDT", m_last))
            await asyncio.sleep(0.2)
            await ws.close()
        else:
            await ws.send(_trade_msg("BTCUSDT", now))
            await asyncio.sleep(5)

    server = await websockets.serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    bus = EventBus()
    events: list[object] = []

    async def collect(e):
        events.append(e)

    bus.subscribe(Candle, collect)
    bus.subscribe(Trade, collect)

    orig = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT", "ETHUSDT"], bus, rest_base_url=rest_base)
        await feed.start()
        for _ in range(200):
            if any(isinstance(e, Trade) for e in events):
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig
        server.close()
        await server.wait_closed()
        await rest_runner.cleanup()

    backfilled = [e for e in events if isinstance(e, Candle) and e.ts > m_last]
    per_symbol = {
        s: [c.ts for c in backfilled if c.symbol == s]
        for s in ("BTCUSDT", "ETHUSDT")
    }
    expected = [m_last + timedelta(minutes=i) for i in range(1, 4)]
    assert per_symbol["BTCUSDT"] == expected            # chronological per symbol
    assert per_symbol["ETHUSDT"] == expected
    live_idx = next(i for i, e in enumerate(events) if isinstance(e, Trade))
    last_bf_idx = max(i for i, e in enumerate(events)
                      if isinstance(e, Candle) and e.ts > m_last)
    assert last_bf_idx < live_idx                       # ALL backfill precedes live
    assert {r["symbol"] for r in rest_requests} == {"BTCUSDT", "ETHUSDT"}


async def test_backfill_rest_failure_rides_existing_reconnect_loop():
    """P0.15's stated behavior, verified — a REST failure during backfill
    propagates into the reconnect loop; the next cycle completes the
    backfill. No new retry semantics are introduced or asserted."""
    now = datetime.now(tz=UTC)
    m_last = now.replace(second=0, microsecond=0) - timedelta(minutes=6)
    rows = [_row(m_last + timedelta(minutes=i), o=100 + i) for i in range(1, 4)]
    rest_runner, rest_base, rest_requests = await _rest_server(rows, fail_times=1)

    connections = 0

    async def ws_handler(ws):
        nonlocal connections
        connections += 1
        try:
            if connections == 1:
                await ws.send(_kline_msg("BTCUSDT", m_last))
                await asyncio.sleep(0.2)
                await ws.close()
            else:
                await asyncio.sleep(5)   # stay open; backfill runs before reads
        except websockets.ConnectionClosed:
            pass

    server = await websockets.serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    bus = EventBus()
    candles: list[Candle] = []

    async def collect(c):
        candles.append(c)

    bus.subscribe(Candle, collect)

    orig = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], bus, rest_base_url=rest_base)
        await feed.start()
        for _ in range(300):                            # covers the 1s backoff cycle
            if len([c for c in candles if c.ts > m_last]) == 3:
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig
        server.close()
        await server.wait_closed()
        await rest_runner.cleanup()

    backfilled = [c for c in candles if c.ts > m_last]
    assert [c.ts for c in backfilled] == [m_last + timedelta(minutes=i) for i in range(1, 4)]
    assert len(rest_requests) >= 2                      # failed once, then succeeded
    assert connections >= 3                             # drop + failed cycle + good cycle


async def test_second_disconnect_requests_from_advanced_position():
    """Gap tracking advances past backfilled and live-received candles: the
    second disconnect's REST request starts after everything already seen."""
    now = datetime.now(tz=UTC)
    m_last = now.replace(second=0, microsecond=0) - timedelta(minutes=10)
    m_live = m_last + timedelta(minutes=6)              # newer live kline on conn 2
    rows = [_row(m_last + timedelta(minutes=i), o=100 + i) for i in range(1, 5)]
    rest_runner, rest_base, rest_requests = await _rest_server(rows)

    connections = 0

    async def ws_handler(ws):
        nonlocal connections
        connections += 1
        try:
            if connections == 1:
                await ws.send(_kline_msg("BTCUSDT", m_last))
                await asyncio.sleep(0.2)
                await ws.close()
            elif connections == 2:
                await ws.send(_kline_msg("BTCUSDT", m_live))  # advances tracking
                await asyncio.sleep(0.3)
                await ws.close()
            else:
                await asyncio.sleep(5)
        except websockets.ConnectionClosed:
            pass

    server = await websockets.serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    bus = EventBus()
    orig = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], bus, rest_base_url=rest_base)
        await feed.start()
        for _ in range(300):
            if len(rest_requests) >= 2:
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig
        server.close()
        await server.wait_closed()
        await rest_runner.cleanup()

    assert len(rest_requests) >= 2
    first_start = int(rest_requests[0]["startTime"])
    second_start = int(rest_requests[1]["startTime"])
    assert first_start == int((m_last + timedelta(minutes=1)).timestamp() * 1000)
    # after backfill (through m_last+4) and the live kline at m_live,
    # the next request starts at m_live + 1m — never re-fetching old ground
    assert second_start == int((m_live + timedelta(minutes=1)).timestamp() * 1000)


async def test_first_connection_makes_no_rest_calls():
    rest_runner, rest_base, rest_requests = await _rest_server([])

    async def ws_handler(ws):
        await asyncio.sleep(3)

    server = await websockets.serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    orig_ws_base = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], EventBus(), rest_base_url=rest_base)
        await feed.start()
        for _ in range(50):
            if feed.connected:
                break
            await asyncio.sleep(0.1)
        assert feed.connected is True
        await asyncio.sleep(0.3)               # backfill step already ran
        await feed.stop()
    finally:
        binance.WS_BASE = orig_ws_base
        server.close()
        await server.wait_closed()
        await rest_runner.cleanup()

    assert rest_requests == []                  # no previous candle -> no fetch

"""Shared FeedProvider conformance suite (roadmap P0.19).

The SAME six tests run against BinanceFeed and ReplayFeed — identical
assertions on the same canonical dataset. Provider-specific knowledge lives
ONLY in the environment fixture (how to stand each provider up); every test
body is provider-blind and verifies nothing beyond the P0.9 contract and
roadmap-required behavior (normalized-events-only, [start, end) ascending
history). Implementation details (backoff, heartbeat, pacing, reconnect)
stay in the per-provider test files.

Local test servers are deliberately kept local to this module (no coupling
to other test modules, per owner direction).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import websockets
from aiohttp import web
from conftest import TxPool

from marketscalper import db
from marketscalper.core.bus import EventBus
from marketscalper.providers import binance as binance_module
from marketscalper.providers.base import (
    BookTicker,
    Candle,
    Capabilities,
    FeedProvider,
    Tick,
    Trade,
)
from marketscalper.providers.binance import BinanceFeed
from marketscalper.providers.replay import ReplayFeed

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)
NORMALIZED_TYPES = (Trade, Tick, BookTicker, Candle)

# One canonical dataset — both providers serve it, so every assertion below
# is a shared constant ("identical tests pass for both").
CANON = [
    Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=i),
           o=100.0 + i, h=102.0 + i, l=99.0 + i, c=101.0 + i,
           v=1.5, qv=(100.0 + i) * 1.5, n_trades=3 + i, taker_buy_v=0.5)
    for i in range(5)
]


# ------------------------------------------------- local binance environment


def _kline_row(c: Candle) -> list:
    open_ms = int(c.ts.timestamp() * 1000)
    return [open_ms, str(c.o), str(c.h), str(c.l), str(c.c), str(c.v),
            open_ms + 59_999, str(c.qv), c.n_trades, str(c.taker_buy_v), "0", "0"]


async def _local_rest():
    async def handler(request):
        q = request.rel_url.query
        start, end_incl = int(q["startTime"]), int(q["endTime"])
        rows = [_kline_row(c) for c in CANON
                if start <= int(c.ts.timestamp() * 1000) <= end_incl]
        return web.json_response(rows)

    app = web.Application()
    app.router.add_get("/api/v3/klines", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _local_ws():
    trade = json.dumps({
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "100.5", "q": "0.1",
                 "T": int(M0.timestamp() * 1000), "m": False},
    })
    book = json.dumps({
        "stream": "btcusdt@bookTicker",
        "data": {"u": 1, "s": "BTCUSDT", "b": "100.4", "B": "1", "a": "100.6", "A": "1"},
    })

    async def handler(ws):
        try:
            while True:
                await ws.send(trade)
                await ws.send(book)
                await asyncio.sleep(0.05)
        except websockets.ConnectionClosed:
            pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


# -------------------------------------------------------- parametrized env


@pytest.fixture(params=["binance", "replay"])
async def env(request, monkeypatch, db_conn):
    """Provider + bus + event collector. All provider-specific setup is here.

    Both params request the dev-DB fixture (the replay param needs it; the
    binance param simply ignores it) — so the whole conformance suite skips
    together when MARKETSCALPER_DB_DSN is unset, like all DB-backed tests."""
    bus = EventBus()
    events: list[object] = []

    async def collect(e):
        events.append(e)

    for t in NORMALIZED_TYPES:
        bus.subscribe(t, collect)

    if request.param == "binance":
        ws_server, ws_port = await _local_ws()
        rest_runner, rest_base = await _local_rest()
        monkeypatch.setattr(
            binance_module, "WS_BASE", f"ws://127.0.0.1:{ws_port}/stream?streams=")
        feed = BinanceFeed(["BTCUSDT"], bus, rest_base_url=rest_base)
        yield SimpleNamespace(feed=feed, events=events)
        await feed.stop()
        ws_server.close()
        await ws_server.wait_closed()
        await rest_runner.cleanup()
    else:
        await db.insert_candles(
            db_conn,
            [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
              c.n_trades, c.taker_buy_v) for c in CANON],
        )
        feed = ReplayFeed(["BTCUSDT"], bus, TxPool(db_conn),
                          M0, M0 + timedelta(minutes=5), speed=60)
        yield SimpleNamespace(feed=feed, events=events)
        await feed.stop()


async def _wait(predicate, timeout_s=10.0):
    for _ in range(int(timeout_s / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


# --------------------------------------------------- the six shared tests


async def test_contract_surface(env):
    feed = env.feed
    assert isinstance(feed, FeedProvider)
    assert isinstance(feed.name, str) and feed.name
    caps = feed.capabilities
    assert isinstance(caps, Capabilities)
    for field in dataclasses.fields(Capabilities):
        assert isinstance(getattr(caps, field.name), bool)


async def test_disconnected_before_start(env):
    assert env.feed.connected is False


async def test_start_connects_and_publishes_only_normalized_types(env):
    await env.feed.start()
    assert await _wait(lambda: len(env.events) >= 1)
    assert env.feed.connected is True
    assert all(type(e) in NORMALIZED_TYPES for e in env.events)


async def test_stop_disconnects_and_publishing_ceases(env):
    await env.feed.start()
    assert await _wait(lambda: len(env.events) >= 1)
    await env.feed.stop()
    assert env.feed.connected is False
    seen = len(env.events)
    await asyncio.sleep(0.3)
    assert len(env.events) == seen


async def test_fetch_half_open_ascending(env):
    got = await env.feed.fetch_historical_candles(
        "BTCUSDT", "1m", M0, M0 + timedelta(minutes=3))
    assert [c.ts for c in got] == [M0 + timedelta(minutes=i) for i in range(3)]


async def test_fetch_matches_canonical_dataset(env):
    got = await env.feed.fetch_historical_candles(
        "BTCUSDT", "1m", M0, M0 + timedelta(minutes=3))
    assert got == CANON[:3]

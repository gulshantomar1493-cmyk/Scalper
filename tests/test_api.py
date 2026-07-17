"""Tests for the FastAPI app (roadmap P0.21) — real server, real clients.

The app runs in-process under uvicorn on an ephemeral port; REST is tested
with aiohttp and the WebSocket with the websockets client — all existing
dependencies, no test frameworks added.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
import uvicorn
import websockets
from conftest import TxPool

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Trade
from marketscalper.providers.replay import ReplayFeed

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"127.0.0.1:{port}"


async def _stop(server, task):
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


def _pipeline(pool=None, replay_provider=None):
    """bus + store (subscribed FIRST, per the composition note) + app."""
    bus = EventBus()
    store = StateStore(bus)
    app = create_app(bus, store, pool, TOKEN, replay_provider=replay_provider)
    return bus, store, app


def _replay_body(speed="max", minutes=5, symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "start": M0.isoformat(),
        "end": (M0 + timedelta(minutes=minutes)).isoformat(),
        "speed": speed,
    }


async def _seed_candles(db_conn, n=5):
    rows = [("BTCUSDT", "1m", M0 + timedelta(minutes=i),
             100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 1.5, 150.0, 3, 0.5)
            for i in range(n)]
    await db.insert_candles(db_conn, rows)


# ------------------------------------------------------------------- REST


async def test_health_is_open():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/health") as resp:
                assert resp.status == 200
                assert await resp.json() == {"status": "ok"}
    finally:
        await _stop(server, task)


async def test_candles_requires_bearer_token():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "1m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params) as resp:
                assert resp.status == 401                       # missing
            bad = {"Authorization": "Bearer wrong"}
            async with s.get(f"http://{addr}/candles", params=params, headers=bad) as resp:
                assert resp.status == 401                       # wrong
    finally:
        await _stop(server, task)


async def test_candles_history_roundtrip(db_conn):
    rows = [("BTCUSDT", "1m", M0 + timedelta(minutes=i),
             67000 + i, 67010 + i, 66990 + i, 67005 + i, 1.0, 67000.0, 10 + i, 0.5)
            for i in range(3)]
    await db.insert_candles(db_conn, rows)

    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "1m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params, headers=AUTH) as resp:
                assert resp.status == 200
                body = await resp.json()
        assert [c["ts"] for c in body] == [(M0 + timedelta(minutes=i)).isoformat()
                                           for i in range(3)]
        assert body[0]["o"] == 67000 and body[2]["n_trades"] == 12
    finally:
        await _stop(server, task)


async def test_candles_rejects_unknown_tf():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "15m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params, headers=AUTH) as resp:
                assert resp.status == 400
    finally:
        await _stop(server, task)


# -------------------------------------------------------------- WebSocket


async def test_ws_rejects_bad_token():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        with pytest.raises(Exception):                          # 403 handshake or 1008 close
            async with websockets.connect(f"ws://{addr}/ws?token=wrong") as ws:
                await asyncio.wait_for(ws.recv(), timeout=2)
    finally:
        await _stop(server, task)


async def test_ws_pushes_candle_and_state_diff():
    bus, _, app = _pipeline()
    CandleBuilder(bus)                                          # trades -> closed candles
    # prime: the builder discards each symbol's first bucket (startup rule)
    await bus.publish(Trade(symbol="BTCUSDT", price=1.0, qty=1.0,
                            ts=M0 - timedelta(minutes=1), is_buyer_maker=False))
    server, task, addr = await _serve(app)
    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
            await bus.publish(Trade(symbol="BTCUSDT", price=67200.0, qty=2.0,
                                    ts=M0 + timedelta(seconds=5), is_buyer_maker=False))
            await bus.publish(Trade(symbol="BTCUSDT", price=67210.0, qty=1.0,
                                    ts=M0 + timedelta(seconds=65), is_buyer_maker=True))
            import json
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert msg["candle"]["symbol"] == "BTCUSDT"
        assert msg["candle"]["tf"] == "1m"
        assert msg["candle"]["ts"] == M0.isoformat()
        assert msg["candle"]["o"] == 67200.0 and msg["candle"]["n_trades"] == 1
        diff = msg["state_diff"]["BTCUSDT"]["last_candle_1m"]
        assert diff["ts"] == M0.isoformat()                     # store updated before push
    finally:
        await _stop(server, task)


async def test_replay_endpoints_require_token_and_config():
    _, _, app = _pipeline()                               # replay NOT configured
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/replay/start", json=_replay_body()) as r:
                assert r.status == 401                    # auth first
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert r.status == 503                    # not configured
            async with s.post(f"http://{addr}/replay/start", json=_replay_body(),
                              headers=AUTH) as r:
                assert r.status == 503
    finally:
        await _stop(server, task)


async def test_replay_start_runs_to_completion_over_existing_ws(db_conn):
    await _seed_candles(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert (await r.json())["running"] is False   # idle initially

            async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
                async with s.post(f"http://{addr}/replay/start",
                                  json=_replay_body(speed="max"), headers=AUTH) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["running"] is True and body["symbol"] == "BTCUSDT"
                    assert body["speed"] == "max"
                import json as _json
                msg = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert set(msg) == {"candle", "state_diff"}   # existing protocol only
                assert msg["candle"]["symbol"] == "BTCUSDT"

            for _ in range(100):                              # completion -> idle
                async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                    status = await r.json()
                if status["running"] is False:
                    break
                await asyncio.sleep(0.05)
            assert status == {"running": False, "symbol": None, "start": None,
                              "end": None, "speed": None}
    finally:
        await _stop(server, task)


async def test_replay_start_validation(db_conn):
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    bad = [
        _replay_body(speed=2),                                   # invalid speed
        {**_replay_body(), "start": _replay_body()["end"],
         "end": _replay_body()["start"]},                        # start >= end
        _replay_body(symbol="DOGEUSDT"),                         # invalid symbol
        {**_replay_body(), "start": "not-a-date"},               # unparseable
    ]
    try:
        async with aiohttp.ClientSession() as s:
            for payload in bad:
                async with s.post(f"http://{addr}/replay/start", json=payload,
                                  headers=AUTH) as r:
                    assert r.status == 400, payload
    finally:
        await _stop(server, task)


async def test_replay_second_start_409_then_stop(db_conn):
    await _seed_candles(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/replay/start",
                              json=_replay_body(speed=60), headers=AUTH) as r:
                assert r.status == 200                           # slow replay: stays running
            async with s.post(f"http://{addr}/replay/start",
                              json=_replay_body(speed=60), headers=AUTH) as r:
                assert r.status == 409                           # already running
            async with s.post(f"http://{addr}/replay/stop", headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["running"] is False
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert (await r.json())["running"] is False      # stopped -> idle
            async with s.post(f"http://{addr}/replay/stop", headers=AUTH) as r:
                assert r.status == 200                           # idle stop = no-op
    finally:
        await _stop(server, task)


async def test_replay_speeds_endpoint(db_conn):
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/replay/speeds", headers=AUTH) as r:
                assert await r.json() == {"speeds": [1, 10, 60, "max"]}
    finally:
        await _stop(server, task)


async def test_ws_broadcasts_to_all_clients():
    bus, _, app = _pipeline()
    CandleBuilder(bus)
    # prime: the builder discards each symbol's first bucket (startup rule)
    await bus.publish(Trade(symbol="ETHUSDT", price=1.0, qty=1.0,
                            ts=M0 - timedelta(minutes=1), is_buyer_maker=False))
    server, task, addr = await _serve(app)
    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws1, \
                   websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws2:
            await bus.publish(Trade(symbol="ETHUSDT", price=3500.0, qty=1.0,
                                    ts=M0, is_buyer_maker=False))
            await bus.publish(Trade(symbol="ETHUSDT", price=3501.0, qty=1.0,
                                    ts=M0 + timedelta(seconds=61), is_buyer_maker=False))
            import json
            m1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
            m2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5))
        assert m1 == m2
        assert m1["candle"]["symbol"] == "ETHUSDT"
    finally:
        await _stop(server, task)

"""Tests for the candle persistence writer + reference routing (P0.17)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import websockets
from conftest import TxPool as _TxPool

from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.candle_writer import CandleWriter
from marketscalper.core.reconciler import KlineReconciler
from marketscalper.providers import binance
from marketscalper.providers.base import Candle, Trade
from marketscalper.providers.binance import BinanceFeed

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(ts=M0, tf="1m", symbol="BTCUSDT", o=67200.0):
    return Candle(symbol=symbol, tf=tf, ts=ts, o=o, h=o + 30, l=o - 10,
                  c=o + 15, v=12.5, qv=838125.0, n_trades=420, taker_buy_v=7.1)


async def _rows(conn):
    return await conn.fetch(
        "SELECT symbol, tf, ts, o, h, l, c, v, qv, n_trades, taker_buy_v"
        " FROM candles ORDER BY tf, ts")


# ----------------------------------------------------------- persistence


async def test_truth_candle_persists_exact_row(db_conn):
    bus = EventBus()
    writer = CandleWriter(bus, _TxPool(db_conn))
    c = _candle()
    await bus.publish(c)
    rows = await _rows(db_conn)
    assert len(rows) == 1 and writer.rows_written == 1 and writer.write_errors == 0
    r = rows[0]
    assert (r["symbol"], r["tf"], r["ts"]) == (c.symbol, "1m", c.ts)
    assert (float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) == (c.o, c.h, c.l, c.c)
    assert (float(r["v"]), float(r["qv"]), r["n_trades"], float(r["taker_buy_v"])) == (
        c.v, c.qv, c.n_trades, c.taker_buy_v)


async def test_5m_candle_persisted(db_conn):
    bus = EventBus()
    CandleWriter(bus, _TxPool(db_conn))
    await bus.publish(_candle(tf="5m"))
    rows = await _rows(db_conn)
    assert len(rows) == 1 and rows[0]["tf"] == "5m"


async def test_multiple_candles_write_in_order(db_conn):
    bus = EventBus()
    writer = CandleWriter(bus, _TxPool(db_conn))
    for i in range(3):
        await bus.publish(_candle(ts=M0 + timedelta(minutes=i), o=100.0 + i))
    rows = await _rows(db_conn)
    assert [r["ts"] for r in rows] == [M0 + timedelta(minutes=i) for i in range(3)]
    assert writer.rows_written == 3


async def test_duplicate_logged_skipped_and_writer_continues(db_conn, caplog):
    bus = EventBus()
    writer = CandleWriter(bus, _TxPool(db_conn))
    await bus.publish(_candle(o=100.0))                       # first: persists
    with caplog.at_level("ERROR"):
        await bus.publish(_candle(o=999.0))                   # same key: skipped
    await bus.publish(_candle(ts=M0 + timedelta(minutes=1)))  # writer still alive
    rows = await _rows(db_conn)
    assert len(rows) == 2
    assert float(rows[0]["o"]) == 100.0                       # first write kept
    assert (writer.rows_written, writer.write_errors) == (2, 1)
    assert any("insert failed" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------ reference routing


async def _ws_serving_one_kline(kline_open: datetime):
    open_ms = int(kline_open.timestamp() * 1000)
    msg = json.dumps({
        "stream": "btcusdt@kline_1m",
        "data": {"e": "kline", "s": "BTCUSDT",
                 "k": {"t": open_ms, "T": open_ms + 59_999, "s": "BTCUSDT",
                       "i": "1m", "o": "1", "c": "2", "h": "3", "l": "0.5",
                       "v": "1", "n": 1, "x": True, "q": "1", "V": "1",
                       "Q": "1", "B": "0"}},
    })

    async def handler(ws):
        await ws.send(msg)
        await asyncio.sleep(3)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_reference_kline_goes_to_callback_not_bus():
    kline_open = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    server, port = await _ws_serving_one_kline(kline_open)
    bus_candles: list[Candle] = []
    referenced: list[Candle] = []

    bus = EventBus()

    async def collect(c):
        bus_candles.append(c)

    bus.subscribe(Candle, collect)

    orig = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], bus, on_reference_candle=referenced.append)
        await feed.start()
        for _ in range(100):
            if referenced:
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig
        server.close()
        await server.wait_closed()

    assert len(referenced) == 1 and referenced[0].ts == kline_open
    assert bus_candles == []                       # reference never hits the bus


async def test_without_callback_standalone_behavior_preserved():
    kline_open = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    server, port = await _ws_serving_one_kline(kline_open)
    bus_candles: list[Candle] = []
    bus = EventBus()

    async def collect(c):
        bus_candles.append(c)

    bus.subscribe(Candle, collect)

    orig = binance.WS_BASE
    binance.WS_BASE = f"ws://127.0.0.1:{port}/stream?streams="
    try:
        feed = BinanceFeed(["BTCUSDT"], bus)       # no callback
        await feed.start()
        for _ in range(100):
            if bus_candles:
                break
            await asyncio.sleep(0.05)
        await feed.stop()
    finally:
        binance.WS_BASE = orig
        server.close()
        await server.wait_closed()

    assert len(bus_candles) == 1                   # P0.10 behavior intact


# ------------------------------------------------------- end-to-end mini


async def test_mini_flow_trades_to_db_and_reference_to_reconciler(db_conn):
    """Trade -> builder -> bus -> writer -> DB; reference -> reconciler."""
    bus = EventBus()
    CandleBuilder(bus)
    writer = CandleWriter(bus, _TxPool(db_conn))
    reconciler = KlineReconciler()

    async def to_built(c: Candle):                 # composition-level wiring
        if c.tf == "1m":
            reconciler.on_built(c)

    bus.subscribe(Candle, to_built)

    # prime: the first bucket per symbol is discarded at rollover (startup rule)
    await bus.publish(Trade(symbol="BTCUSDT", price=1.0, qty=1.0,
                            ts=M0 - timedelta(minutes=1), is_buyer_maker=False))
    # one trade in minute M0, one in M0+1 -> closes the M0 truth candle
    await bus.publish(Trade(symbol="BTCUSDT", price=67200.0, qty=2.0,
                            ts=M0 + timedelta(seconds=5), is_buyer_maker=False))
    await bus.publish(Trade(symbol="BTCUSDT", price=67210.0, qty=1.0,
                            ts=M0 + timedelta(seconds=65), is_buyer_maker=True))

    rows = await _rows(db_conn)
    assert len(rows) == 1 and rows[0]["ts"] == M0  # truth persisted
    assert writer.rows_written == 1

    # reference kline for M0, identical values -> clean reconciliation pair
    reference = Candle(symbol="BTCUSDT", tf="1m", ts=M0, o=67200.0, h=67200.0,
                       l=67200.0, c=67200.0, v=2.0, qv=134400.0, n_trades=1,
                       taker_buy_v=2.0)
    reconciler.on_reference(reference)
    assert (reconciler.pairs_compared, reconciler.mismatches) == (1, 0)

"""Tests for ReplayFeed (roadmap P0.24) — including the pipeline-identity
proof that replay 5m aggregation equals the live builder's, permanently."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from conftest import TxPool

from marketscalper import db
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle, FeedProvider, Trade
from marketscalper.providers.replay import ReplayFeed, delay_for_speed

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)  # minute bucket divisible by 5


def _candle(symbol, minute, o, taker=0.4, tf="1m"):
    ts = M0 + timedelta(minutes=minute)
    return Candle(symbol=symbol, tf=tf, ts=ts, o=o, h=o + 2, l=o - 1, c=o + 1,
                  v=1.5, qv=o * 1.5, n_trades=3, taker_buy_v=taker)


def _row(c: Candle):
    return (c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
            c.n_trades, c.taker_buy_v)


async def _seed(conn, candles):
    await db.insert_candles(conn, [_row(c) for c in candles])


async def _collecting_bus():
    bus = EventBus()
    events: list[Candle] = []

    async def collect(e):
        events.append(e)

    bus.subscribe(Candle, collect)
    return bus, events


async def _replay_all(db_conn, symbols, start, end, speed="max"):
    """Run a full replay to completion; return the collected events."""
    bus, events = await _collecting_bus()
    feed = ReplayFeed(symbols, bus, TxPool(db_conn), start, end, speed=speed)
    await feed.start()
    for _ in range(400):
        if feed._task is not None and feed._task.done():
            break
        await asyncio.sleep(0.01)
    await feed.stop()
    return events, feed


# ------------------------------------------------------- contract + flags


def test_replayfeed_satisfies_feedprovider_contract():
    feed = ReplayFeed(["BTCUSDT"], EventBus(), None, M0, M0, speed="max")
    assert isinstance(feed, FeedProvider)
    assert feed.name == "replay" and feed.connected is False
    caps = feed.capabilities
    assert caps.supports_orderbook is False          # pinned by the roadmap
    assert caps.supports_trades is False             # candles, not trades
    assert caps.supports_historical_data is True
    assert caps.supports_live_data is True


def test_invalid_speed_rejected():
    with pytest.raises(ValueError):
        ReplayFeed(["BTCUSDT"], EventBus(), None, M0, M0, speed=2)


def test_delay_for_speed_pure_mapping():
    assert delay_for_speed(1) == 60.0
    assert delay_for_speed(10) == 6.0
    assert delay_for_speed(60) == 1.0
    assert delay_for_speed("max") == 0.0


# --------------------------------------------------------- historical fetch


async def test_fetch_historical_candles_half_open_ascending(db_conn):
    await _seed(db_conn, [_candle("BTCUSDT", i, 100 + i) for i in range(4)])
    feed = ReplayFeed(["BTCUSDT"], EventBus(), TxPool(db_conn), M0, M0, speed="max")
    got = await feed.fetch_historical_candles(
        "BTCUSDT", "1m", M0, M0 + timedelta(minutes=3))
    assert [c.ts for c in got] == [M0 + timedelta(minutes=i) for i in range(3)]
    assert got[0] == _candle("BTCUSDT", 0, 100)      # exact value round-trip


# ---------------------------------------------------------------- emission


async def test_emits_exact_sequence_with_deterministic_interleave(db_conn):
    seeded = [_candle("BTCUSDT", i, 100 + i) for i in range(5)] + \
             [_candle("ETHUSDT", i, 3500 + i) for i in range(5)]
    await _seed(db_conn, seeded)
    events, feed = await _replay_all(
        db_conn, ["BTCUSDT", "ETHUSDT"], M0, M0 + timedelta(minutes=5))

    one_m = [e for e in events if e.tf == "1m"]
    five_m = [e for e in events if e.tf == "5m"]
    assert len(one_m) == 10 and len(five_m) == 2
    # deterministic (ts, symbol) interleave: BTC before ETH each minute
    assert [(c.symbol, c.ts) for c in one_m] == [
        (s, M0 + timedelta(minutes=i))
        for i in range(5) for s in ("BTCUSDT", "ETHUSDT")
    ]
    assert one_m[0] == _candle("BTCUSDT", 0, 100)    # identical normalized events
    assert feed.connected is False                    # range exhausted


async def test_5m_published_at_a2_boundary_with_correct_fold(db_conn):
    seeded = [_candle("BTCUSDT", i, [100, 105, 95, 102, 103][i]) for i in range(5)]
    await _seed(db_conn, seeded)
    events, _ = await _replay_all(db_conn, ["BTCUSDT"], M0, M0 + timedelta(minutes=5))

    assert [e.tf for e in events] == ["1m"] * 4 + ["1m", "5m"]  # right after minute-4 close
    c5 = events[-1]
    assert c5.ts == M0
    assert (c5.o, c5.h, c5.l, c5.c) == (100, 107, 94, 104)      # o+2 highs / o-1 lows / c=o+1
    assert c5.v == pytest.approx(7.5)
    assert c5.n_trades == 15
    assert c5.taker_buy_v == pytest.approx(2.0)


async def test_gap_across_window_discards_partial(db_conn, caplog):
    seeded = [_candle("BTCUSDT", m, 100 + m) for m in (0, 1, 7, 8, 9)]
    await _seed(db_conn, seeded)
    with caplog.at_level("WARNING"):
        events, _ = await _replay_all(
            db_conn, ["BTCUSDT"], M0, M0 + timedelta(minutes=10))
    five_m = [e for e in events if e.tf == "5m"]
    assert len(five_m) == 1                          # window 0 never published
    assert five_m[0].ts == M0 + timedelta(minutes=5)
    assert five_m[0].n_trades == 9                   # minutes 7,8,9 only
    assert any("partial 5m" in r.message for r in caplog.records)


# ------------------------------------------------------- pipeline identity


async def test_pipeline_identity_live_builder_vs_replay(db_conn):
    """The crown jewel: the same market data through the live path
    (trades -> CandleBuilder) and through storage -> ReplayFeed must yield
    IDENTICAL 1m and 5m event sequences. Guards the self-contained fold
    against ever diverging from the live A2 rules."""
    # live path: two trades per minute for minutes 0..9, close m9 via m10
    live_bus, live_events = await _collecting_bus()
    CandleBuilder(live_bus)
    for m in range(11):
        base = 100.0 + m
        await live_bus.publish(Trade(symbol="BTCUSDT", price=base, qty=0.5,
                                     ts=M0 + timedelta(minutes=m, seconds=5),
                                     is_buyer_maker=(m % 2 == 0)))
        if m < 10:  # second trade shapes h/l/c within the minute
            await live_bus.publish(Trade(symbol="BTCUSDT", price=base + 1.5, qty=0.25,
                                         ts=M0 + timedelta(minutes=m, seconds=40),
                                         is_buyer_maker=False))
    live_1m = [e for e in live_events if e.tf == "1m"]
    live_5m = [e for e in live_events if e.tf == "5m"]
    assert len(live_1m) == 10 and len(live_5m) == 2  # sanity

    # store the live truth candles, then replay them
    await _seed(db_conn, live_1m)
    replay_events, _ = await _replay_all(
        db_conn, ["BTCUSDT"], M0, M0 + timedelta(minutes=10))

    assert [e for e in replay_events if e.tf == "1m"] == live_1m
    assert [e for e in replay_events if e.tf == "5m"] == live_5m  # identical fold


# ------------------------------------------------- lifecycle + determinism


async def test_stop_mid_stream_and_connected_lifecycle(db_conn):
    await _seed(db_conn, [_candle("BTCUSDT", i, 100 + i) for i in range(5)])
    bus, events = await _collecting_bus()
    feed = ReplayFeed(["BTCUSDT"], bus, TxPool(db_conn),
                      M0, M0 + timedelta(minutes=5), speed=60)  # 1s per candle
    assert feed.connected is False
    await feed.start()
    for _ in range(100):
        if feed.connected:
            break
        await asyncio.sleep(0.02)
    assert feed.connected is True                     # streaming
    await asyncio.sleep(0.1)
    await feed.stop()
    assert feed.connected is False
    assert 0 < len(events) < 7                        # halted mid-stream


async def test_double_start_rejected(db_conn):
    feed = ReplayFeed(["BTCUSDT"], EventBus(), TxPool(db_conn),
                      M0, M0 + timedelta(minutes=1), speed="max")
    await feed.start()
    with pytest.raises(RuntimeError):
        await feed.start()
    await feed.stop()


async def test_two_runs_produce_identical_event_lists(db_conn):
    await _seed(db_conn, [_candle("BTCUSDT", i, 100 + i) for i in range(10)]
                + [_candle("ETHUSDT", i, 3500 + i) for i in range(10)])

    async def run():
        events, _ = await _replay_all(
            db_conn, ["BTCUSDT", "ETHUSDT"], M0, M0 + timedelta(minutes=10))
        return events

    assert await run() == await run()


async def test_statestore_updates_from_replay_like_live(db_conn):
    await _seed(db_conn, [_candle("BTCUSDT", i, 100 + i) for i in range(5)])
    bus = EventBus()
    store = StateStore(bus)
    feed = ReplayFeed(["BTCUSDT"], bus, TxPool(db_conn),
                      M0, M0 + timedelta(minutes=5), speed="max")
    await feed.start()
    for _ in range(200):
        if feed._task.done():
            break
        await asyncio.sleep(0.01)
    await feed.stop()
    state = store.snapshot("BTCUSDT")
    assert state.last_candle_1m.ts == M0 + timedelta(minutes=4)
    assert state.last_candle_5m.ts == M0                        # 5m reached the store

"""Tests for the StateStore (roadmap P0.20)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(symbol="BTCUSDT", tf="1m", ts=M0, o=67200.0):
    return Candle(symbol=symbol, tf=tf, ts=ts, o=o, h=o + 30, l=o - 10,
                  c=o + 15, v=12.5, qv=838125.0, n_trades=420, taker_buy_v=7.1)


def _rig():
    bus = EventBus()
    return bus, StateStore(bus)


async def test_candles_update_correct_symbol_and_slots():
    bus, store = _rig()
    c1 = _candle(tf="1m")
    c5 = _candle(tf="5m", o=67100.0)
    await bus.publish(c1)
    await bus.publish(c5)
    state = store.snapshot("BTCUSDT")
    assert state.last_candle_1m == c1
    assert state.last_candle_5m == c5


async def test_symbol_appears_on_first_candle():
    bus, store = _rig()
    assert store.snapshot("BTCUSDT") is None
    await bus.publish(_candle())
    state = store.snapshot("BTCUSDT")
    assert state is not None and state.last_candle_5m is None


async def test_diff_returns_only_changed_fields_and_symbols():
    bus, store = _rig()
    c_btc = _candle(symbol="BTCUSDT", tf="1m")
    c_eth = _candle(symbol="ETHUSDT", tf="5m", o=3500.0)
    await bus.publish(c_btc)
    await bus.publish(c_eth)
    diff = store.diff()
    assert diff == {
        "BTCUSDT": {"last_candle_1m": c_btc},
        "ETHUSDT": {"last_candle_5m": c_eth},
    }


async def test_diff_empty_when_nothing_changed():
    bus, store = _rig()
    await bus.publish(_candle())
    store.diff()                       # consumes the change
    assert store.diff() == {}          # nothing since


async def test_consecutive_updates_collapse_to_latest():
    bus, store = _rig()
    await bus.publish(_candle(ts=M0, o=100.0))
    latest = _candle(ts=M0 + timedelta(minutes=1), o=101.0)
    await bus.publish(latest)
    diff = store.diff()
    assert diff["BTCUSDT"]["last_candle_1m"] == latest    # only the latest survives


async def test_snapshot_returns_copy_not_live_state():
    bus, store = _rig()
    c = _candle()
    await bus.publish(c)
    snap = store.snapshot("BTCUSDT")
    snap.last_candle_1m = None                            # mutate the copy
    assert store.snapshot("BTCUSDT").last_candle_1m == c  # store unaffected


async def test_symbols_are_isolated():
    bus, store = _rig()
    await bus.publish(_candle(symbol="BTCUSDT"))
    await bus.publish(_candle(symbol="ETHUSDT", o=3500.0))
    assert store.snapshot("BTCUSDT").last_candle_1m.o == 67200.0
    assert store.snapshot("ETHUSDT").last_candle_1m.o == 3500.0


async def test_unknown_tf_is_ignored(caplog):
    bus, store = _rig()
    with caplog.at_level("WARNING"):
        await bus.publish(_candle(tf="15m"))
    assert store.snapshot("BTCUSDT") is None
    assert store.diff() == {}
    assert any("unknown tf" in r.message for r in caplog.records)


async def test_deterministic_same_sequence_same_diff_stream():
    seq = [
        _candle(ts=M0, o=100.0),
        _candle(symbol="ETHUSDT", ts=M0, o=3500.0),
        _candle(tf="5m", ts=M0, o=99.0),
        _candle(ts=M0 + timedelta(minutes=1), o=101.0),
    ]

    async def run():
        bus, store = _rig()
        diffs = []
        for event in seq:
            await bus.publish(event)
            diffs.append(store.diff())
        return diffs

    assert await run() == await run()

"""Unit tests for the display-only live forming-bar tracker (chart UX item 5).

Pure — an EventBus + Trade events, no DB, no network. Confirms the forming bar
folds OHLCV correctly, rolls on a new minute, drops out-of-order trades, and
throttles (but always fires on a new bucket).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.core.bus import EventBus
from marketscalper.core.live_bar import (FormingBar, LiveBarTracker,
                                         LiveIndicatorTracker)
from marketscalper.providers.base import Candle, Trade


def _candle(price, ts):
    return Candle(symbol="BTCUSDT", tf="1m", ts=ts, o=price, h=price, l=price,
                  c=price, v=1.0, qv=float(price), n_trades=1, taker_buy_v=0.5)

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _trade(price, ts, qty=1.0):
    return Trade(symbol="BTCUSDT", price=price, qty=qty, ts=ts,
                 is_buyer_maker=False, n_trades=1)


async def _collect(bus):
    seen = []

    async def on(fb):
        seen.append(fb)

    bus.subscribe(FormingBar, on)
    return seen


async def test_current_price_tracks_live_forming_close():
    """Paper V2 (B4): current_price returns the latest live price per symbol
    (None before any trade) so market orders fill live, not at the stale close."""
    bus = EventBus()
    tracker = LiveBarTracker(bus, min_interval_s=0)
    assert tracker.current_price("BTCUSDT") is None      # nothing seen yet
    await bus.publish(_trade(100, M0 + timedelta(seconds=1)))
    await bus.publish(_trade(105, M0 + timedelta(seconds=10)))
    assert tracker.current_price("BTCUSDT") == 105        # latest trade price
    assert tracker.current_price("ETHUSDT") is None       # unknown symbol
    await bus.publish(_trade(98, M0 + timedelta(seconds=20)))
    assert tracker.current_price("BTCUSDT") == 98          # follows the live tick


async def test_forming_folds_ohlcv_within_bucket():
    bus = EventBus()
    seen = await _collect(bus)
    LiveBarTracker(bus, min_interval_s=0)              # no throttle
    await bus.publish(_trade(100, M0 + timedelta(seconds=1)))
    await bus.publish(_trade(105, M0 + timedelta(seconds=10)))
    await bus.publish(_trade(98, M0 + timedelta(seconds=20)))
    await bus.publish(_trade(102, M0 + timedelta(seconds=30)))
    assert len(seen) == 4
    last = seen[-1]
    assert (last.o, last.h, last.l, last.c) == (100, 105, 98, 102)
    assert last.v == 4.0 and last.ts == M0
    assert isinstance(last, FormingBar)


async def test_new_minute_starts_a_fresh_bar():
    bus = EventBus()
    seen = await _collect(bus)
    LiveBarTracker(bus, min_interval_s=0)
    await bus.publish(_trade(100, M0 + timedelta(seconds=30)))
    await bus.publish(_trade(200, M0 + timedelta(minutes=1, seconds=5)))
    assert seen[-1].ts == M0 + timedelta(minutes=1)
    assert (seen[-1].o, seen[-1].c, seen[-1].v) == (200, 200, 1.0)


async def test_out_of_order_trade_ignored():
    bus = EventBus()
    seen = await _collect(bus)
    LiveBarTracker(bus, min_interval_s=0)
    await bus.publish(_trade(100, M0 + timedelta(minutes=1, seconds=5)))
    n = len(seen)
    await bus.publish(_trade(999, M0 + timedelta(seconds=5)))   # older bucket
    assert len(seen) == n                              # dropped


async def test_throttle_but_new_bucket_always_fires():
    bus = EventBus()
    seen = await _collect(bus)
    LiveBarTracker(bus, min_interval_s=999)            # throttle same-bucket updates
    await bus.publish(_trade(100, M0 + timedelta(seconds=1)))   # new bucket -> fires
    await bus.publish(_trade(101, M0 + timedelta(seconds=2)))   # throttled
    await bus.publish(_trade(102, M0 + timedelta(seconds=3)))   # throttled
    assert len(seen) == 1
    await bus.publish(_trade(200, M0 + timedelta(minutes=1)))   # new bucket -> fires
    assert len(seen) == 2 and seen[-1].o == 200


# ---- live indicator tracker (chart UX item 2, live forming stream) ----


async def test_live_indicators_seeded_interim_has_all():
    bus = EventBus()
    seed = {"BTCUSDT": [_candle(float(x), M0) for x in range(1, 260)]}   # 259 closes
    t = LiveIndicatorTracker(bus, ["BTCUSDT"], seed_candles=seed)
    out = t.interim("BTCUSDT", 300.0)
    assert out is not None
    for k in ("ema20", "ema50", "ema200", "rsi"):
        assert k in out
    assert 0.0 <= out["rsi"] <= 100.0


async def test_live_indicators_advance_on_closed_1m_candle():
    bus = EventBus()
    seed = {"BTCUSDT": [_candle(float(x), M0) for x in range(1, 260)]}
    t = LiveIndicatorTracker(bus, ["BTCUSDT"], seed_candles=seed)
    before = t.interim("BTCUSDT", 300.0)["ema20"]
    await bus.publish(_candle(1000.0, M0 + timedelta(minutes=1)))       # closed 1m
    after = t.interim("BTCUSDT", 300.0)["ema20"]
    assert after != before                                             # state advanced


async def test_live_indicators_ignore_5m_candles():
    bus = EventBus()
    seed = {"BTCUSDT": [_candle(float(x), M0) for x in range(1, 260)]}
    t = LiveIndicatorTracker(bus, ["BTCUSDT"], seed_candles=seed)
    before = t.interim("BTCUSDT", 300.0)["ema20"]
    c5 = Candle(symbol="BTCUSDT", tf="5m", ts=M0 + timedelta(minutes=5),
                o=9, h=9, l=9, c=9, v=1.0, qv=9.0, n_trades=1, taker_buy_v=0.5)
    await bus.publish(c5)                                              # 5m must be ignored
    assert t.interim("BTCUSDT", 300.0)["ema20"] == before


async def test_live_indicators_peek_does_not_mutate_state():
    bus = EventBus()
    seed = {"BTCUSDT": [_candle(float(x), M0) for x in range(1, 260)]}
    t = LiveIndicatorTracker(bus, ["BTCUSDT"], seed_candles=seed)
    a = t.interim("BTCUSDT", 300.0)["ema20"]
    t.interim("BTCUSDT", 9999.0)                                       # a wild peek
    assert t.interim("BTCUSDT", 300.0)["ema20"] == a                   # unchanged

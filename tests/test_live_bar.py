"""Unit tests for the display-only live forming-bar tracker (chart UX item 5).

Pure — an EventBus + Trade events, no DB, no network. Confirms the forming bar
folds OHLCV correctly, rolls on a new minute, drops out-of-order trades, and
throttles (but always fires on a new bucket).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.core.bus import EventBus
from marketscalper.core.live_bar import FormingBar, LiveBarTracker
from marketscalper.providers.base import Trade

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

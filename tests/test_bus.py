"""Tests for the EventBus (roadmap P0.9): routing + deterministic ordering."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Tick, Trade

TS = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)


async def test_type_keyed_routing():
    bus = EventBus()
    seen: list[tuple[str, object]] = []

    async def on_trade(e):
        seen.append(("trade", e))

    async def on_tick(e):
        seen.append(("tick", e))

    bus.subscribe(Trade, on_trade)
    bus.subscribe(Tick, on_tick)

    trade = Trade(symbol="BTCUSDT", price=67000.0, qty=0.1, ts=TS, is_buyer_maker=True)
    tick = Tick(symbol="BTCUSDT", price=67001.0, ts=TS)
    await bus.publish(trade)
    await bus.publish(tick)

    assert seen == [("trade", trade), ("tick", tick)]  # each handler only its own type


async def test_delivery_follows_subscription_order_and_is_repeatable():
    bus = EventBus()
    order: list[str] = []

    for label in ("first", "second", "third"):
        async def handler(e, label=label):
            order.append(label)
        bus.subscribe(Tick, handler)

    tick = Tick(symbol="BTCUSDT", price=1.0, ts=TS)
    await bus.publish(tick)
    await bus.publish(tick)
    assert order == ["first", "second", "third"] * 2  # deterministic, run after run


async def test_handlers_are_awaited_sequentially_not_concurrently():
    bus = EventBus()
    order: list[str] = []

    async def slow(e):
        order.append("slow-start")
        await asyncio.sleep(0.01)
        order.append("slow-end")

    async def fast(e):
        order.append("fast")

    bus.subscribe(Tick, slow)
    bus.subscribe(Tick, fast)
    await bus.publish(Tick(symbol="BTCUSDT", price=1.0, ts=TS))

    # fast runs only after slow fully completes -> sequential await, no gather
    assert order == ["slow-start", "slow-end", "fast"]


async def test_publish_without_subscribers_is_a_noop():
    bus = EventBus()
    await bus.publish(Tick(symbol="BTCUSDT", price=1.0, ts=TS))  # must not raise


def test_public_api_is_exactly_subscribe_and_publish():
    public = {n for n in dir(EventBus) if not n.startswith("_")}
    assert public == {"subscribe", "publish"}

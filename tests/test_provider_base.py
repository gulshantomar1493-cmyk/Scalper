"""Tests for the FeedProvider contract + normalized types (roadmap P0.9)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from marketscalper.providers.base import (
    BookTicker,
    Candle,
    Capabilities,
    FeedProvider,
    Tick,
    Trade,
)

UTC = timezone.utc
TS = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def test_capability_flags_have_exact_roadmap_names():
    caps = Capabilities(
        supports_live_data=True,
        supports_historical_data=True,
        supports_orderbook=False,
        supports_trades=True,
    )
    assert caps.supports_live_data and not caps.supports_orderbook
    assert {f.name for f in dataclasses.fields(Capabilities)} == {
        "supports_live_data",
        "supports_historical_data",
        "supports_orderbook",
        "supports_trades",
    }


@pytest.mark.parametrize(
    "instance",
    [
        Capabilities(True, True, True, True),
        Tick(symbol="BTCUSDT", price=67000.0, ts=TS),
        Trade(symbol="BTCUSDT", price=67000.0, qty=0.5, ts=TS, is_buyer_maker=False),
        BookTicker(symbol="BTCUSDT", bid_px=67000.0, bid_qty=1.0,
                   ask_px=67000.5, ask_qty=2.0, ts=TS),
        Candle(symbol="BTCUSDT", tf="1m", ts=TS, o=1, h=2, l=0.5, c=1.5,
               v=10, qv=670000, n_trades=42, taker_buy_v=6),
    ],
)
def test_normalized_types_are_immutable(instance):
    field = dataclasses.fields(instance)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field, "mutated")


def test_candle_mirrors_schema_columns():
    assert [f.name for f in dataclasses.fields(Candle)] == [
        "symbol", "tf", "ts", "o", "h", "l", "c", "v", "qv",
        "n_trades", "taker_buy_v",
    ]


def test_trade_carries_exactly_what_candle_builder_consumes():
    assert [f.name for f in dataclasses.fields(Trade)] == [
        "symbol", "price", "qty", "ts", "is_buyer_maker", "n_trades",
    ]


def test_feedprovider_is_abstract():
    with pytest.raises(TypeError):
        FeedProvider()  # type: ignore[abstract]


def test_incomplete_implementation_is_rejected():
    class Partial(FeedProvider):  # missing fetch_historical_candles etc.
        name = "partial"

        @property
        def capabilities(self) -> Capabilities:
            return Capabilities(True, True, True, True)

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_interface_surface_is_exactly_the_roadmap_contract():
    assert FeedProvider.__abstractmethods__ == {
        "capabilities",
        "connected",
        "start",
        "stop",
        "fetch_historical_candles",
    }


async def test_minimal_complete_implementation_satisfies_contract():
    class Dummy(FeedProvider):
        name = "dummy"

        def __init__(self) -> None:
            self._connected = False

        @property
        def capabilities(self) -> Capabilities:
            return Capabilities(False, True, False, False)

        @property
        def connected(self) -> bool:
            return self._connected

        async def start(self) -> None:
            self._connected = True

        async def stop(self) -> None:
            self._connected = False

        async def fetch_historical_candles(self, symbol, tf, start, end):
            return []

    p = Dummy()
    assert isinstance(p, FeedProvider) and p.connected is False
    await p.start()
    assert p.connected is True
    assert await p.fetch_historical_candles("BTCUSDT", "1m", TS, TS) == []
    await p.stop()
    assert p.connected is False

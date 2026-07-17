"""Network-free tests for BinanceFeed (roadmap P0.10).

Normalization, routing, URL construction, backoff/heartbeat policy math,
interface conformance, and the bus-publishing path — all without a socket.
Live-connection behavior is covered by the P0.19 conformance suite later.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from marketscalper.core.bus import EventBus
from marketscalper.providers.base import BookTicker, Candle, FeedProvider, Trade
from marketscalper.providers.binance import (
    BACKOFF_CAP_S,
    BinanceFeed,
    is_stale,
    next_backoff,
    normalize_message,
    parse_agg_trade,
    parse_book_ticker,
    parse_closed_kline,
    stream_url,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 14, 19, 0, 30, tzinfo=UTC)

AGG_TRADE = {  # Binance aggTrade payload shape
    "e": "aggTrade", "E": 1784055600123, "s": "BTCUSDT", "a": 12345,
    "p": "67215.10", "q": "0.052", "f": 1, "l": 2,
    "T": 1784055600100, "m": False, "M": True,
}

BOOK_TICKER = {"u": 400900217, "s": "BTCUSDT", "b": "67215.00",
               "B": "1.20", "a": "67215.50", "A": "0.80"}

KLINE_CLOSED = {
    "e": "kline", "E": 1784055660005, "s": "BTCUSDT",
    "k": {"t": 1784055600000, "T": 1784055659999, "s": "BTCUSDT", "i": "1m",
          "f": 1, "L": 2, "o": "67200.0", "c": "67215.1", "h": "67230.0",
          "l": "67195.5", "v": "12.5", "n": 420, "x": True, "q": "838125.0",
          "V": "7.1", "Q": "477105.0", "B": "0"},
}


def _open_kline():
    k = {**KLINE_CLOSED, "k": {**KLINE_CLOSED["k"], "x": False}}
    return k


# ------------------------------------------------------------ normalization


def test_parse_agg_trade_maps_exactly_to_trade():
    t = parse_agg_trade(AGG_TRADE)
    assert t == Trade(
        symbol="BTCUSDT", price=67215.10, qty=0.052,
        ts=datetime.fromtimestamp(1784055600.100, tz=UTC), is_buyer_maker=False,
    )


def test_parse_book_ticker_uses_supplied_arrival_time():
    b = parse_book_ticker(BOOK_TICKER, NOW)
    assert b == BookTicker(
        symbol="BTCUSDT", bid_px=67215.00, bid_qty=1.20,
        ask_px=67215.50, ask_qty=0.80, ts=NOW,
    )


def test_parse_closed_kline_maps_exactly_to_candle():
    c = parse_closed_kline(KLINE_CLOSED)
    assert c == Candle(
        symbol="BTCUSDT", tf="1m",
        ts=datetime.fromtimestamp(1784055600.000, tz=UTC),
        o=67200.0, h=67230.0, l=67195.5, c=67215.1,
        v=12.5, qv=838125.0, n_trades=420, taker_buy_v=7.1,
    )


def test_unclosed_kline_is_ignored():
    assert parse_closed_kline(_open_kline()) is None


# ------------------------------------------------------------------ routing


def test_normalize_message_routes_all_three_streams():
    assert isinstance(
        normalize_message({"stream": "btcusdt@aggTrade", "data": AGG_TRADE}, NOW), Trade)
    assert isinstance(
        normalize_message({"stream": "btcusdt@bookTicker", "data": BOOK_TICKER}, NOW), BookTicker)
    assert isinstance(
        normalize_message({"stream": "btcusdt@kline_1m", "data": KLINE_CLOSED}, NOW), Candle)


def test_normalize_message_ignores_unclosed_kline_and_unknown_streams():
    assert normalize_message({"stream": "btcusdt@kline_1m", "data": _open_kline()}, NOW) is None
    assert normalize_message({"stream": "btcusdt@depth", "data": {}}, NOW) is None
    assert normalize_message({"result": None, "id": 1}, NOW) is None  # subscribe ack shape


# ---------------------------------------------------------------- URL/policy


def test_stream_url_builds_all_streams_lowercased():
    url = stream_url(["BTCUSDT", "ETHUSDT"])
    assert url.startswith("wss://stream.binance.com:9443/stream?streams=")
    streams = url.split("=", 1)[1].split("/")
    assert streams == [
        "btcusdt@aggTrade", "btcusdt@kline_1m", "btcusdt@bookTicker",
        "ethusdt@aggTrade", "ethusdt@kline_1m", "ethusdt@bookTicker",
    ]


def test_backoff_doubles_and_caps():
    seq, b = [], 1.0
    for _ in range(7):
        seq.append(b)
        b = next_backoff(b)
    assert seq == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert next_backoff(BACKOFF_CAP_S) == BACKOFF_CAP_S


def test_heartbeat_staleness_boundaries():
    assert is_stale(last_msg_at=100.0, now=131.0, timeout_s=30.0) is True
    assert is_stale(last_msg_at=100.0, now=130.0, timeout_s=30.0) is False
    assert is_stale(last_msg_at=100.0, now=100.0, timeout_s=30.0) is False


# ------------------------------------------------------ provider conformance


def test_binancefeed_satisfies_feedprovider_contract():
    feed = BinanceFeed(["BTCUSDT"], EventBus())
    assert isinstance(feed, FeedProvider)
    assert feed.name == "binance"
    assert feed.connected is False
    caps = feed.capabilities
    assert caps.supports_live_data and caps.supports_trades and caps.supports_orderbook
    assert caps.supports_historical_data is True  # implemented at P0.15


async def test_fetch_historical_candles_rejects_unknown_timeframe():
    feed = BinanceFeed(["BTCUSDT"], EventBus())
    with pytest.raises(ValueError):
        await feed.fetch_historical_candles("BTCUSDT", "15m", NOW, NOW)


async def test_normalized_events_reach_the_bus():
    """The publish path: normalized message -> EventBus -> typed subscriber."""
    bus = EventBus()
    feed = BinanceFeed(["BTCUSDT"], bus)
    seen: list[object] = []

    async def collect(e):
        seen.append(e)

    bus.subscribe(Trade, collect)
    bus.subscribe(Candle, collect)
    bus.subscribe(BookTicker, collect)

    for msg in (
        {"stream": "btcusdt@aggTrade", "data": AGG_TRADE},
        {"stream": "btcusdt@kline_1m", "data": KLINE_CLOSED},
        {"stream": "btcusdt@kline_1m", "data": _open_kline()},   # dropped
        {"stream": "btcusdt@bookTicker", "data": BOOK_TICKER},
    ):
        event = normalize_message(msg, NOW)
        if event is not None:
            await feed._bus.publish(event)

    assert [type(e) for e in seen] == [Trade, Candle, BookTicker]

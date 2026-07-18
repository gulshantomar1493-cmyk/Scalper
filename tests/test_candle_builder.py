"""Tests for the Candle Builder (roadmap P0.12; Architecture §4.1)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.providers.base import Candle, Trade

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, 0, tzinfo=UTC)  # exact minute start


def _trade(ts, price, qty, maker=False, symbol="BTCUSDT"):
    return Trade(symbol=symbol, price=price, qty=qty, ts=ts, is_buyer_maker=maker)


async def _rig(prime=("BTCUSDT",)):
    """Fresh bus + builder + collector of published Candles.

    The builder discards each symbol's first bucket (startup rule, P0.28
    owner decision), so `prime` publishes one throwaway trade per symbol a
    minute before M0 — steady-state tests then behave exactly as specified.
    """
    bus = EventBus()
    closed: list[Candle] = []

    async def collect(c):
        closed.append(c)

    builder = CandleBuilder(bus)
    bus.subscribe(Candle, collect)
    for symbol in prime:
        await bus.publish(_trade(M0 - timedelta(minutes=1), 1, 1, symbol=symbol))
    return bus, builder, closed


async def test_first_trade_opens_without_publishing():
    bus, _, closed = await _rig(prime=())
    await bus.publish(_trade(M0 + timedelta(seconds=5), 67000, 0.5))
    assert closed == []  # open candle is never emitted


async def test_startup_partial_bucket_is_discarded_with_warning(caplog):
    """P0.28 owner decision: the first bucket per symbol after process start
    is never published — the first published candle is fully observed."""
    bus, _, closed = await _rig(prime=())
    await bus.publish(_trade(M0 + timedelta(seconds=39), 67000, 0.5))  # mid-minute start
    with caplog.at_level("WARNING"):
        await bus.publish(_trade(M0 + timedelta(seconds=61), 67010, 0.2))  # rollover
    assert closed == []                                   # startup bucket discarded
    assert any("startup" in r.message for r in caplog.records)
    await bus.publish(_trade(M0 + timedelta(seconds=121), 67020, 0.1))  # next rollover
    assert len(closed) == 1                               # first fully observed minute
    assert closed[0].ts == M0 + timedelta(minutes=1)
    assert (closed[0].o, closed[0].v) == (67010, 0.2)


async def test_ohlc_volume_quote_volume_and_trade_count():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0 + timedelta(seconds=1), 67000, 0.5))   # open
    await bus.publish(_trade(M0 + timedelta(seconds=20), 67100, 0.2))  # high
    await bus.publish(_trade(M0 + timedelta(seconds=40), 66900, 0.3))  # low
    await bus.publish(_trade(M0 + timedelta(seconds=59), 67050, 1.0))  # close
    await bus.publish(_trade(M0 + timedelta(seconds=61), 67060, 0.1))  # rolls bucket
    assert len(closed) == 1
    c = closed[0]
    assert (c.o, c.h, c.l, c.c) == (67000, 67100, 66900, 67050)
    assert c.v == pytest.approx(0.5 + 0.2 + 0.3 + 1.0)
    assert c.qv == pytest.approx(67000 * 0.5 + 67100 * 0.2 + 66900 * 0.3 + 67050 * 1.0)
    assert c.n_trades == 4


async def test_n_trades_sums_raw_counts_not_events():
    """Candle n_trades must follow kline "n" semantics: one aggTrade event
    spanning many raw trades contributes its raw count, not 1."""
    bus, _, closed = await _rig()
    await bus.publish(dataclasses.replace(
        _trade(M0 + timedelta(seconds=1), 67000, 0.5), n_trades=3))
    await bus.publish(dataclasses.replace(
        _trade(M0 + timedelta(seconds=30), 67010, 0.2), n_trades=4))
    await bus.publish(_trade(M0 + timedelta(seconds=61), 67020, 0.1))  # rollover
    assert len(closed) == 1
    assert closed[0].n_trades == 7


async def test_taker_buy_v_counts_only_taker_buys():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0, 67000, 0.5, maker=False))                        # taker buy
    await bus.publish(_trade(M0 + timedelta(seconds=10), 67000, 0.3, maker=True))  # taker sell
    await bus.publish(_trade(M0 + timedelta(seconds=20), 67000, 0.2, maker=False)) # taker buy
    await bus.publish(_trade(M0 + timedelta(seconds=70), 67000, 0.1))              # rollover
    assert len(closed) == 1
    assert closed[0].taker_buy_v == pytest.approx(0.7)   # 0.5 + 0.2, not 0.3
    assert closed[0].v == pytest.approx(1.0)


async def test_rollover_publishes_and_seeds_new_bucket_from_rolling_trade():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0 + timedelta(seconds=30), 67000, 0.5))
    await bus.publish(_trade(M0 + timedelta(seconds=65), 67200, 0.4))  # closes M0
    await bus.publish(_trade(M0 + timedelta(seconds=125), 67300, 0.1))  # closes M0+1m
    assert len(closed) == 2
    second = closed[1]
    assert second.ts == M0 + timedelta(minutes=1)
    assert (second.o, second.c, second.v, second.n_trades) == (67200, 67200, 0.4, 1)


async def test_published_candle_has_bucket_start_utc_and_tf_1m():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0 + timedelta(seconds=42, microseconds=123456), 1, 1))
    await bus.publish(_trade(M0 + timedelta(seconds=61), 1, 1))
    c = closed[0]
    assert c.ts == M0 and c.ts.tzinfo is not None and c.tf == "1m"


async def test_minute_boundary_trade_belongs_to_next_bucket():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0 + timedelta(seconds=59, microseconds=999000), 67000, 1))
    await bus.publish(_trade(M0 + timedelta(seconds=60), 67005, 1))  # exactly :00 -> next bucket
    assert len(closed) == 1
    assert closed[0].ts == M0 and closed[0].n_trades == 1


async def test_gap_produces_no_synthetic_candles():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0, 67000, 1))
    await bus.publish(_trade(M0 + timedelta(minutes=7), 67100, 1))  # 6 empty minutes
    assert len(closed) == 1                                          # only M0 closed
    assert closed[0].ts == M0


async def test_symbols_are_independent():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0, 67000, 1, symbol="BTCUSDT"))
    await bus.publish(_trade(M0 + timedelta(seconds=10), 3500, 2, symbol="ETHUSDT"))
    await bus.publish(_trade(M0 + timedelta(seconds=70), 67100, 1, symbol="BTCUSDT"))
    assert [c.symbol for c in closed] == ["BTCUSDT"]  # ETH still open
    assert closed[0].v == 1


async def test_out_of_order_trade_is_dropped_with_warning(caplog):
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0 + timedelta(minutes=2), 67000, 1))
    with caplog.at_level("WARNING"):
        await bus.publish(_trade(M0, 66000, 5))  # earlier bucket -> dropped
    assert closed == []
    assert any("out-of-order" in r.message for r in caplog.records)
    await bus.publish(_trade(M0 + timedelta(minutes=3), 67100, 1))  # rollover
    assert len(closed) == 1
    assert (closed[0].o, closed[0].v, closed[0].n_trades) == (67000, 1, 1)  # untouched by drop


async def test_published_candles_are_immutable():
    bus, _, closed = await _rig()
    await bus.publish(_trade(M0, 67000, 1))
    await bus.publish(_trade(M0 + timedelta(seconds=61), 67010, 1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        closed[0].c = 0  # a closed candle must never change


async def test_deterministic_same_input_same_output():
    trades = [
        _trade(M0 + timedelta(seconds=s), 67000 + s, 0.1 * (i + 1), maker=(i % 2 == 0))
        for i, s in enumerate((0, 10, 30, 59, 61, 90, 121, 130, 185))
    ]

    async def run():
        bus, _, closed = await _rig()
        for t in trades:
            await bus.publish(t)
        return closed

    assert await run() == await run()  # byte-identical candle sequences


# ------------------------------------------- 5m aggregation (P0.13, rule A2)
# M0 = 19:00 UTC; its minute bucket is divisible by 5, so windows align
# :00-:04, :05-:09, ... exactly as A2's epoch alignment requires.


def _minute(i: int, **kw):
    """One trade in minute M0+i."""
    return _trade(M0 + timedelta(minutes=i, seconds=1), **kw)


async def test_5m_closes_exactly_at_a2_boundary():
    bus, _, closed = await _rig()
    prices = [100, 105, 95, 102, 103]
    for i, p in enumerate(prices):
        await bus.publish(_minute(i, price=p, qty=1))
    assert all(c.tf == "1m" for c in closed)             # nothing 5m yet (:04 still open)
    await bus.publish(_minute(5, price=104, qty=1))      # closes :04 -> A2 boundary fires
    five = [c for c in closed if c.tf == "5m"]
    assert len(five) == 1
    assert five[0].ts == M0                              # window start, not close time
    assert len([c for c in closed if c.tf == "1m"]) == 5


async def test_5m_aggregation_values_are_fold_of_five_1m():
    bus, _, closed = await _rig()
    # (price, qty, maker) per minute — distinct values everywhere
    spec = [(100.0, 1.0, False), (105.0, 2.0, True), (95.0, 3.0, False),
            (102.0, 4.0, True), (103.0, 5.0, False)]
    for i, (p, q, m) in enumerate(spec):
        await bus.publish(_minute(i, price=p, qty=q, maker=m))
    await bus.publish(_minute(5, price=1, qty=1))
    c5 = [c for c in closed if c.tf == "5m"][0]
    assert (c5.o, c5.h, c5.l, c5.c) == (100.0, 105.0, 95.0, 103.0)
    assert c5.v == pytest.approx(15.0)
    assert c5.qv == pytest.approx(sum(p * q for p, q, _ in spec))
    assert c5.n_trades == 5
    assert c5.taker_buy_v == pytest.approx(1.0 + 3.0 + 5.0)  # maker=False minutes only


async def test_no_5m_before_the_boundary_minute_closes():
    bus, _, closed = await _rig()
    for i in range(4):                                    # :00..:03
        await bus.publish(_minute(i, price=100, qty=1))
    await bus.publish(_minute(4, price=100, qty=1))       # closes :03 — not boundary
    assert [c.tf for c in closed] == ["1m"] * 4           # (3+base)%5 != 4 -> no 5m


async def test_1m_published_before_5m_at_the_boundary():
    bus, _, closed = await _rig()
    for i in range(6):
        await bus.publish(_minute(i, price=100, qty=1))
    assert [c.tf for c in closed][-2:] == ["1m", "5m"]    # §4.1 order: close 1m, then roll


async def test_mid_window_start_discards_incomplete_5m(caplog):
    """D7 fix (verified scenario): a window entered mid-way is false data
    — before the fix it published 'with what it received'."""
    bus, _, closed = await _rig()
    with caplog.at_level("WARNING"):
        for i in (2, 3, 4, 5):                            # builder starts at :02
            await bus.publish(_minute(i, price=100 + i, qty=1))
    assert [c for c in closed if c.tf == "5m"] == []
    assert any("incomplete 5m candle" in r.message for r in caplog.records)


async def test_gap_across_window_boundary_discards_both_partials(caplog):
    """D7 fix: the cut window w0 (existing discard) AND the mid-entered
    resume window w1 (seeded at :07) are both incomplete — neither
    publishes."""
    bus, _, closed = await _rig()
    await bus.publish(_minute(0, price=100, qty=1))
    await bus.publish(_minute(1, price=101, qty=1))       # closes :00 -> folds w0
    await bus.publish(_minute(7, price=107, qty=1))       # closes :01 -> folds w0
    with caplog.at_level("WARNING"):
        await bus.publish(_minute(8, price=108, qty=1))   # closes :07 -> w1 != w0: discard w0
        await bus.publish(_minute(9, price=109, qty=1))   # closes :08 -> folds w1
        await bus.publish(_minute(10, price=110, qty=1))  # closes :09 -> boundary
    assert any("partial 5m aggregate" in r.message for r in caplog.records)
    assert any("incomplete 5m candle" in r.message for r in caplog.records)
    assert [c for c in closed if c.tf == "5m"] == []      # neither published


async def test_hole_inside_window_discards_incomplete_5m(caplog):
    """D7 fix (verified scenario): a minute missing INSIDE the window
    silently corrupted the published aggregates before the fix."""
    bus, _, closed = await _rig()
    with caplog.at_level("WARNING"):
        for i in (0, 2, 3, 4, 5):                         # :01 never trades
            await bus.publish(_minute(i, price=100 + i, qty=1))
    assert [c for c in closed if c.tf == "5m"] == []
    assert any("incomplete 5m candle" in r.message for r in caplog.records)


async def test_next_complete_window_publishes_after_a_discard(caplog):
    """Recovery: an incomplete window never poisons the following one —
    the first fully observed window publishes normally."""
    bus, _, closed = await _rig()
    with caplog.at_level("WARNING"):
        for i in (2, 3, 4, 5, 6, 7, 8, 9, 10):            # :00-:01 missing
            await bus.publish(_minute(i, price=100 + i, qty=1))
    five = [c for c in closed if c.tf == "5m"]
    assert len(five) == 1                                  # w1 only
    assert five[0].ts == M0 + timedelta(minutes=5)
    assert five[0].n_trades == 5 and five[0].o == 105      # :05..:09 complete


async def test_5m_symbols_are_independent():
    bus, _, closed = await _rig()
    for i in range(6):
        await bus.publish(_minute(i, price=100, qty=1, symbol="BTCUSDT"))
    await bus.publish(_minute(0, price=3500, qty=1, symbol="ETHUSDT"))
    five = [c for c in closed if c.tf == "5m"]
    assert [c.symbol for c in five] == ["BTCUSDT"]         # ETH window still open


async def test_5m_determinism_same_input_same_output():
    seq = [(0, 100), (1, 105), (2, 95), (7, 102), (8, 104), (9, 103), (10, 101), (11, 99)]

    async def run():
        bus, _, closed = await _rig()
        for i, p in seq:
            await bus.publish(_minute(i, price=p, qty=1))
        return closed

    assert await run() == await run()


# ------------------------------------------------ P0.18 gap-fill coverage


async def test_same_timestamp_trades_both_count():
    """Two trades sharing one exact timestamp belong to the same candle."""
    bus, _, closed = await _rig()
    ts = M0 + timedelta(seconds=10)
    await bus.publish(_trade(ts, 67000, 0.5, maker=False))
    await bus.publish(_trade(ts, 67005, 0.3, maker=True))   # identical ts
    await bus.publish(_trade(M0 + timedelta(seconds=70), 67010, 0.1))  # rollover
    assert len(closed) == 1
    c = closed[0]
    assert c.n_trades == 2                                   # both counted
    assert c.v == pytest.approx(0.8)
    assert (c.o, c.c) == (67000, 67005)                      # second is the close
    assert c.taker_buy_v == pytest.approx(0.5)               # maker trade excluded


async def test_interleaved_multi_symbol_sequence_1m_5m_deterministic():
    """A longer BTC/ETH-interleaved tick sequence: correct per-symbol 1m and
    5m outputs, and byte-identical across runs."""
    trades = []
    for i in range(6):  # minutes 0..5; minute-5 trades close minute 4 (A2 boundary)
        trades.append(_trade(M0 + timedelta(minutes=i, seconds=1), 100 + i, 1.0,
                             maker=(i % 2 == 0), symbol="BTCUSDT"))
        trades.append(_trade(M0 + timedelta(minutes=i, seconds=2), 3500 + i, 2.0,
                             maker=(i % 2 == 1), symbol="ETHUSDT"))

    async def run():
        bus, _, closed = await _rig(prime=("BTCUSDT", "ETHUSDT"))
        for t in trades:
            await bus.publish(t)
        return closed

    closed = await run()
    one_m = [c for c in closed if c.tf == "1m"]
    five_m = [c for c in closed if c.tf == "5m"]
    for symbol, base in (("BTCUSDT", 100), ("ETHUSDT", 3500)):
        mine = [c for c in one_m if c.symbol == symbol]
        assert [c.ts for c in mine] == [M0 + timedelta(minutes=i) for i in range(5)]
        assert [c.o for c in mine] == [base + i for i in range(5)]   # no cross-symbol bleed
    assert {(c.symbol, c.ts) for c in five_m} == {("BTCUSDT", M0), ("ETHUSDT", M0)}
    btc5 = next(c for c in five_m if c.symbol == "BTCUSDT")
    assert (btc5.o, btc5.c, btc5.n_trades) == (100, 104, 5)
    assert closed == await run()                              # deterministic


async def test_fully_skipped_5m_window_no_phantom_no_warning(caplog):
    """Gap landing exactly after a boundary close: the skipped window w1
    yields no 5m candle and no discard warning (nothing was partial)."""
    bus, _, closed = await _rig()
    with caplog.at_level("WARNING"):
        for i in range(5):                                   # minutes 0..4 (w0)
            await bus.publish(_minute(i, price=100 + i, qty=1))
        await bus.publish(_minute(10, price=200, qty=1))     # closes :04 -> w0 publishes;
                                                             # w1 (:05-:09) never opens
        for i in (11, 12, 13, 14, 15):                       # w2 completes at :14 close
            await bus.publish(_minute(i, price=200 + i, qty=1))
    five_m = [c for c in closed if c.tf == "5m"]
    assert [c.ts for c in five_m] == [M0, M0 + timedelta(minutes=10)]  # w0 and w2 only
    assert not any("partial 5m" in r.message for r in caplog.records)  # clean skip

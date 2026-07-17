"""Tests for the shared momentum utilities (roadmap P1.1) — IncrementalATR.

Validation strategy (P1.1 task plan):
  1. a hand-computed reference vector with the full arithmetic documented
     inline (spreadsheet-reproducible), and
  2. an independent naive batch implementation of the same textbook
     definition, cross-checked over a longer varied deterministic sequence.
The naive implementation replaces the originally planned memory-cited
"published" vector so every expected number in this file is reproducible
from the formula alone — no unverifiable provenance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(h, l, c, i=0, tf="1m"):
    """Minimal valid Candle; ATR reads only h/l/c (o and volumes are inert)."""
    return Candle(symbol="BTCUSDT", tf=tf, ts=M0 + timedelta(minutes=i),
                  o=c, h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=float(c), n_trades=1, taker_buy_v=0.5)


# ------------------------------------------------------------- warm-up


def test_warmup_returns_none_then_first_value_at_candle_15():
    atr = IncrementalATR()                       # period=14
    results = [atr.update(_candle(105, 95, 100, i)) for i in range(15)]
    assert results[:14] == [None] * 14           # candle 1 + 13 TRs -> None
    assert results[14] == pytest.approx(10.0)    # all TRs = h-l = 10
    assert atr.value == results[14]


def test_value_mirrors_last_update_and_starts_none():
    atr = IncrementalATR(period=1)
    assert atr.value is None
    atr.update(_candle(105, 95, 100, 0))
    assert atr.value is None                     # first candle: no TR yet
    out = atr.update(_candle(103, 98, 100, 1))
    assert atr.value == out == pytest.approx(5.0)


# ------------------------------------------- hand-computed reference vector
#
# period=14. Candle 1: (h=105, l=95, c=100) -> no TR. Candles 2..15 all
# close at 100 with prev_close=100 inside [l, h], so TR = h - l exactly:
#   TRs = [10,12,8,14,6,10,12,8,14,6,10,12,8,14], sum = 144
#   seed ATR (candle 15) = 144/14 = 10.285714285714286
# Candle 16: (103.5, 96.5, 100) -> TR = 7
#   ATR = (10.285714285714286*13 + 7)/14 = 140.7142857142857/14
#       = 10.051020408163265
# Candle 17: (110.5, 89.5, 100) -> TR = 21
#   ATR = (10.051020408163265*13 + 21)/14 = 151.66326530612245/14
#       = 10.833090379008747

_TRS = [10, 12, 8, 14, 6, 10, 12, 8, 14, 6, 10, 12, 8, 14]


def test_hand_computed_reference_vector_step_by_step():
    atr = IncrementalATR()
    out = [atr.update(_candle(105, 95, 100, 0))]
    for i, tr in enumerate(_TRS, start=1):
        out.append(atr.update(_candle(100 + tr / 2, 100 - tr / 2, 100, i)))
    out.append(atr.update(_candle(103.5, 96.5, 100, 15)))
    out.append(atr.update(_candle(110.5, 89.5, 100, 16)))
    assert out[:14] == [None] * 14
    assert out[14] == pytest.approx(10.285714285714286, rel=1e-9)
    assert out[15] == pytest.approx(10.051020408163265, rel=1e-9)
    assert out[16] == pytest.approx(10.833090379008747, rel=1e-9)


def test_custom_period_3_hand_computed():
    # c2 TR=max(20,10,10)=20 · c3 TR=max(8,7,1)=8 · c4 TR=max(10,5,5)=10
    # seed = 38/3 = 12.666666666666666
    # c5 TR=max(4,1,3)=4 -> (12.666666666666666*2 + 4)/3 = 9.777777777777779
    atr = IncrementalATR(period=3)
    assert atr.update(_candle(105, 95, 100, 0)) is None
    assert atr.update(_candle(110, 90, 105, 1)) is None
    assert atr.update(_candle(112, 104, 110, 2)) is None
    assert atr.update(_candle(115, 105, 112, 3)) == pytest.approx(
        12.666666666666666, rel=1e-9)
    assert atr.update(_candle(113, 109, 111, 4)) == pytest.approx(
        9.777777777777779, rel=1e-9)


# ------------------------------------------------- TR component selection
# period=1 makes ATR = TR of the current candle, exposing TR directly.


def test_tr_gap_up_uses_high_minus_prev_close():
    atr = IncrementalATR(period=1)
    atr.update(_candle(105, 95, 100, 0))
    assert atr.update(_candle(120, 115, 118, 1)) == pytest.approx(20.0)  # |120-100|


def test_tr_gap_down_uses_low_minus_prev_close():
    atr = IncrementalATR(period=1)
    atr.update(_candle(105, 95, 100, 0))
    assert atr.update(_candle(85, 80, 82, 1)) == pytest.approx(20.0)     # |80-100|


def test_tr_inside_bar_uses_high_minus_low():
    atr = IncrementalATR(period=1)
    atr.update(_candle(105, 95, 100, 0))
    assert atr.update(_candle(103, 98, 100, 1)) == pytest.approx(5.0)    # 103-98


def test_zero_range_candles_yield_zero_tr():
    atr = IncrementalATR(period=1)
    atr.update(_candle(100, 100, 100, 0))
    assert atr.update(_candle(100, 100, 100, 1)) == pytest.approx(0.0)


# ------------------------------------------------------------ stream gaps


def test_stream_gap_neither_resets_state_nor_value():
    atr = IncrementalATR(period=3)
    atr.update(_candle(105, 95, 100, 0))
    for i in range(1, 4):                                  # warm: TRs 10,10,10
        atr.update(_candle(105, 95, 100, i))
    assert atr.value == pytest.approx(10.0)
    # 7-minute hole in the stream; contained range -> TR = h-l = 4
    out = atr.update(_candle(102, 98, 100, 11))
    assert out == pytest.approx((10.0 * 2 + 4) / 3, rel=1e-9)


# ------------------------------------------------- independence & agnosticism


def test_instances_are_independent():
    a, b = IncrementalATR(period=1), IncrementalATR(period=1)
    a.update(_candle(105, 95, 100, 0))
    a.update(_candle(103, 98, 100, 1))
    assert a.value == pytest.approx(5.0)
    assert b.value is None                                 # untouched


def test_timeframe_is_opaque_same_math_on_5m_candles():
    atr1, atr5 = IncrementalATR(period=3), IncrementalATR(period=3)
    seq = [(105, 95, 100), (110, 90, 105), (112, 104, 110), (115, 105, 112)]
    for i, (h, l, c) in enumerate(seq):
        r1 = atr1.update(_candle(h, l, c, i, tf="1m"))
        r5 = atr5.update(_candle(h, l, c, i * 5, tf="5m"))
        assert r1 == r5


def test_period_below_1_rejected():
    with pytest.raises(ValueError):
        IncrementalATR(period=0)


# ----------------------------------------------------------- determinism


def _varied_candles(n):
    """Deterministic varied sequence (gaps, trends, inside bars) — no RNG."""
    out = []
    for i in range(n):
        c = 100.0 + ((i * 7) % 13) - 6
        h = c + ((i * 5) % 7)
        l = c - ((i * 3) % 5)
        out.append(_candle(h, l, c, i))
    return out


def test_same_stream_twice_is_bit_identical():
    candles = _varied_candles(60)
    a1, a2 = IncrementalATR(), IncrementalATR()
    out1 = [a1.update(c) for c in candles]
    out2 = [a2.update(c) for c in candles]
    assert out1 == out2                                    # exact, not approx


# ------------------------------------- independent naive implementation


def _naive_atr(candles, period=14):
    """Batch textbook Wilder ATR: independent code path, same convention.

    TRs from candle 2; seed = SMA of first `period` TRs; then RMA. Returns
    one output per input candle (None while warming) in identical
    left-to-right float-operation order, so equality is exact."""
    closes = [c.c for c in candles]
    out = [None]
    trs = []
    atr = None
    for i in range(1, len(candles)):
        c = candles[i]
        tr = max(c.h - c.l, abs(c.h - closes[i - 1]), abs(c.l - closes[i - 1]))
        if atr is None:
            trs.append(tr)
            if len(trs) < period:
                out.append(None)
                continue
            seed = 0.0
            for t in trs:                                  # same order as +=
                seed += t
            atr = seed / period
        else:
            atr = (atr * (period - 1) + tr) / period
        out.append(atr)
    return out


def test_incremental_matches_independent_naive_implementation():
    candles = _varied_candles(60)
    atr = IncrementalATR()
    incremental = [atr.update(c) for c in candles]
    assert incremental == _naive_atr(candles, period=14)   # exact equality

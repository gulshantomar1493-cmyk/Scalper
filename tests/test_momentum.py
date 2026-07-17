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

from marketscalper.engines.momentum import (
    IncrementalATR,
    MomentumState,
    RegimeClassifier,
    RegimeConfig,
    classify_regime,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(h, l, c, i=0, tf="1m", o=None):
    """Minimal valid Candle; volumes are inert for the momentum utilities."""
    return Candle(symbol="BTCUSDT", tf=tf, ts=M0 + timedelta(minutes=i),
                  o=float(c if o is None else o), h=float(h), l=float(l),
                  c=float(c), v=1.0, qv=float(c), n_trades=1, taker_buy_v=0.5)


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


# ====================================================== MomentumState (P1.2)
# Update-order contract (pinned): the ATR is updated BEFORE MomentumState
# for every candle; every helper below follows it.


def _ms(period=1, ratio=0.1):
    atr = IncrementalATR(period=period)
    return atr, MomentumState(atr, shift_accel_atr_ratio=ratio)


def _feed(atr, ms, candle):
    atr.update(candle)                       # pinned order: ATR first
    ms.update(candle)


def _closes(atr, ms, closes, span=1.0, start=0):
    """Feed candles with given closes; h/l = c +/- span (contains prev close
    for small deltas, keeping ATR simple where tests want it simple)."""
    for j, c in enumerate(closes):
        _feed(atr, ms, _candle(c + span, c - span, c, start + j))


def test_velocity_warmup_none_through_candle_5_then_sma_seed():
    # closes 100,102,101,104,103,106 -> deltas +2,-1,+3,-1,+3; SMA = 6/5 = 1.2
    atr, ms = _ms()
    for j, c in enumerate([100, 102, 101, 104, 103]):
        _feed(atr, ms, _candle(c + 1, c - 1, c, j))
        assert ms.velocity is None
    _feed(atr, ms, _candle(107, 105, 106, 5))
    assert ms.velocity == pytest.approx(1.2, rel=1e-9)


def test_velocity_ema_continuation_hand_computed():
    # seed 1.2 (above); alpha = 1/3:
    # c7 close 105, d=-1: v = -1/3 + (2/3)*1.2            = 0.4666666666666667
    # c8 close 108, d=+3: v = 1 + (2/3)*0.4666666666666667 = 1.3111111111111111
    # c9 close 107, d=-1: v = -1/3 + (2/3)*1.3111111111111111
    #                                                      = 0.5407407407407408
    atr, ms = _ms()
    _closes(atr, ms, [100, 102, 101, 104, 103, 106])
    _feed(atr, ms, _candle(106, 104, 105, 6))
    assert ms.velocity == pytest.approx(0.4666666666666667, rel=1e-9)
    _feed(atr, ms, _candle(109, 107, 108, 7))
    assert ms.velocity == pytest.approx(1.3111111111111111, rel=1e-9)
    _feed(atr, ms, _candle(108, 106, 107, 8))
    assert ms.velocity == pytest.approx(0.5407407407407408, rel=1e-9)


def test_acceleration_none_until_candle_7_then_velocity_diff():
    atr, ms = _ms()
    _closes(atr, ms, [100, 102, 101, 104, 103, 106])
    assert ms.velocity is not None and ms.acceleration is None   # candle 6
    _feed(atr, ms, _candle(106, 104, 105, 6))                    # candle 7
    assert ms.acceleration == pytest.approx(0.4666666666666667 - 1.2, rel=1e-9)


def test_shift_true_on_flip_bar_only():
    # v = 1.0 after five +1 deltas; crash candle 7: d = -15
    # v7 = -5 + 2/3 = -4.333...; accel = -5.333...; ATR(1) TR = 16 -> thr 1.6
    atr, ms = _ms()
    _closes(atr, ms, [100, 101, 102, 103, 104, 105])
    assert ms.velocity == pytest.approx(1.0)
    _feed(atr, ms, _candle(91, 89, 90, 6))                       # flip bar
    assert ms.momentum_shift is True
    _feed(atr, ms, _candle(91, 89, 90, 7))                       # next bar
    assert ms.momentum_shift is False                            # not sticky


def test_shift_suppressed_below_threshold():
    # gentle flip: d = -2.4 -> v = -0.13333 (flip), accel = -1.13333
    # candle range widened so ATR(1) TR = 16 -> threshold 1.6 > |accel|
    atr, ms = _ms()
    _closes(atr, ms, [100, 101, 102, 103, 104, 105])
    _feed(atr, ms, _candle(106, 90, 102.6, 6))
    assert ms.velocity < 0                                       # flipped
    assert ms.momentum_shift is False                            # too weak


def test_shift_suppressed_without_sign_flip():
    # accelerating up: d = +15, accel large, no flip
    atr, ms = _ms()
    _closes(atr, ms, [100, 101, 102, 103, 104, 105])
    _feed(atr, ms, _candle(121, 119, 120, 6))
    assert ms.acceleration is not None and abs(ms.acceleration) > 1
    assert ms.momentum_shift is False


def test_shift_suppressed_while_atr_unwarm():
    # same crash as the true-positive test, but ATR(period=20) never warms
    atr, ms = _ms(period=20)
    _closes(atr, ms, [100, 101, 102, 103, 104, 105])
    _feed(atr, ms, _candle(91, 89, 90, 6))
    assert atr.value is None and ms.momentum_shift is False


def test_zero_velocity_never_flips():
    # deltas +1,+1,-1,-1,0 -> seed v = 0.0; then d = -3 -> v < 0, but the
    # strict sign rule means 0 -> negative is NOT a flip
    atr, ms = _ms()
    _closes(atr, ms, [100, 101, 102, 101, 100, 100])
    assert ms.velocity == pytest.approx(0.0)
    _feed(atr, ms, _candle(98, 96, 97, 6))
    assert ms.velocity < 0
    assert ms.momentum_shift is False


def test_body_dominance_hand_computed_and_sliding():
    # bodies/ranges: 0.4, 0.2, 1.0, 0.0 (zero-range), 0.6 -> mean 0.44
    atr, ms = _ms()
    specs = [(105, 95, 104, 100),    # body 4 / range 10 = 0.4
             (105, 95, 101, 99),     # 2/10 = 0.2
             (105, 95, 105, 95),     # 10/10 = 1.0
             (100, 100, 100, 100),   # zero-range -> 0.0
             (105, 95, 98, 104)]     # 6/10 = 0.6
    for j, (h, l, c, o) in enumerate(specs):
        assert ms.body_dominance is None
        _feed(atr, ms, _candle(h, l, c, j, o=o))
    assert ms.body_dominance == pytest.approx(0.44, rel=1e-9)
    # candle 6 (0.4 body/range 0.8/2.0) slides out the first 0.4 value:
    _feed(atr, ms, _candle(99, 97, 97.8, 5, o=98.6))
    assert ms.body_dominance == pytest.approx((0.2 + 1.0 + 0.0 + 0.6 + 0.4) / 5,
                                              rel=1e-9)


def test_momentum_determinism_same_stream_twice():
    candles = _varied_candles(60)
    outs = []
    for _ in range(2):
        atr, ms = _ms(period=14)
        run = []
        for c in candles:
            _feed(atr, ms, c)
            run.append((ms.velocity, ms.acceleration,
                        ms.momentum_shift, ms.body_dominance))
        outs.append(run)
    assert outs[0] == outs[1]                                   # exact


def test_momentum_instances_are_independent():
    atr, ms = _ms()
    _closes(atr, ms, [100, 101, 102, 103, 104, 105])
    atr2, ms2 = _ms()
    assert ms2.velocity is None and ms2.acceleration is None
    assert ms2.momentum_shift is False and ms2.body_dominance is None


# ==================================================== RegimeClassifier (P1.4)
# Cadence contract (pinned): the ATRs are updated BEFORE classifier.update()
# for every closed 1m candle; the helpers below follow it.

_CFG = RegimeConfig()


def test_classify_regime_truth_table_and_precedence():
    # expansion: atr_1m > 1.5*median · coil: atr_1m < 0.6*atr_5m
    assert classify_regime(16.0, 10.0, 10.0, _CFG) == "expansion"   # 16 > 15
    assert classify_regime(5.0, 10.0, 10.0, _CFG) == "coil"         # 5 < 6
    assert classify_regime(10.0, 10.0, 10.0, _CFG) == "normal"
    # both conditions true -> expansion wins (D9 precedence)
    assert classify_regime(16.0, 100.0, 10.0, _CFG) == "expansion"  # also 16 < 60
    # any unwarm input -> None
    assert classify_regime(None, 10.0, 10.0, _CFG) is None
    assert classify_regime(10.0, None, 10.0, _CFG) is None
    assert classify_regime(10.0, 10.0, None, _CFG) is None
    # strict inequalities: boundary equality is normal
    assert classify_regime(15.0, 10.0, 10.0, _CFG) == "normal"      # == 1.5*median
    assert classify_regime(15.0, 25.0, 15.0, _CFG) == "normal"      # == 0.6*atr_5m


def _tr_candle(tr, i):
    """Candle whose TR is exactly `tr` for a period=1 ATR (contained range)."""
    return _candle(100 + tr / 2, 100 - tr / 2, 100, i)


def _warm_atr5(value=10.0):
    """period=1 ATR warmed to exactly `value`."""
    atr = IncrementalATR(period=1)
    atr.update(_candle(105, 95, 100, 0, tf="5m"))
    atr.update(_candle(100 + value / 2, 100 - value / 2, 100, 5, tf="5m"))
    assert atr.value == pytest.approx(value)
    return atr


def _drive(rc, atr_1m, trs, start=1):
    """Feed 1m candles with the given TRs (ATR first), classify after each."""
    out = []
    for j, tr in enumerate(trs):
        atr_1m.update(_tr_candle(tr, start + j))
        out.append(rc.update())
    return out


def test_median_window_slide_and_classification_hand_computed():
    # window=4, expansion_ratio=1.0 -> expansion iff atr_1m > median.
    # atr_5m tiny (0.5): coil needs atr_1m < 0.3 — never true here.
    cfg = RegimeConfig(expansion_ratio=1.0, median_window_bars=4)
    atr1 = IncrementalATR(period=1)
    rc = RegimeClassifier("BTCUSDT", atr1, _warm_atr5(0.5), cfg)
    atr1.update(_tr_candle(10, 0))              # first candle: ATR unwarm
    assert rc.update() is None                  # window empty -> unknown
    # TRs 10,20,30,40: window fills at the 4th value; median (20+30)/2 = 25
    out = _drive(rc, atr1, [10, 20, 30, 40])
    assert out == [None, None, None, "expansion"]        # 40 > 25
    # slide: TR=22 evicts 10 -> sorted [20,22,30,40], median 26 -> normal
    assert _drive(rc, atr1, [22], start=5) == ["normal"]
    assert rc.counts == {"coil": 0, "normal": 1, "expansion": 1, "unknown": 4}


def test_unknown_until_all_three_inputs_warm():
    # full median window but atr_5m never warm -> still None (D9)
    cfg = RegimeConfig(median_window_bars=2)
    atr1 = IncrementalATR(period=1)
    atr5_unwarm = IncrementalATR(period=20)
    rc = RegimeClassifier("BTCUSDT", atr1, atr5_unwarm, cfg)
    atr1.update(_tr_candle(10, 0))
    rc.update()
    out = _drive(rc, atr1, [10, 10])
    assert out == [None, None]
    assert rc.counts["unknown"] == 3


def test_precedence_with_real_atr_instances():
    # window=2: TRs 10,10 -> median 10 -> coil (10 < 0.6*100 = 60);
    # then TR=40 -> window [10,40], median 25 -> 40 > 37.5 (expansion)
    # AND 40 < 60 (coil) -> expansion wins.
    cfg = RegimeConfig(median_window_bars=2)
    atr1 = IncrementalATR(period=1)
    rc = RegimeClassifier("BTCUSDT", atr1, _warm_atr5(100.0), cfg)
    atr1.update(_tr_candle(10, 0))
    assert rc.update() is None
    assert _drive(rc, atr1, [10, 10, 40]) == [None, "coil", "expansion"]


def test_summary_line_contents_and_log_levels(caplog):
    cfg = RegimeConfig(median_window_bars=2)
    atr1 = IncrementalATR(period=1)
    rc = RegimeClassifier("BTCUSDT", atr1, _warm_atr5(100.0), cfg)
    atr1.update(_tr_candle(10, 0))
    with caplog.at_level("DEBUG", logger="marketscalper.engines.momentum"):
        rc.update()
        _drive(rc, atr1, [10, 10])
    debug_lines = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debug_lines) == 3                      # one per bar, DEBUG only
    caplog.clear()
    with caplog.at_level("INFO", logger="marketscalper.engines.momentum"):
        rc.log_summary("2026-04-18..2026-07-17")
    assert len(caplog.records) == 1                   # exactly one INFO line
    line = caplog.records[0].getMessage()
    assert "BTCUSDT" in line and "2026-04-18..2026-07-17" in line
    assert "unknown=2" in line and "coil=1" in line
    assert "(33.3%)" in line and "(66.7%)" in line
    assert "compression_ratio=0.6" in line
    assert "expansion_ratio=1.5" in line and "median_window_bars=2" in line


def test_regime_determinism_same_stream_twice():
    candles = _varied_candles(60)
    runs = []
    for _ in range(2):
        atr1, atr5 = IncrementalATR(period=3), IncrementalATR(period=3)
        rc = RegimeClassifier("BTCUSDT", atr1, atr5,
                              RegimeConfig(median_window_bars=10))
        seq = []
        for i, c in enumerate(candles):
            atr1.update(c)
            if i % 5 == 4:                            # every 5th bar as "5m"
                atr5.update(c)
            seq.append(rc.update())
        runs.append((seq, rc.counts))
    assert runs[0] == runs[1]


def test_regime_classifiers_are_independent():
    atr1 = IncrementalATR(period=1)
    rc = RegimeClassifier("BTCUSDT", atr1, _warm_atr5(), RegimeConfig())
    atr1.update(_tr_candle(10, 0))
    rc.update()
    rc2 = RegimeClassifier("ETHUSDT", IncrementalATR(), IncrementalATR(),
                           RegimeConfig())
    assert rc2.regime is None
    assert rc2.counts == {"coil": 0, "normal": 0, "expansion": 0, "unknown": 0}

"""Tests for the hypothetical outcome evaluator (§7; D22/A16.2; P4.4).

Candle-geometry edge cases with hand-computed R multiples: clean SL/TP,
same-candle SL-first ambiguity, gap-through at the open (SL / TP1 / TP2
skip), fill-on-gap, entry-never-filled, horizon mark-to-market, MAE/MFE,
and the SHORT mirror. The creation bar (index 0) is never an entry bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.engines.evaluator import Outcome, evaluate_outcome
from marketscalper.providers.base import Candle

UTC = timezone.utc
T0 = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _c(i, o, h, l, c):
    return Candle(symbol="BTCUSDT", tf="1m", ts=T0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _eval(candles, direction="LONG", entry=100.0, sl=99.0, tp1=102.0,
          tp2=None, **kw):
    return evaluate_outcome(direction=direction, entry=entry, sl=sl,
                            tp1=tp1, tp2=tp2, candles=candles, **kw)


# ----------------------------------------------------------- LONG basics


def test_long_clean_tp1():
    # creation bar (no fill), fill bar touches 100, then rises to 102
    out = _eval([_c(0, 101, 101.5, 100.5, 101),      # creation, no entry
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill at 100
                 _c(2, 100.2, 102.3, 100.1, 102.1)]) # hits tp1 102
    assert out.outcome == "tp1"
    assert out.eval_r == 2.0                          # (102-100)/(100-99)
    assert out.filled and out.fill_index == 1 and out.exit_index == 2
    assert out.eval_mfe >= 2.0 and out.eval_mae <= 0.0


def test_long_clean_sl():
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 100.2, 100.3, 98.9, 99.0)])    # hits sl 99
    assert out.outcome == "sl" and out.eval_r == -1.0
    assert out.eval_mae <= -1.0


def test_long_same_candle_sl_first():
    # one bar spans BOTH sl 99 and tp1 102 -> SL wins (§7 worst case)
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 100.0, 102.5, 98.5, 100.0)])   # spans sl & tp1
    assert out.outcome == "sl" and out.eval_r == -1.0


def test_long_gap_through_sl_at_open():
    # bar OPENS below sl (98) -> sl resolved at the open, worse than -1R
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 98.0, 98.5, 97.0, 97.5)])      # gap-down open 98
    assert out.outcome == "sl"
    assert out.eval_r == -2.0                          # (98-100)/1


def test_long_gap_through_tp1_at_open():
    # bar OPENS above tp1 (103) -> tp1 at the open, better than 2R
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 103.0, 103.5, 102.8, 103.2)])  # gap-up open 103
    assert out.outcome == "tp1" and out.eval_r == 3.0  # (103-100)/1


def test_long_gap_skips_tp1_to_tp2():
    # tp2 = 104, bar opens at 105 -> outcome tp2 (gap-skip case)
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 105.0, 105.5, 104.8, 105.2)],  # gap past tp2
                tp2=104.0)
    assert out.outcome == "tp2" and out.eval_r == 5.0  # (105-100)/1


def test_long_gap_open_beyond_sl_wins_over_intrabar_tp():
    # gap-open below sl AND the bar later prints tp1 -> still sl (§7)
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),   # fill 100
                 _c(2, 98.0, 102.5, 97.5, 102.0)])    # gap sl then rally
    assert out.outcome == "sl" and out.eval_r == -2.0


# ------------------------------------------------------------ entry model


def test_entry_never_filled_is_none():
    # price stays above entry 100 the whole window -> never triggered
    out = _eval([_c(0, 105, 105.5, 104.5, 105),
                 _c(1, 105, 105.5, 104.5, 105),
                 _c(2, 105, 105.5, 104.5, 105)])
    assert out.outcome == "none" and out.eval_r is None
    assert not out.filled and out.fill_index is None


def test_fill_on_gap_open_below_entry():
    # bar opens at 99.5 (<= entry 100) -> fill at the open, not at entry
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 99.5, 99.9, 99.4, 99.8),       # gap fill at 99.5
                 _c(2, 99.8, 102.1, 99.7, 102.0)])    # hits tp1 102
    # A16.2: R is normalized by the FILL risk (fill-sl), not the planned
    # (entry-sl). fill 99.5, sl 99 -> fill_risk 0.5; (102-99.5)/0.5 = 5.0
    assert out.outcome == "tp1" and out.eval_r == 5.0
    assert out.fill_index == 1


def test_creation_bar_is_not_an_entry_bar():
    # candle 0 touches entry, but the fill can only be on a later bar
    out = _eval([_c(0, 100.0, 100.5, 99.5, 100.0),    # touches entry - ignored
                 _c(1, 101, 101.5, 100.5, 101)],       # above entry, no fill
                entry_window=5)
    assert not out.filled and out.outcome == "none"


def test_horizon_mark_to_market():
    # fill, then meander without touching sl/tp1 until the horizon
    candles = [_c(0, 101, 101.2, 100.6, 101),
               _c(1, 100.5, 100.6, 99.8, 100.2)]       # fill 100
    for i in range(2, 8):                              # 6 flat bars ~100.5
        candles.append(_c(i, 100.5, 100.9, 100.1, 100.5))
    out = _eval(candles, horizon=5)                    # 5 bars after fill
    assert out.outcome == "none" and out.filled
    # mtm at the horizon bar close (100.5) -> r = (100.5-100)/1 = 0.5
    assert out.eval_r == 0.5


def test_mae_mfe_tracked_over_hold():
    # dips to 99.5 (mae -0.5) then rallies to tp1 102 (mfe >= 2.0)
    out = _eval([_c(0, 101, 101.2, 100.6, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),    # fill 100
                 _c(2, 100.2, 100.3, 99.5, 100.0),    # dip 99.5
                 _c(3, 100.0, 102.4, 99.9, 102.2)])   # tp1
    assert out.outcome == "tp1"
    assert out.eval_mae == -0.5                        # (99.5-100)/1
    assert out.eval_mfe >= 2.0


# ---------------------------------------------------------- SHORT mirror


def test_short_clean_tp1():
    # entry 100, sl 101, tp1 98 -> falls to 98
    out = _eval([_c(0, 99, 99.5, 98.5, 99),
                 _c(1, 99.5, 100.4, 99.4, 100.0),     # fill 100
                 _c(2, 100.0, 100.1, 97.9, 98.0)],    # tp1 98
                direction="SHORT", sl=101.0, tp1=98.0)
    assert out.outcome == "tp1" and out.eval_r == 2.0  # (100-98)/(101-100)


def test_short_gap_through_sl():
    out = _eval([_c(0, 99, 99.5, 98.5, 99),
                 _c(1, 99.5, 100.4, 99.4, 100.0),     # fill 100
                 _c(2, 102.0, 103.0, 101.9, 102.5)],  # gap-up open 102
                direction="SHORT", sl=101.0, tp1=98.0)
    assert out.outcome == "sl" and out.eval_r == -2.0  # (100-102)/1


def test_short_same_candle_sl_first():
    out = _eval([_c(0, 99, 99.5, 98.5, 99),
                 _c(1, 99.5, 100.4, 99.4, 100.0),     # fill 100
                 _c(2, 100.0, 101.5, 97.5, 100.0)],   # spans sl 101 & tp1 98
                direction="SHORT", sl=101.0, tp1=98.0)
    assert out.outcome == "sl" and out.eval_r == -1.0


def test_short_mae_mfe_tracked_over_hold():
    # SHORT: an adverse rally to 100.5 (mae -0.5) then falls to tp1 98
    out = _eval([_c(0, 99, 99.5, 98.5, 99),
                 _c(1, 99.5, 100.4, 99.4, 100.0),     # fill 100
                 _c(2, 100.0, 100.5, 99.8, 100.0),    # adverse high 100.5
                 _c(3, 100.0, 100.1, 97.9, 98.0)],    # tp1 98
                direction="SHORT", sl=101.0, tp1=98.0)
    assert out.outcome == "tp1" and out.eval_r == 2.0
    assert out.eval_mae == -0.5                        # (100-100.5)/(101-100)
    assert out.eval_mfe >= 2.0


def test_short_horizon_mark_to_market():
    candles = [_c(0, 99, 99.4, 98.6, 99),
               _c(1, 99.5, 100.4, 99.4, 100.0)]        # fill 100
    for i in range(2, 8):                              # flat ~99.5
        candles.append(_c(i, 99.5, 99.9, 99.1, 99.5))
    out = _eval(candles, direction="SHORT", sl=101.0, tp1=98.0, horizon=5)
    assert out.outcome == "none" and out.filled
    assert out.eval_r == 0.5                            # (100-99.5)/(101-100)


def test_intrabar_reaching_tp2_without_gap_resolves_tp1():
    # A16.2: TP1 is the single-outcome exit; a bar that rallies past tp2
    # (105) WITHOUT gapping open still resolves at tp1 (touched first on
    # the way up). tp2 reachability is the MFE, not the outcome.
    out = _eval([_c(0, 101, 101.2, 100.6, 101),
                 _c(1, 100.5, 100.6, 99.8, 100.2),    # fill 100
                 _c(2, 100.2, 105.5, 100.1, 105.0)],  # wick to 105, no gap
                tp2=104.0)
    assert out.outcome == "tp1" and out.eval_r == 2.0  # (102-100)/1
    assert out.eval_mfe >= 5.0                          # tp2+ reachable


# --------------------------------------------------------------- degenerate


def test_degenerate_geometry_not_evaluable():
    out = _eval([_c(0, 100, 100, 100, 100)], entry=100.0, sl=100.0)
    assert out.outcome == "none" and out.eval_r is None


def test_gap_fill_through_the_stop_is_a_catastrophic_stop():
    # LONG entry 100, sl 99; a bar opens at 98 (below BOTH entry and stop)
    # -> fill 98 is already past the stop -> immediate 'sl', loss measured
    # against the planned risk (entry-sl=1): (98-100)/1 = -2.0
    out = _eval([_c(0, 101, 101.5, 100.5, 101),
                 _c(1, 98.0, 98.5, 97.0, 97.5)])       # gap through both
    assert out.outcome == "sl" and out.eval_r == -2.0
    assert out.eval_mae == -2.0 and out.eval_mfe == 0.0
    assert out.filled and out.exit_index == out.fill_index


def test_immutable_and_deterministic():
    candles = [_c(0, 101, 101.5, 100.5, 101),
               _c(1, 100.5, 100.6, 99.8, 100.2),
               _c(2, 100.2, 102.3, 100.1, 102.1)]
    a = _eval(candles)
    b = _eval(list(candles))
    assert a == b and isinstance(a, Outcome)

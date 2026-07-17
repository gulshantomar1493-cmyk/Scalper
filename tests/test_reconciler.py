"""Tests for kline reconciliation (roadmap P0.14; D5 comparison rules)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from marketscalper.core.reconciler import KlineReconciler, compare_candles
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(**overrides) -> Candle:
    base = dict(
        symbol="BTCUSDT", tf="1m", ts=M0,
        o=67200.0, h=67230.0, l=67195.5, c=67215.1,
        v=12.5, qv=838125.0, n_trades=420, taker_buy_v=7.1,
    )
    base.update(overrides)
    return Candle(**base)


# ------------------------------------------------------------- pure comparison


def test_identical_candles_match():
    assert compare_candles(_candle(), _candle()) == []


@pytest.mark.parametrize("field", ["o", "h", "l", "c"])
def test_ohlc_mismatch_is_exact_no_tolerance(field):
    reference = _candle(**{field: getattr(_candle(), field) + 0.01})
    problems = compare_candles(_candle(), reference)
    assert len(problems) == 1 and problems[0].startswith(f"{field}:")


def test_volume_fields_within_tenth_percent_match():
    built = _candle()
    reference = _candle(
        v=12.5 * 1.001,              # exactly 0.1% apart -> inside tolerance
        qv=838125.0 * 0.999,
        n_trades=420,                # 420 vs 420
        taker_buy_v=7.1,
    )
    assert compare_candles(built, reference) == []


def test_volume_fields_beyond_tenth_percent_mismatch():
    reference = _candle(v=12.5 * 1.0011)  # 0.11% apart
    problems = compare_candles(_candle(), reference)
    assert len(problems) == 1 and problems[0].startswith("v:")


def test_n_trades_relative_tolerance():
    assert compare_candles(_candle(n_trades=1000), _candle(n_trades=1001)) == []      # 0.1%
    problems = compare_candles(_candle(n_trades=1000), _candle(n_trades=1002))        # 0.2%
    assert len(problems) == 1 and problems[0].startswith("n_trades:")


def test_zero_volume_on_both_sides_is_equal():
    assert compare_candles(_candle(taker_buy_v=0.0), _candle(taker_buy_v=0.0)) == []


def test_comparison_is_deterministic():
    a, b = _candle(), _candle(v=13.5, o=67201.0)
    assert compare_candles(a, b) == compare_candles(a, b)


# --------------------------------------------------------------- the reconciler


def test_matching_pair_counts_and_stays_silent(caplog):
    r = KlineReconciler()
    with caplog.at_level("WARNING"):
        r.on_built(_candle())
        r.on_reference(_candle())
    assert (r.pairs_compared, r.mismatches) == (1, 0)
    assert caplog.records == []


def test_mismatch_logged_with_both_rows_labeled_by_intake(caplog):
    r = KlineReconciler()
    with caplog.at_level("WARNING"):
        r.on_built(_candle())
        r.on_reference(_candle(o=67201.0))
    assert (r.pairs_compared, r.mismatches) == (1, 1)
    msg = caplog.records[0].getMessage()
    assert "MISMATCH" in msg and "built=" in msg and "reference=" in msg
    assert "o: built=67200.0 reference=67201.0" in msg


def test_reference_first_arrival_gives_identical_result():
    """No arrival-order semantics: labels come from the intake, not timing."""
    a, b = KlineReconciler(), KlineReconciler()
    built, reference = _candle(), _candle(c=67300.0)
    a.on_built(built); a.on_reference(reference)
    b.on_reference(reference); b.on_built(built)
    assert (a.pairs_compared, a.mismatches) == (b.pairs_compared, b.mismatches) == (1, 1)


def test_unpaired_candles_remain_pending_forever_no_expiry():
    r = KlineReconciler()
    for i in range(3):
        r.on_built(_candle(ts=M0 + timedelta(minutes=i)))
    assert r.pairs_compared == 0
    # counterpart arrives 90 minutes later in candle-time — still pairs
    r.on_reference(_candle(ts=M0))
    assert r.pairs_compared == 1 and r.mismatches == 0


def test_duplicate_same_side_keeps_first_and_warns(caplog):
    r = KlineReconciler()
    r.on_built(_candle(v=12.5))
    with caplog.at_level("WARNING"):
        r.on_built(_candle(v=99.0))  # duplicate built for same key -> ignored
    assert any("duplicate built" in rec.getMessage() for rec in caplog.records)
    r.on_reference(_candle(v=12.5))  # compares against the FIRST built row
    assert (r.pairs_compared, r.mismatches) == (1, 0)


def test_pairs_are_keyed_by_symbol_and_ts():
    r = KlineReconciler()
    r.on_built(_candle(symbol="BTCUSDT"))
    r.on_reference(_candle(symbol="ETHUSDT"))          # different symbol: no pair
    r.on_built(_candle(ts=M0 + timedelta(minutes=1)))  # different minute: no pair
    assert r.pairs_compared == 0
    r.on_reference(_candle(symbol="BTCUSDT"))
    assert r.pairs_compared == 1


def test_non_1m_candle_is_rejected():
    r = KlineReconciler()
    with pytest.raises(ValueError):
        r.on_built(_candle(tf="5m"))


def test_reconciler_never_modifies_candles():
    r = KlineReconciler()
    built, reference = _candle(), _candle(o=1.0)
    r.on_built(built)
    r.on_reference(reference)
    assert built == _candle() and reference == _candle(o=1.0)  # untouched
    with pytest.raises(dataclasses.FrozenInstanceError):
        built.o = 2.0


def test_counters_accumulate_across_pairs():
    r = KlineReconciler()
    for i in range(4):
        r.on_built(_candle(ts=M0 + timedelta(minutes=i)))
        bad = {"o": 1.0} if i % 2 else {}
        r.on_reference(_candle(ts=M0 + timedelta(minutes=i), **bad))
    assert (r.pairs_compared, r.mismatches) == (4, 2)

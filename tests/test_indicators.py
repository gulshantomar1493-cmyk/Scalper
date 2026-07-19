"""Unit tests for the display-only indicators (chart UX item 2).

Pure math — no DB, no network. Verifies EMA/SMA/RSI conventions and that the
incremental state (used for the live forming stream) matches the batch series.
"""

from __future__ import annotations

from marketscalper.core import indicators as ind


def test_sma_window():
    out = ind.sma([1, 2, 3, 4, 5, 6], 3)
    assert out[:2] == [None, None]
    assert out[2:] == [2.0, 3.0, 4.0, 5.0]


def test_ema_seed_is_sma_then_convention():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ind.ema(closes, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0                                 # SMA seed of [1,2,3]
    k = 2 / 4
    assert abs(out[3] - (4 * k + 2 * (1 - k))) < 1e-9    # 3.0
    assert abs(out[4] - (5 * k + out[3] * (1 - k))) < 1e-9


def test_ema_too_short_is_all_none():
    assert ind.ema([1.0, 2.0], 5) == [None, None]


def test_rsi_all_gains_is_100():
    out = ind.rsi([float(x) for x in range(1, 20)], 14)
    assert out[13] is None and out[14] == 100.0


def test_rsi_classic_series_in_range():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41]
    out = ind.rsi(closes, 14)
    assert out[14] is not None and 55.0 < out[14] < 85.0   # strong-uptrend RSI
    assert all(0.0 <= v <= 100.0 for v in out if v is not None)


def test_incremental_ema_matches_batch_series():
    closes = [float(x) for x in range(1, 40)]
    series = ind.ema(closes, 10)
    st = ind.EmaState(10)
    st.seed(closes[:25])
    for c in closes[25:]:
        st.update(c)
    assert abs(st.value - series[-1]) < 1e-6


def test_incremental_ema_peek_does_not_mutate():
    closes = [float(x) for x in range(1, 30)]
    st = ind.EmaState(10)
    st.seed(closes)
    before = st.value
    peeked = st.peek(closes[-1] + 5.0)
    assert st.value == before and peeked != before


def test_incremental_rsi_peek_matches_batch():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    series = ind.rsi(closes, 14)                          # series[14], series[15]
    st = ind.RsiState(14)
    st.seed(closes[:15])                                  # state at index 14
    assert abs(st.peek(closes[15]) - series[15]) < 1e-6
    assert abs(st.value - series[14]) < 1e-6

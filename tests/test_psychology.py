"""Tests for the psychology guards (§8; Decision D23; roadmap P4.9).

The pure PsychologyGuard: revenge (<5 min after a logged loss, same
symbol), overtrade warn (>6 taken/day), hard lock (>8 taken/day), the
per-day boundary, per-symbol revenge, in-place re-log, and prune.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.engines.psychology import (
    OVERTRADE_LOCK,
    OVERTRADE_WARN,
    PsychologyGuard,
    G5State,
)

UTC = timezone.utc
T0 = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _take(guard, rid, minutes, symbol="BTCUSDT", result="win"):
    guard.record_taken(rid, T0 + timedelta(minutes=minutes), symbol, result)


def test_clean_slate_passes():
    g = PsychologyGuard()
    st = g.evaluate(T0, "BTCUSDT")
    assert isinstance(st, G5State)
    assert st.passed and not st.locked and not st.warn
    assert "0 taken today" in st.detail


def test_revenge_within_window_fails_same_symbol():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "loss")
    # a new signal 3 min after the logged loss on the same symbol -> fail
    st = g.evaluate(T0 + timedelta(minutes=3), "BTCUSDT")
    assert not st.passed and "revenge" in st.detail


def test_revenge_only_same_symbol():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "loss")
    # a signal on ETH is not blocked by a BTC loss
    st = g.evaluate(T0 + timedelta(minutes=3), "ETHUSDT")
    assert st.passed


def test_revenge_only_losses():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "win")          # a WIN doesn't trigger revenge
    assert g.evaluate(T0 + timedelta(minutes=3), "BTCUSDT").passed


def test_revenge_window_boundary_exclusive():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "loss")
    # exactly 5 min later is OUTSIDE the window (strict <)
    assert g.evaluate(T0 + timedelta(minutes=5), "BTCUSDT").passed
    # 4:59 is inside
    assert not g.evaluate(
        T0 + timedelta(minutes=4, seconds=59), "BTCUSDT").passed


def test_overtrade_warn_above_six():
    g = PsychologyGuard()
    for i in range(OVERTRADE_WARN):           # exactly 6 -> still no warn
        _take(g, i, i, "BTCUSDT", "win")
    st = g.evaluate(T0 + timedelta(minutes=30), "BTCUSDT")
    assert st.passed and not st.warn          # 6 is not > 6
    _take(g, 99, 7, "BTCUSDT", "win")         # the 7th -> warn
    st = g.evaluate(T0 + timedelta(minutes=30), "BTCUSDT")
    assert st.passed and st.warn and "overtrade warn" in st.detail


def test_hard_lock_above_eight_fails():
    g = PsychologyGuard()
    for i in range(OVERTRADE_LOCK):           # 8 taken -> still passes (warn)
        _take(g, i, i, "BTCUSDT", "win")
    assert g.evaluate(T0 + timedelta(minutes=30), "BTCUSDT").passed
    _take(g, 99, 9, "BTCUSDT", "win")         # the 9th -> hard lock
    st = g.evaluate(T0 + timedelta(minutes=30), "BTCUSDT")
    assert not st.passed and st.locked and "hard lock" in st.detail


def test_lock_takes_precedence_over_warn_but_revenge_wins():
    g = PsychologyGuard()
    for i in range(9):
        _take(g, i, i, "BTCUSDT", "win")      # locked
    _take(g, 50, 9, "BTCUSDT", "loss")        # + a recent loss (10 total)
    st = g.evaluate(T0 + timedelta(minutes=11), "BTCUSDT")
    # revenge is checked first -> its detail wins, still a fail
    assert not st.passed and "revenge" in st.detail


def test_count_is_per_utc_day():
    g = PsychologyGuard()
    for i in range(9):                        # 9 taken "today"
        _take(g, i, i, "BTCUSDT", "win")
    # a signal the NEXT day: the prior day's trades don't count
    next_day = T0 + timedelta(days=1)
    assert g.evaluate(next_day, "BTCUSDT").passed


def test_relog_updates_in_place_no_double_count():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "win")
    _take(g, 1, 0, "BTCUSDT", "loss")         # SAME rid re-logged
    # still one record (keyed by rid), now a loss -> revenge applies
    st = g.evaluate(T0 + timedelta(minutes=2), "BTCUSDT")
    assert not st.passed and "revenge" in st.detail


def test_forget_drops_the_record():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "loss")
    g.forget(1)
    assert g.evaluate(T0 + timedelta(minutes=2), "BTCUSDT").passed


def test_prune_drops_stale_records():
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "win")
    later = T0 + timedelta(days=2)
    g.prune(later)
    assert g.evaluate(later, "BTCUSDT").detail.startswith("0 taken")


def test_record_taken_self_prunes_prior_day():
    # a new log auto-prunes prior-day records -> the map stays bounded.
    # Asserted on the INTERNAL map (evaluate() date-filters its output, so
    # the count alone can't distinguish pruned from merely-filtered).
    g = PsychologyGuard()
    _take(g, 1, 0, "BTCUSDT", "loss")             # today (T0)
    assert len(g._taken) == 1
    g.record_taken(2, T0 + timedelta(days=1), "BTCUSDT", "win")  # next day
    assert len(g._taken) == 1                     # rid=1 physically dropped
    assert 1 not in g._taken and 2 in g._taken

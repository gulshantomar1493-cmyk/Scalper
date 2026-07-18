"""Tests for the recommendation lifecycle engine (§7; D22/A16.1; P4.4).

State-machine transitions active -> {invalidated | expired | evaluated}:
the INVALID rule (entry unfilled), opposite-signal and G1-fail
invalidation, horizon expiry, evaluated terminal, independent advance of
multiple active recs, and terminal removal. Small entry_window/horizon
keep the vectors short.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.engines.lifecycle import (
    LifecycleEvent,
    RecommendationLifecycle,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
T0 = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _c(i, o, h, l, c):
    return Candle(symbol="BTCUSDT", tf="1m", ts=T0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _rec(direction="LONG", entry=100.0, sl=99.0, tp1=102.0, tp2=None,
         strategy="S1", minute=0):
    return {"strategy": strategy, "direction": direction, "entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "created_ts": (T0 + timedelta(minutes=minute)).isoformat()}


def _rig(entry_window=2, horizon=5):
    return RecommendationLifecycle("BTCUSDT", entry_window=entry_window,
                                   horizon=horizon)


# ------------------------------------------------------------ evaluated


def test_active_to_evaluated_tp1():
    lc = _rig()
    lc.on_recommendation(_rec(), _c(0, 101, 101.2, 100.6, 101))
    assert lc.update(_c(1, 100.5, 100.6, 99.8, 100.2)) == []   # fill 100
    events = lc.update(_c(2, 100.2, 102.3, 100.1, 102.1))      # tp1
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, LifecycleEvent)
    assert ev.status == "evaluated" and ev.outcome.outcome == "tp1"
    assert ev.outcome.eval_r == 2.0 and ev.ts == _c(2, 0, 0, 0, 0).ts
    assert lc.active_count == 0                                # removed


def test_active_to_evaluated_sl():
    lc = _rig()
    lc.on_recommendation(_rec(), _c(0, 101, 101.2, 100.6, 101))
    lc.update(_c(1, 100.5, 100.6, 99.8, 100.2))               # fill
    [ev] = lc.update(_c(2, 100.2, 100.3, 98.9, 99.0))         # sl
    assert ev.status == "evaluated" and ev.outcome.outcome == "sl"
    assert ev.outcome.eval_r == -1.0


# ---------------------------------------------------------- invalidated


def test_active_to_invalidated_entry_unfilled():
    # entry 100 never touched within entry_window=2 -> invalidated
    lc = _rig(entry_window=2)
    lc.on_recommendation(_rec(), _c(0, 105, 105.2, 104.6, 105))
    assert lc.update(_c(1, 105, 105.2, 104.6, 105)) == []      # bar 1: waiting
    [ev] = lc.update(_c(2, 105, 105.2, 104.6, 105))            # bar 2: window up
    assert ev.status == "invalidated"
    assert "unfilled" in ev.reason and ev.outcome is None
    assert lc.active_count == 0


def test_active_to_invalidated_opposite_signal():
    lc = _rig()
    lc.on_recommendation(_rec(direction="LONG", strategy="S1"),
                         _c(0, 101, 101.2, 100.6, 101))
    # an opposite-direction signal of the SAME family invalidates
    [ev] = lc.update(_c(1, 100.5, 100.6, 99.8, 100.2),
                     opposite_signals={("S1", "SHORT")})
    assert ev.status == "invalidated"
    assert ev.reason == "opposite-direction signal"


def test_opposite_signal_wrong_family_or_direction_does_not_invalidate():
    lc = _rig()
    lc.on_recommendation(_rec(direction="LONG", strategy="S1"),
                         _c(0, 101, 101.2, 100.6, 101))
    # same direction, or a different family, must NOT invalidate
    assert lc.update(_c(1, 100.9, 101.0, 100.6, 100.8),
                     opposite_signals={("S1", "LONG"), ("S2", "SHORT")}) == []
    assert lc.active_count == 1


def test_active_to_invalidated_g1_fail():
    lc = _rig()
    lc.on_recommendation(_rec(), _c(0, 101, 101.2, 100.6, 101))
    [ev] = lc.update(_c(1, 100.5, 100.6, 99.8, 100.2), g1_ok=False)
    assert ev.status == "invalidated"
    assert ev.reason == "G1 data-integrity fail"


# --------------------------------------------------------------- expired


def test_active_to_expired_horizon():
    lc = _rig(entry_window=2, horizon=3)
    lc.on_recommendation(_rec(), _c(0, 101, 101.2, 100.6, 101))
    lc.update(_c(1, 100.5, 100.6, 99.8, 100.2))               # fill at bar 1
    # 3 flat bars after the fill, no sl/tp1 touch -> expire at the horizon
    assert lc.update(_c(2, 100.5, 100.9, 100.1, 100.5)) == []
    assert lc.update(_c(3, 100.5, 100.9, 100.1, 100.5)) == []
    [ev] = lc.update(_c(4, 100.5, 100.9, 100.1, 100.5))       # bars_since_fill=3
    assert ev.status == "expired" and ev.outcome.outcome == "none"
    assert ev.outcome.eval_r == 0.5                            # mtm (100.5-100)/1


def test_expiry_before_entry_is_invalidation_not_expiry():
    # unfilled at the entry window -> invalidated (not expired); the
    # horizon governs FILLED positions only
    lc = _rig(entry_window=2, horizon=10)
    lc.on_recommendation(_rec(), _c(0, 105, 105.2, 104.6, 105))
    lc.update(_c(1, 105, 105.2, 104.6, 105))
    [ev] = lc.update(_c(2, 105, 105.2, 104.6, 105))
    assert ev.status == "invalidated"


# ---------------------------------------------------------- multiplicity


def test_multiple_recs_advance_independently():
    lc = _rig(entry_window=3, horizon=10)
    lc.on_recommendation(_rec(strategy="S1", minute=0),
                         _c(0, 101, 101.2, 100.6, 101))
    lc.on_recommendation(_rec(strategy="S3", entry=100.0, sl=99.0,
                              tp1=101.0, minute=0),
                         _c(0, 101, 101.2, 100.6, 101))
    # bar 1: both fill at 100; S3 (tp1 101) resolves, S1 (tp1 102) waits
    events = lc.update(_c(1, 100.5, 101.2, 99.8, 101.1))
    assert len(events) == 1
    assert events[0].outcome.outcome == "tp1"
    assert lc.active_count == 1                                # S1 still live
    # bar 2: S1 hits tp1 102
    [ev] = lc.update(_c(2, 101.1, 102.3, 101.0, 102.2))
    assert ev.status == "evaluated" and lc.active_count == 0


def test_terminal_rec_never_re_emits():
    lc = _rig()
    lc.on_recommendation(_rec(), _c(0, 101, 101.2, 100.6, 101))
    lc.update(_c(1, 100.5, 100.6, 99.8, 100.2))
    assert lc.update(_c(2, 100.2, 102.3, 100.1, 102.1))       # evaluated
    assert lc.update(_c(3, 102, 103, 101, 102.5)) == []       # nothing more
    assert lc.active_count == 0

"""Tests for the Order Block Engine (§4.5; Decision D13; roadmap P2.14/15/17)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketscalper import db
from marketscalper.engines.orderblock import (
    OB_TRACKED_PER_BUCKET,
    OrderBlock,
    OrderBlockEngine,
    ob_to_row,
)
from marketscalper.engines.structure import BosEvent, Pivot
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(i, o, c, h=None, l=None):
    h = max(o, c) + 1 if h is None else h
    l = min(o, c) - 1 if l is None else l
    return Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _bos(i, direction="UP", displacement=True):
    ts = M0 + timedelta(minutes=i)
    pivot = Pivot("BTCUSDT", "1m", ts, ts, "H" if direction == "UP" else "L",
                  100.0)
    return BosEvent("BTCUSDT", "1m", ts, direction, pivot, 100.0, displacement)


def _bull_setup(engine):
    """Bearish candle at bar 1 (o=102,c=99,l=98) then impulse; displacement
    BOS UP at bar 3 -> bullish OB zone [98, 102]."""
    engine.update(_candle(0, 100, 101))            # bullish (skipped in scan)
    engine.update(_candle(1, 102, 99, l=98))       # the OB source (bearish)
    engine.update(_candle(2, 99, 104))             # impulse
    engine.on_bos(_bos(3))
    [ob] = engine.update(_candle(3, 104, 108))     # BOS bar
    return ob


def test_bullish_ob_detected_from_last_bearish_candle():
    engine = OrderBlockEngine("BTCUSDT")
    ob = _bull_setup(engine)
    assert (ob.direction, ob.breaker, ob.status) == ("BULL", False, "active")
    assert (ob.zone_lo, ob.zone_hi) == (98.0, 102.0)     # [low, open]
    assert ob.source_ts == M0 + timedelta(minutes=1)
    assert engine.blocks == [ob] and engine.breakers == []


def test_bearish_ob_mirrored_zone_open_high():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 100, 99))             # bearish (skipped)
    engine.update(_candle(1, 98, 101, h=102))      # bullish source
    engine.update(_candle(2, 101, 96))             # impulse down
    engine.on_bos(_bos(3, "DOWN"))
    [ob] = engine.update(_candle(3, 96, 92))
    assert ob.direction == "BEAR"
    assert (ob.zone_lo, ob.zone_hi) == (98.0, 102.0)     # [open, high]


def test_no_ob_without_displacement_or_opposite_candle():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 102, 99))
    engine.on_bos(_bos(1, displacement=False))     # weak BOS
    assert engine.update(_candle(1, 99, 104)) == []
    engine.on_bos(_bos(2, displacement=None))      # unclassifiable
    assert engine.update(_candle(2, 104, 105)) == []
    # monotonic bullish history: no bearish source within the bound
    engine2 = OrderBlockEngine("BTCUSDT")
    for i in range(5):
        engine2.update(_candle(i, 100 + i, 101 + i))
    engine2.on_bos(_bos(5))
    assert engine2.update(_candle(5, 105, 106)) == []


def test_doji_is_skipped_in_the_scan():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 102, 99, l=98))       # true source (bearish)
    engine.update(_candle(1, 100, 100))            # doji: no color
    engine.update(_candle(2, 100, 104))            # impulse
    engine.on_bos(_bos(3))
    [ob] = engine.update(_candle(3, 104, 108))
    assert ob.source_ts == M0                      # skipped the doji


def test_duplicate_identity_ignored():
    engine = OrderBlockEngine("BTCUSDT")
    _bull_setup(engine)
    engine.on_bos(_bos(4))                         # same source would match
    assert engine.update(_candle(4, 108, 109)) == []
    assert len(engine.blocks) == 1


def test_mitigation_on_first_overlap_not_on_creation_bar():
    engine = OrderBlockEngine("BTCUSDT")
    ob = _bull_setup(engine)                       # zone [98, 102]
    engine.update(_candle(4, 108, 107, l=106))     # far above: no touch
    assert ob.status == "active"
    engine.update(_candle(5, 107, 103, l=101.5))   # low dips into the zone
    assert ob.status == "mitigated"
    engine.update(_candle(6, 103, 104, l=100))     # sticky
    assert ob.status == "mitigated"


def test_break_is_strict_and_precedes_mitigation():
    engine = OrderBlockEngine("BTCUSDT")
    ob = _bull_setup(engine)                       # zone [98, 102]
    engine.update(_candle(4, 103, 98, l=97))       # close == lo: NOT broken
    assert ob.status == "mitigated"                # (it did overlap though)
    [breaker] = engine.update(_candle(5, 98, 97.5, l=96))  # close < 98
    assert ob.status == "broken"
    assert (breaker.direction, breaker.breaker) == ("BEAR", True)
    assert (breaker.zone_lo, breaker.zone_hi) == (98.0, 102.0)
    assert breaker.status == "active"              # break bar overlaps the
    assert engine.blocks == [] and engine.breakers == [breaker]  # zone, but
    # D13.3: the breaker's lifecycle starts the NEXT bar — not mitigated yet


def test_breaker_lifecycle_and_single_flip():
    engine = OrderBlockEngine("BTCUSDT")
    ob = _bull_setup(engine)
    engine.update(_candle(4, 97.5, 97, l=96))      # break the OB
    [breaker] = engine.breakers
    engine.update(_candle(5, 97, 99, h=100))       # retest into [98, 102]
    assert breaker.status == "mitigated"
    created = engine.update(_candle(6, 99, 103, h=104))  # close > 102: broken
    assert breaker.status == "broken"
    assert created == []                           # one flip only (D13.3)
    assert engine.breakers == []


def test_stale_pending_bos_is_discarded():
    """Freeze-audit fix: a latch surviving a mid-cadence exception belongs
    to an earlier bar and must never be consumed late."""
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 100, 101))
    engine.update(_candle(1, 102, 99, l=98))
    engine.update(_candle(2, 99, 104))
    engine.on_bos(_bos(3))                         # bar-3 event...
    assert engine.update(_candle(4, 104, 108)) == []   # ...bar-4 candle
    assert engine.blocks == []


def test_zone_not_evaluated_on_its_creation_bar():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 100, 101))
    engine.update(_candle(1, 102, 99, l=98))
    engine.update(_candle(2, 99, 104))
    engine.on_bos(_bos(3))
    [ob] = engine.update(_candle(3, 104, 108, l=100))  # dips into [98,102]
    assert ob.status == "active"                   # D13.2 creation-bar rule
    engine.update(_candle(4, 108, 109, l=101))     # next bar: overlap counts
    assert ob.status == "mitigated"


def test_lookback_bound_is_exactly_twenty_bars():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 100, 101))            # bullish filler
    engine.update(_candle(1, 102, 99, l=98))       # source, 20 bars back
    for i in range(2, 21):
        engine.update(_candle(i, 100 + i, 101 + i))
    engine.on_bos(_bos(21))
    [ob] = engine.update(_candle(21, 121, 122))
    assert ob.source_ts == M0 + timedelta(minutes=1)
    # 21 bars back: outside the D13.1 bound
    engine2 = OrderBlockEngine("BTCUSDT")
    engine2.update(_candle(0, 102, 99, l=98))      # source
    for i in range(1, 21):
        engine2.update(_candle(i, 100 + i, 101 + i))
    engine2.on_bos(_bos(21))
    assert engine2.update(_candle(21, 121, 122)) == []


def test_bearish_boundary_equality_not_a_break():
    engine = OrderBlockEngine("BTCUSDT")
    engine.update(_candle(0, 100, 99))
    engine.update(_candle(1, 98, 101, h=102))      # bull source: zone [98,102]
    engine.update(_candle(2, 101, 96))
    engine.on_bos(_bos(3, "DOWN"))
    [ob] = engine.update(_candle(3, 96, 92))
    engine.update(_candle(4, 95, 102, h=103))      # close == zone_hi
    assert ob.status == "mitigated"                # touched, never broken
    engine.update(_candle(5, 102, 103.5, h=104))   # close > zone_hi: broken
    assert ob.status == "broken"


def test_tracking_bound_per_bucket():
    engine = OrderBlockEngine("BTCUSDT")
    bar = 0
    for n in range(OB_TRACKED_PER_BUCKET + 1):     # 11 bullish OBs
        engine.update(_candle(bar, 102 + n, 99 + n))       # bearish source
        engine.update(_candle(bar + 1, 99 + n, 104 + n))   # impulse
        engine.on_bos(_bos(bar + 2))
        engine.update(_candle(bar + 2, 104 + n, 108 + n))
        bar += 3
    assert len(engine.blocks) == OB_TRACKED_PER_BUCKET     # oldest dropped


def test_determinism_same_feed_twice():
    def run():
        engine = OrderBlockEngine("BTCUSDT")
        out = [_bull_setup(engine)]
        out.append(engine.update(_candle(4, 103, 98, l=97)))
        out.append(engine.update(_candle(5, 98, 97.5, l=96)))
        out.append(engine.update(_candle(6, 97, 99, h=100)))
        return out, engine.blocks, engine.breakers
    assert run() == run()


async def test_persistence_capability_ob_rows_only(db_conn):
    engine = OrderBlockEngine("BTCUSDT")
    ob = _bull_setup(engine)
    level_id = await db.insert_level(db_conn, **ob_to_row(ob, "BTCUSDT"))
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert rows[0]["kind"] == "OB_BULL"
    assert float(rows[0]["p1"]) == 102.0 and float(rows[0]["p2"]) == 98.0
    await db.update_level_lifecycle(db_conn, level_id, touches=0,
                                    status="mitigated", status_ts=M0)
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert rows[0]["status"] == "mitigated"
    engine.update(_candle(4, 97.5, 97, l=96))      # spawn the breaker
    [breaker] = engine.breakers
    with pytest.raises(ValueError):
        ob_to_row(breaker, "BTCUSDT")              # state-only (D13.4)

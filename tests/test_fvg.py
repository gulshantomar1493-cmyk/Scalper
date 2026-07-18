"""Tests for the FVG Engine (§4.5; Decision D14; roadmap P2.16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper import db
from marketscalper.engines.fvg import (
    FVG_TRACKED_PER_DIRECTION,
    FvgEngine,
    fvg_to_row,
)
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(i, o, h, l, c):
    return Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _rig(atr_period=1):
    atr = IncrementalATR(period=atr_period)
    return atr, FvgEngine("BTCUSDT", atr)


def _step(atr, fvg, candle):
    atr.update(candle)
    return fvg.update(candle)


def _bull_gap(atr, fvg, start=0):
    """c1 high 102, c3 low 106 -> bullish gap [102, 106], CE 104.
    ATR(1) at the detection bar: TR = 3.0, threshold 0.9 < gap size 4."""
    _step(atr, fvg, _candle(start, 100, 102, 99, 101))
    _step(atr, fvg, _candle(start + 1, 101, 107, 101, 106.5))
    [gap] = _step(atr, fvg, _candle(start + 2, 106.5, 109, 106, 108))
    return gap


def test_bullish_fvg_detected_with_zone_and_ce():
    atr, fvg = _rig()
    gap = _bull_gap(atr, fvg)
    assert (gap.direction, gap.lo, gap.hi) == ("BULL", 102.0, 106.0)
    assert gap.ce == 104.0 and gap.status == "active"
    assert fvg.gaps == [gap]


def test_bearish_fvg_mirrored():
    atr, fvg = _rig()
    _step(atr, fvg, _candle(0, 108, 109, 106, 107))    # c1: low 106
    _step(atr, fvg, _candle(1, 107, 107, 101, 101.5))
    [gap] = _step(atr, fvg, _candle(2, 101.5, 102, 99, 100))  # c3: high 102
    assert (gap.direction, gap.lo, gap.hi) == ("BEAR", 102.0, 106.0)


def test_strict_gap_inequality_and_min_size_inclusive():
    # c1.h == c3.l -> no gap (outcome pinned by D14.1; at this ATR the
    # size filter would independently reject a zero-size gap too)
    atr, fvg = _rig()
    _step(atr, fvg, _candle(0, 100, 102, 99, 101))
    _step(atr, fvg, _candle(1, 101, 107, 101, 106))
    assert _step(atr, fvg, _candle(2, 106, 109, 102, 108)) == []
    # size exactly 0.3*ATR: detected (inclusive minimum, D14.1).
    # ATR(1) TR of c3 = max(h-l, |h-prev_c|, |l-prev_c|) engineered to 10:
    # gap [103, 106] size 3 == 0.3*10
    atr, fvg = _rig()
    _step(atr, fvg, _candle(0, 100, 103, 99, 101))     # c1: high 103
    _step(atr, fvg, _candle(1, 101, 107, 101, 106))
    [gap] = _step(atr, fvg, _candle(2, 106, 116, 106, 107))   # TR = 10
    assert atr.value == 10.0 and (gap.lo, gap.hi) == (103.0, 106.0)
    # a hair smaller gap with the same ATR: rejected
    atr, fvg = _rig()
    _step(atr, fvg, _candle(0, 100, 103.5, 99, 101))
    _step(atr, fvg, _candle(1, 101, 107, 101, 106))
    assert _step(atr, fvg, _candle(2, 106, 116, 106, 107)) == []


def test_no_detection_while_atr_unwarm():
    atr, fvg = _rig(atr_period=50)
    _step(atr, fvg, _candle(0, 100, 102, 99, 101))
    _step(atr, fvg, _candle(1, 101, 107, 101, 106.5))
    assert _step(atr, fvg, _candle(2, 106.5, 109, 106, 108)) == []
    assert atr.value is None and fvg.gaps == []


def test_creation_bar_not_evaluated_then_ce_inclusive():
    atr, fvg = _rig()
    gap = _bull_gap(atr, fvg)                          # [102,106], CE 104
    assert gap.status == "active"                      # c3 low 106 == hi edge
    _step(atr, fvg, _candle(3, 108, 109, 105, 108.5))  # above CE: no test
    assert gap.status == "active"
    _step(atr, fvg, _candle(4, 108, 108.5, 104, 105))  # low == CE exactly
    assert gap.status == "ce_tested"                   # inclusive (D14.2)
    _step(atr, fvg, _candle(5, 105, 106, 104.5, 105.5))
    assert gap.status == "ce_tested"                   # sticky


def test_full_fill_archives_including_direct_spear():
    atr, fvg = _rig()
    gap = _bull_gap(atr, fvg)
    _step(atr, fvg, _candle(3, 108, 108.5, 102, 103))  # low == lo: filled
    assert gap.status == "filled" and fvg.gaps == []
    # direct active -> filled in one candle
    atr, fvg = _rig()
    gap = _bull_gap(atr, fvg)
    _step(atr, fvg, _candle(3, 108, 108.5, 101, 102.5))
    assert gap.status == "filled" and fvg.gaps == []


def test_bearish_lifecycle_mirrored():
    atr, fvg = _rig()
    _step(atr, fvg, _candle(0, 108, 109, 106, 107))
    _step(atr, fvg, _candle(1, 107, 107, 101, 101.5))
    [gap] = _step(atr, fvg, _candle(2, 101.5, 102, 99, 100))  # [102,106]
    _step(atr, fvg, _candle(3, 100, 104, 99, 101))     # high == CE
    assert gap.status == "ce_tested"
    _step(atr, fvg, _candle(4, 101, 106, 100, 102))    # high == hi: filled
    assert gap.status == "filled" and fvg.gaps == []


def test_tracking_bound_per_direction():
    atr, fvg = _rig()
    made = 0
    i = 0
    while made < FVG_TRACKED_PER_DIRECTION + 1:
        base = 100 + made * 50                         # far apart: no fills
        _step(atr, fvg, _candle(i, base, base + 2, base - 1, base + 1))
        _step(atr, fvg, _candle(i + 1, base + 1, base + 7, base + 1, base + 6))
        created = _step(atr, fvg, _candle(i + 2, base + 6, base + 9,
                                          base + 6, base + 8))
        made += len(created)
        i += 3
    # 31 BULL gaps created in all (the +50 base jumps make bridging gaps
    # too); the trim rule keeps the newest 10 — nothing else bounds it
    assert len(fvg.gaps) == FVG_TRACKED_PER_DIRECTION
    assert all(g.direction == "BULL" for g in fvg.gaps)
    # per-direction independence (D14.3): a BEAR gap must coexist with a
    # full BULL bucket — a global 10-cap would evict instead of extend
    _step(atr, fvg, _candle(i, 2000, 2001, 1999, 2000.5))
    _step(atr, fvg, _candle(i + 1, 2000.5, 2000.5, 1994, 1995))
    created = _step(atr, fvg, _candle(i + 2, 1995, 1995.5, 1993, 1994))
    assert [g.direction for g in created] == ["BEAR"]  # [1995.5, 1999]
    assert len(fvg.gaps) == FVG_TRACKED_PER_DIRECTION + 1
    assert sum(g.direction == "BEAR" for g in fvg.gaps) == 1


def test_determinism_same_feed_twice():
    def run():
        atr, fvg = _rig()
        out = [_bull_gap(atr, fvg)]
        out.append(_step(atr, fvg, _candle(3, 108, 108.5, 104, 105)))
        out.append(_step(atr, fvg, _candle(4, 105, 106, 101, 102)))
        return out, fvg.gaps
    assert run() == run()


async def test_persistence_capability_round_trip(db_conn):
    atr, fvg = _rig()
    gap = _bull_gap(atr, fvg)
    level_id = await db.insert_level(db_conn, **fvg_to_row(gap, "BTCUSDT"))
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert rows[0]["kind"] == "FVG_BULL"
    assert float(rows[0]["p1"]) == 106.0 and float(rows[0]["p2"]) == 102.0
    await db.update_level_lifecycle(db_conn, level_id, touches=0,
                                    status="ce_tested", status_ts=M0)
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert rows[0]["status"] == "ce_tested"            # app-layer vocabulary

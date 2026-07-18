"""Tests for the Confluence Engine (§4.5 stacking; Decision D15; P2.18/19)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from marketscalper.engines.confluence import confluence_zones
from marketscalper.engines.fvg import FairValueGap
from marketscalper.engines.liquidity import LiquidityPool
from marketscalper.engines.orderblock import OrderBlock

UTC = timezone.utc
T0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _ob(direction, lo, hi, minute, status="active", breaker=False):
    ts = T0 + timedelta(minutes=minute)
    return OrderBlock(direction, lo, hi, ts, minute, ts,
                      breaker=breaker, status=status)


def _fvg(direction, lo, hi, minute):
    return FairValueGap(direction, lo, hi, minute,
                        T0 + timedelta(minutes=minute))


def _line(price):
    """Flat kept-line stand-in: the function reads intercept/slope/a_index
    only; price at any bar = exp(ln(price))."""
    return SimpleNamespace(intercept=math.log(price), slope=0.0, a_index=0)


def _pool(kind, price):
    return LiquidityPool(kind=kind, price=price, size=2, strength=1.0,
                        member_ts=(T0,))


# The hand-computed scenario (ATR 10 -> tol exactly 3.0):
#   anchors: OB1 BULL [100,105] t0 | BR1 BEAR breaker [95,97] t2
#            | FVG1 BULL [107,110] t1
#   evidence extras: line ~109 (inside FVG1), EQH pool 104,
#            levels LONDON_H 116.5 / PDH 113.0 / PWH 113.25
#   OB2 mitigated [98,99] must appear NOWHERE (anchor or evidence).
def _scenario():
    return dict(
        blocks=[_ob("BULL", 100.0, 105.0, 0),
                _ob("BULL", 98.0, 99.0, 5, status="mitigated")],
        breakers=[_ob("BEAR", 95.0, 97.0, 2, breaker=True)],
        gaps=[_fvg("BULL", 107.0, 110.0, 1)],
        lines=[_line(109.0)],
        pools=[_pool("EQH", 104.0)],
        key_levels={"LONDON_H": 116.5, "PDH": 113.0, "PWH": 113.25},
        atr=10.0,
        bar_index=50,
    )


def test_stack_counts_members_and_order():
    zones = confluence_zones(**_scenario())
    assert [(z.kind, z.count, z.htf_magnet) for z in zones] == [
        ("FVG", 5, True), ("OB", 4, True), ("BREAKER", 2, False)]
    fvg1, ob1, br1 = zones
    # FVG1 [107,110]: OB1 gap 2 | line inside | EQH gap 3 (inclusive)
    #                | PDH gap 3 (inclusive) | PWH gap 3.25 excluded
    assert fvg1.members == ("FVG", "OB", "TRENDLINE", "EQH", "PDH")
    # OB1 [100,105]: BR1 gap 3 (inclusive) | FVG1 gap 2 | EQH inside
    assert ob1.members == ("OB", "BREAKER", "FVG", "EQH")
    assert (ob1.lo, ob1.hi, ob1.direction) == (100.0, 105.0, "BULL")
    # BR1 [95,97]: only OB1 within band
    assert br1.members == ("BREAKER", "OB")


def test_inclusive_band_boundary():
    zones = confluence_zones(**_scenario())
    fvg1 = zones[0]
    assert "PDH" in fvg1.members       # gap 113.0-110.0 = 3.0 == tol
    assert "PWH" not in fvg1.members   # gap 3.25 > tol
    assert "LONDON_H" not in fvg1.members


def test_mitigated_ob_fully_excluded():
    zones = confluence_zones(**_scenario())
    assert all(z.kind != "OB" or z.lo == 100.0 for z in zones)
    # BR1 [95,97] vs mitigated OB2 [98,99]: gap 1 <= tol — if OB2 leaked
    # into evidence BR1 would count 3, not 2
    br1 = [z for z in zones if z.kind == "BREAKER"][0]
    assert br1.count == 2


def test_atr_unwarm_returns_empty():
    scenario = _scenario()
    scenario["atr"] = None
    assert confluence_zones(**scenario) == []


def test_created_ts_tiebreak_newer_first():
    zones = confluence_zones(
        blocks=[_ob("BULL", 100.0, 101.0, 0), _ob("BULL", 300.0, 301.0, 7)],
        breakers=[], gaps=[], lines=[], pools=[], key_levels={},
        atr=10.0, bar_index=10)
    assert [(z.count, z.lo) for z in zones] == [(1, 300.0), (1, 100.0)]


def test_trendline_priced_at_current_bar():
    # slope 0.01/bar from bar 0: at bar 10 price = exp(ln(100)+0.1) ~ 110.5
    line = SimpleNamespace(intercept=math.log(100.0), slope=0.01, a_index=0)
    anchor = _fvg("BULL", 108.0, 109.0, 1)
    zones = confluence_zones(
        blocks=[], breakers=[], gaps=[anchor], lines=[line],
        pools=[], key_levels={}, atr=10.0, bar_index=10)
    [zone] = zones
    assert zone.members == ("FVG", "TRENDLINE")    # gap ~1.52 <= 3
    zones = confluence_zones(
        blocks=[], breakers=[], gaps=[anchor], lines=[line],
        pools=[], key_levels={}, atr=10.0, bar_index=40)
    [zone] = zones                                 # bar 40: price ~149.2
    assert zone.members == ("FVG",)
    # anchor offset matters: a_index 5 at bar 10 -> exp(ln(100)+0.05)
    # ~105.13 (gap ~0.63); dropping "- a_index" would price ~110.52 (out)
    offset_line = SimpleNamespace(intercept=math.log(100.0), slope=0.01,
                                  a_index=5)
    anchor2 = _fvg("BULL", 103.0, 104.5, 1)
    zones = confluence_zones(
        blocks=[], breakers=[], gaps=[anchor2], lines=[offset_line],
        pools=[], key_levels={}, atr=10.0, bar_index=10)
    [zone] = zones
    assert zone.members == ("FVG", "TRENDLINE")


def test_htf_magnet_boundary_at_exactly_three():
    zones = confluence_zones(
        blocks=[_ob("BULL", 100.0, 105.0, 0)], breakers=[], gaps=[],
        lines=[], pools=[_pool("EQH", 104.0)], key_levels={"PDH": 106.0},
        atr=10.0, bar_index=10)
    [zone] = zones
    assert zone.count == 3 and zone.htf_magnet is True   # §4.5: "3+"


def test_determinism_same_inputs_twice():
    assert confluence_zones(**_scenario()) == confluence_zones(**_scenario())

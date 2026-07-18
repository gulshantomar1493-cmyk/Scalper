"""Tests for the Liquidity Engine (§4.4; Decision D12; roadmap P2.8–P2.13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketscalper import db
from marketscalper.engines.liquidity import (
    SWEEP_RVOL_PLACEHOLDER_PASSES,
    LiquidityEngine,
    LiquidityPool,
    SweepEvent,
    SweepShift,
    key_level_to_row,
    pool_to_row,
    session_of,
)
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import ChochEvent, Pivot
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)      # Tuesday, NY session


def _candle(i=0, o=100.0, h=None, l=None, c=None, ts=None):
    h = o + 1 if h is None else h
    l = o - 1 if l is None else l
    c = o if c is None else c
    return Candle(symbol="BTCUSDT", tf="1m",
                  ts=ts if ts is not None else M0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _pivot(kind, price, i=0, tf="1m"):
    ts = M0 + timedelta(minutes=i)
    return Pivot("BTCUSDT", tf, ts, ts, kind, float(price))


def _rig(atr_period=1):
    atr = IncrementalATR(period=atr_period)
    return atr, LiquidityEngine("BTCUSDT", atr)


def _step(atr, liq, candle):
    atr.update(candle)
    return liq.update(candle)


def _warm(atr, liq, i=0, o=100.0):
    """Two candles: warm the period-1 ATR to exactly 2.0 (range h-l)."""
    _step(atr, liq, _candle(i, o))
    _step(atr, liq, _candle(i + 1, o))
    assert atr.value == pytest.approx(2.0)


# ------------------------------------------------------------ session map


def test_session_map_covers_all_24_hours_per_d12():
    assert [session_of(h) for h in range(24)] == (
        ["ASIA"] * 8 + ["LONDON"] * 5 + ["NY"] * 8 + ["LATE"] * 3)


# ------------------------------------------------------------------ pools


def test_eqh_pool_clusters_within_strict_tolerance():
    atr, liq = _rig()
    _warm(atr, liq)                                # ATR 2.0 -> tol 0.2 strict
    liq.on_pivot(_pivot("H", 110.00, 2))
    liq.on_pivot(_pivot("H", 110.15, 3))           # within 0.2 of anchor
    liq.on_pivot(_pivot("H", 111.00, 4))           # far: not a member
    _step(atr, liq, _candle(2))
    [pool] = liq.pools
    assert pool.kind == "EQH" and pool.size == 2
    assert pool.price == pytest.approx((110.00 + 110.15) / 2)


def test_tolerance_boundary_is_strict():
    # exactly-representable floats so < and <= genuinely differ:
    # ATR = 2.5 -> tol = 0.25 exact; pivots exactly 0.25 apart
    atr, liq = _rig()
    _step(atr, liq, _candle(0, o=100, h=101.25, l=98.75))
    _step(atr, liq, _candle(1, o=100, h=101.25, l=98.75))
    assert atr.value == 2.5 and 0.1 * 2.5 == 0.25  # float preconditions
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.25, 3))           # exactly at tolerance
    _step(atr, liq, _candle(2))
    assert liq.pools == []                         # < is strict: no pool
    liq.on_pivot(_pivot("H", 110.2, 4))            # inside: pools again
    _step(atr, liq, _candle(3))
    assert any(p.kind == "EQH" for p in liq.pools)


def test_pool_strength_decay_hand_computed():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("L", 90.0, 2))
    liq.on_pivot(_pivot("L", 90.1, 3))
    _step(atr, liq, _candle(2))                    # recompute at bar 2
    [pool] = liq.pools
    assert pool.kind == "EQL"
    # newest member index = 2 (bar of confirmation), cur = 2 -> age 0
    assert pool.strength == pytest.approx(2.0)
    # a later pivot triggers recompute at bar 3: age 1 -> 2/(1+1/1440)
    liq.on_pivot(_pivot("H", 500.0, 4))            # unrelated, marks dirty
    _step(atr, liq, _candle(3))
    [pool] = [p for p in liq.pools if p.kind == "EQL"]
    assert pool.strength == pytest.approx(2.0 / (1 + 1 / 1440))


def test_no_pools_while_atr_unwarm():
    atr, liq = _rig(atr_period=50)
    liq.on_pivot(_pivot("H", 110.0, 0))
    liq.on_pivot(_pivot("H", 110.1, 1))
    _step(atr, liq, _candle(0))
    assert atr.value is None and liq.pools == []


def test_pivot_window_slides_at_twenty():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))            # the eventual orphan
    liq.on_pivot(_pivot("H", 110.1, 3))
    for n in range(19):                            # push 19 more: window 20
        liq.on_pivot(_pivot("H", 200.0 + 10 * n, 4 + n))
    _step(atr, liq, _candle(2))
    assert liq.pools == []                         # 110.0 slid out; no pairs


# ------------------------------------------------------------- key levels


def test_day_rollover_promotes_pdh_pdl_when_fully_observed():
    atr, liq = _rig()
    d0 = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)   # observed from the boundary
    _step(atr, liq, _candle(o=100, h=105, l=95, ts=d0))
    _step(atr, liq, _candle(o=101, h=106, l=96,
                            ts=d0 + timedelta(hours=23, minutes=59)))
    assert "PDH" not in liq.key_levels             # day still running
    _step(atr, liq, _candle(o=101, ts=d0 + timedelta(days=1)))   # midnight
    assert liq.key_levels["PDH"] == 106.0          # promoted at midnight
    assert liq.key_levels["PDL"] == 95.0
    assert liq.running_extremes["DAY_H"] == 102.0  # fresh day reset


def test_partial_startup_periods_never_promote():
    """Freeze-audit fix (D7 doctrine): a period not observed from its exact
    boundary start is false data and must never become a sweep target."""
    atr, liq = _rig()
    seed = datetime(2026, 7, 14, 23, 58, tzinfo=UTC)   # mid-day, mid-LATE
    _step(atr, liq, _candle(o=100, h=105, l=95, ts=seed))
    _step(atr, liq, _candle(o=100, ts=seed + timedelta(minutes=2)))  # 00:00
    assert "PDH" not in liq.key_levels and "PDL" not in liq.key_levels
    assert "LATE_H" not in liq.key_levels          # partial session too
    assert "PWH" not in liq.key_levels
    # the NEW day was born at its boundary -> promotes normally
    _step(atr, liq, _candle(o=100, h=103, l=97,
                            ts=seed + timedelta(minutes=3)))
    _step(atr, liq, _candle(o=100, ts=datetime(2026, 7, 16, 0, 0, tzinfo=UTC)))
    assert liq.key_levels["PDH"] == 103.0
    assert liq.key_levels["PDL"] == 97.0


def test_week_rollover_promotes_pwh_pwl():
    atr, liq = _rig()
    monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)   # Monday boundary seed
    _step(atr, liq, _candle(o=100, h=120, l=80, ts=monday))
    _step(atr, liq, _candle(o=100, ts=datetime(2026, 7, 19, 23, 59, tzinfo=UTC)))
    _step(atr, liq, _candle(o=100, ts=datetime(2026, 7, 20, 0, 0, tzinfo=UTC)))
    assert liq.key_levels["PWH"] == 120.0
    assert liq.key_levels["PWL"] == 80.0
    # PDH is still Monday's (the only COMPLETE day observed): the partial
    # Sunday (first candle 23:59) was correctly suppressed
    assert liq.key_levels["PDH"] == 120.0


def test_session_completion_promotes_session_levels():
    atr, liq = _rig()
    london = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)   # session boundary seed
    _step(atr, liq, _candle(o=100, h=104, l=97, ts=london))
    _step(atr, liq, _candle(o=100, ts=london + timedelta(hours=4, minutes=59)))
    assert "LONDON_H" not in liq.key_levels        # LONDON still running
    _step(atr, liq, _candle(o=100, ts=datetime(2026, 7, 14, 13, 0, tzinfo=UTC)))
    assert liq.key_levels["LONDON_H"] == 104.0     # NY began
    assert liq.key_levels["LONDON_L"] == 97.0


# ------------------------------------------------------------------ sweeps


def _sweep_candle(price, ts=None, i=5):
    """High-side sweep of `price`: wick through, body rejected (top wick
    3.9 of range 4 = 97.5%, clearing the strict 60% rule)."""
    return _candle(i, o=price - 2.9, h=price + 1, l=price - 3,
                   c=price - 2.9, ts=ts)


def _sweep_low_candle(price, ts=None, i=5):
    """Low-side mirror: wick under, body rejected above (97.5% wick)."""
    return _candle(i, o=price + 2.9, h=price + 3, l=price - 1,
                   c=price + 2.9, ts=ts)


def test_high_side_pool_sweep_with_wick_criterion_and_latch():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))                    # pool forms (price 110.05)
    events = _step(atr, liq, _sweep_candle(110.05, i=3))
    [sweep] = events
    assert isinstance(sweep, SweepEvent)
    assert (sweep.side, sweep.target) == ("HIGH", "EQH")
    assert sweep.target_price == pytest.approx(110.05)
    assert liq.pools == []                         # swept pool leaves the set
    assert _step(atr, liq, _sweep_candle(110.05, i=4)) == []   # latched


def test_sweep_requires_close_back_below_and_real_wick():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))
    # close ABOVE the level: breakout, not a sweep
    assert _step(atr, liq, _candle(3, o=109, h=111.5, l=108.9, c=111)) == []
    # wick exactly 60% of range: strict > fails; placeholder False proves
    # the RVOL OR-arm cannot rescue it
    assert SWEEP_RVOL_PLACEHOLDER_PASSES is False
    c = _candle(4, o=104.0, h=112.0, l=102.0, c=106.0)   # wick 6, range 10
    assert _step(atr, liq, c) == []


def test_level_sweep_and_rearm_on_period_refresh():
    atr, liq = _rig()
    d1 = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)   # day observed from 00:00
    _step(atr, liq, _candle(o=100, h=105, l=95, ts=d1))
    _step(atr, liq, _candle(o=100, ts=d1 + timedelta(hours=23, minutes=58)))
    day2 = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    _step(atr, liq, _candle(o=100, ts=day2))       # midnight: PDH=105
    events = _step(atr, liq, _sweep_candle(105.0, ts=day2 + timedelta(minutes=1)))
    [sweep] = [e for e in events if e.target == "PDH"]
    # (the completed ASIA session's high sits at 105 too and sweeps with it)
    assert any(e.target == "ASIA_H" for e in events)
    latched = _step(atr, liq, _sweep_candle(105.0, ts=day2 + timedelta(minutes=2)))
    assert [e for e in latched if e.target == "PDH"] == []
    # next midnight refreshes PDH (new period key) -> sweepable again;
    # the sweep candles lifted July-15's high to 106, so that is the new PDH
    day3 = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    _step(atr, liq, _candle(o=100, h=105, l=94, ts=day3 - timedelta(minutes=1)))
    _step(atr, liq, _candle(o=100, ts=day3))
    pdh = liq.key_levels["PDH"]
    assert pdh == 106.0
    [sweep2] = [e for e in _step(atr, liq,
                                 _sweep_candle(pdh, ts=day3 + timedelta(minutes=1)))
                if e.target == "PDH"]
    assert sweep2.bar_index > sweep.bar_index


def test_sweep_shift_within_three_candles_only():
    def rig_with_sweep():
        atr, liq = _rig()
        _warm(atr, liq)
        liq.on_pivot(_pivot("H", 110.0, 2))
        liq.on_pivot(_pivot("H", 110.1, 3))
        _step(atr, liq, _candle(2))
        [sweep] = _step(atr, liq, _sweep_candle(110.05, i=3))
        return atr, liq, sweep

    def choch(i):
        ts = M0 + timedelta(minutes=i)
        piv = _pivot("L", 100.0, 0)
        return ChochEvent("BTCUSDT", "1m", ts, "DOWN", piv, 99.0, "BULLISH")

    # CHOCH on sweep+2 -> tagged
    atr, liq, sweep = rig_with_sweep()
    _step(atr, liq, _candle(4))
    liq.on_choch(choch(5))
    [shift] = _step(atr, liq, _candle(5))
    assert isinstance(shift, SweepShift) and shift.sweep == sweep
    # same-bar CHOCH does NOT tag (D12.5: bars +1..+3) — fed in cadence
    # (D12.7: on_choch precedes that bar's update)
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))
    liq.on_choch(choch(3))                         # same bar as the sweep
    events = _step(atr, liq, _sweep_candle(110.05, i=3))
    assert [type(e) for e in events] == [SweepEvent]     # no shift
    # and the consumed flag must not leak into the next bar
    assert all(not isinstance(e, SweepShift)
               for e in _step(atr, liq, _candle(4)))
    # CHOCH on sweep+4 -> window expired
    atr, liq, _ = rig_with_sweep()
    for i in (4, 5, 6):
        _step(atr, liq, _candle(i))
    liq.on_choch(choch(7))
    assert _step(atr, liq, _candle(7)) == []


def test_low_side_pool_sweep_mirrored():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("L", 90.0, 2))
    liq.on_pivot(_pivot("L", 90.1, 3))
    _step(atr, liq, _candle(2))
    [sweep] = _step(atr, liq, _sweep_low_candle(90.05, i=3))
    assert (sweep.side, sweep.target) == ("LOW", "EQL")
    assert sweep.target_price == pytest.approx(90.05)
    assert liq.pools == []


def test_pool_rearm_on_gained_member_is_fresh_liquidity():
    """D12.2: a swept pool that GAINS a member is a new identity."""
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))
    [first] = _step(atr, liq, _sweep_candle(110.05, i=3))
    assert liq.pools == []                         # grabbed
    liq.on_pivot(_pivot("H", 110.05, 6))           # fresh equal-high forms
    _step(atr, liq, _candle(4))
    [pool] = liq.pools                             # superset: sweepable again
    assert pool.size == 3
    [second] = _step(atr, liq, _sweep_candle(pool.price, i=5))
    assert second.bar_index > first.bar_index


def test_subset_of_swept_pool_stays_latched():
    """Freeze-audit fix: shrinking a grabbed pool (member slid out of the
    window) is NOT fresh liquidity."""
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    liq.on_pivot(_pivot("H", 110.05, 4))
    _step(atr, liq, _candle(2))
    assert liq.pools[0].size == 3
    _step(atr, liq, _sweep_candle(110.05, i=3))    # grab the 3-member pool
    for n in range(18):                            # slide 110.0 out (window 20)
        liq.on_pivot(_pivot("H", 300.0 + 10 * n, 5 + n))
    _step(atr, liq, _candle(4))
    # the {110.1, 110.05} remnant is a subset of the swept set: latched
    assert all(p.price > 200 or p.size < 2 for p in liq.pools)
    assert not any(abs(p.price - 110.075) < 0.01 for p in liq.pools)


def test_cross_kind_pools_latch_independently():
    """Freeze-audit fix: outside-bar H+L pivots share ts — sweeping the EQH
    must not latch the same-ts EQL."""
    atr, liq = _rig()
    _warm(atr, liq)
    for i in (2, 3):                               # same ts for H and L
        liq.on_pivot(_pivot("H", 110.0 + 0.1 * (i - 2), i))
        liq.on_pivot(_pivot("L", 90.0 + 0.1 * (i - 2), i))
    _step(atr, liq, _candle(2))
    assert {p.kind for p in liq.pools} == {"EQH", "EQL"}
    _step(atr, liq, _sweep_candle(110.05, i=3))    # grab the EQH only
    assert [p.kind for p in liq.pools] == ["EQL"]  # EQL stays active


def test_zero_range_candle_never_sweeps():
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))
    flat = _candle(3, o=110.05, h=110.05, l=110.05, c=110.05)
    assert _step(atr, liq, flat) == []             # D12.4 zero-range guard


# ------------------------------------------------------- premium/discount


def test_premium_discount_from_5m_range_with_boundary():
    atr, liq = _rig()
    liq.on_external_pivot(_pivot("H", 120.0, 0, tf="5m"))
    _step(atr, liq, _candle(0, o=110))
    assert liq.premium_discount is None            # L pivot missing
    liq.on_external_pivot(_pivot("L", 100.0, 5, tf="5m"))
    _step(atr, liq, _candle(1, o=112, c=112))      # mid = 110
    assert liq.premium_discount == "premium"
    _step(atr, liq, _candle(2, o=108, c=108))
    assert liq.premium_discount == "discount"
    _step(atr, liq, _candle(3, o=110, c=110))      # exactly mid
    assert liq.premium_discount == "discount"      # boundary pinned (D12.6)


# ------------------------------------------------------------ persistence


async def test_persistence_capability_pools_and_levels(db_conn):
    atr, liq = _rig()
    _warm(atr, liq)
    liq.on_pivot(_pivot("H", 110.0, 2))
    liq.on_pivot(_pivot("H", 110.1, 3))
    _step(atr, liq, _candle(2))
    [pool] = liq.pools
    await db.insert_level(db_conn, **pool_to_row(pool, "BTCUSDT", M0))
    await db.insert_level(db_conn, **key_level_to_row("PDH", 105.0,
                                                      "BTCUSDT", M0))
    await db.insert_level(db_conn, **key_level_to_row("ASIA_H", 104.0,
                                                      "BTCUSDT", M0))
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert [r["kind"] for r in rows] == ["EQH", "PDH", "SESSION_H"]
    with pytest.raises(ValueError):
        key_level_to_row("PWH", 120.0, "BTCUSDT", M0)   # state-only (D12.3)


# ------------------------------------------------------------ determinism


def test_liquidity_determinism_same_feed_twice():
    def run():
        atr, liq = _rig()
        out = []
        _warm(atr, liq)
        liq.on_pivot(_pivot("H", 110.0, 2))
        liq.on_pivot(_pivot("H", 110.1, 3))
        liq.on_external_pivot(_pivot("H", 120.0, 0, tf="5m"))
        liq.on_external_pivot(_pivot("L", 100.0, 5, tf="5m"))
        out.append(_step(atr, liq, _candle(2)))
        out.append(_step(atr, liq, _sweep_candle(110.05, i=3)))
        out.append(_step(atr, liq, _candle(4)))
        return (out, liq.pools, liq.key_levels, liq.premium_discount)
    assert run() == run()

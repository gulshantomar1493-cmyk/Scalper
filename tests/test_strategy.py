"""Tests for the Strategy Engine (§5; Decision D20; roadmap P3.12–P3.16).

Trigger + non-trigger scenarios per strategy (P3.16) with hand-computed
entry/SL/TP arithmetic, driven through the engine's public surface with
constructed frozen-engine outputs (the test_qualification rig precedent).

Hand-computed ATR note: the rig uses IncrementalATR(period=1), whose
Wilder RMA collapses to "ATR == this bar's TR" — every expected value
below is derived from that by hand. Exact-equality assertions reuse the
engine's own float expression shape so they compare bitwise.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from marketscalper.engines.confluence import ConfluenceZone
from marketscalper.engines.liquidity import (
    LiquidityPool,
    SweepEvent,
    SweepShift,
)
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.orderblock import OrderBlock
from marketscalper.engines.strategy import Signal, StrategyEngine
from marketscalper.engines.structure import BosEvent, ChochEvent, Pivot
from marketscalper.engines.trendline import TrendlineEvent
from marketscalper.providers.base import Candle

UTC = timezone.utc
T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _candle(i, o=100.0, c=100.2, h=None, l=None):
    h = max(o, c) + 1 if h is None else h
    l = min(o, c) - 1 if l is None else l
    return Candle(symbol="BTCUSDT", tf="1m", ts=T0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _pivot(kind, price, label="HL", minute=0):
    ts = T0 + timedelta(minutes=minute)
    return Pivot("BTCUSDT", "1m", ts, ts, kind, price, label=label)


class _Rig:
    def __init__(self):
        self.atr = IncrementalATR(period=1)
        self.engine = StrategyEngine("BTCUSDT", self.atr)

    def step(self, candle, **kw):
        self.atr.update(candle)
        kw.setdefault("trend_5m", None)
        kw.setdefault("bos_event", None)
        kw.setdefault("choch_event", None)
        kw.setdefault("tl_events", [])
        kw.setdefault("liq_events", [])
        kw.setdefault("zones", [])
        kw.setdefault("blocks", [])
        kw.setdefault("gaps", [])
        kw.setdefault("pools", [])
        kw.setdefault("levels", {})
        kw.setdefault("premium_discount", None)
        kw.setdefault("session_vwap", None)
        kw.setdefault("rvol", None)
        return self.engine.evaluate(candle, **kw)


def _zone(lo, hi, count=2, direction="BULL"):
    return ConfluenceZone("OB", direction, lo, hi, count,
                          ("OB",) * count, count >= 3, T0)


def _shift(sweep_minute, shift_minute, side="LOW", target="EQL"):
    sweep = SweepEvent("BTCUSDT", T0 + timedelta(minutes=sweep_minute),
                       sweep_minute, side, target, 95.0)
    ts = T0 + timedelta(minutes=shift_minute)
    return SweepShift(sweep, ts, ts)


def _choch(minute, direction="UP"):
    """The reversal CHOCH the shift pairs with (same-bar in the rigs)."""
    ts = T0 + timedelta(minutes=minute)
    prior = "BEARISH" if direction == "UP" else "BULLISH"
    return ChochEvent("BTCUSDT", "1m", ts, direction,
                      _pivot("H" if direction == "UP" else "L", 97.0, None),
                      99.0, prior)


# ------------------------------------------------------------------ S1


def _s1_rig():
    """Sweep candle at bar 0 (low 94.0), shift arrives at bar 2.

    Bar-2 TR = max(98-96.5, |98-96.8|, |96.5-96.8|) = 1.5 -> ATR = 1.5.
    Entry zone [97, 98] midpoint 97.5; sl 94 - 0.375 = 93.625; r = 3.875,
    so the opposing pool must sit at or above 101.375.
    """
    rig = _Rig()
    rig.step(_candle(0, o=97.0, c=96.5, h=97.5, l=94.0))    # sweep candle
    rig.step(_candle(1, o=96.5, c=96.8, h=97.3, l=95.8))
    return rig


_S1_BAR2 = dict(o=96.8, c=98.0, h=98.0, l=96.5)


def test_s1_long_trigger_hand_computed():
    rig = _s1_rig()
    signals = rig.step(
        _candle(2, **_S1_BAR2),
        liq_events=[_shift(0, 2)], choch_event=_choch(2),
        premium_discount="discount",                # A8 extreme context
        zones=[_zone(97.0, 98.0)],                  # close 98 inside zone
        pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    [s] = signals
    assert (s.strategy, s.direction) == ("S1", "LONG")
    assert s.entry == 97.5                          # zone midpoint
    assert s.sl == 94.0 - 0.25 * 1.5                # sweep low - buffer
    r = s.entry - s.sl
    assert r == 3.875                               # exact binary floats
    assert s.tp1 == 101.5                           # nearest opposing pool
    assert s.tp1 >= s.entry + r                     # §5: 1R minimum
    assert s.tp2 is None                            # no external 5m H swing
    assert s.invalid_after_bars == 5
    assert any("swept EQL" in f for f in s.facts)
    assert any("A8 extreme" in f for f in s.facts)


def test_s1_tp2_from_external_swing_and_context_via_5m_trend():
    rig = _s1_rig()
    rig.engine.on_external_pivot(_pivot("H", 103.0, "HH"))
    [s] = rig.step(
        _candle(2, **_S1_BAR2),
        liq_events=[_shift(0, 2)], choch_event=_choch(2),
        trend_5m="BULLISH",                              # trend context
        zones=[_zone(97.0, 98.0)],
        pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    assert s.tp2 == 103.0                           # beyond tp1 -> kept
    assert any("5m trend" in f for f in s.facts)


def test_s1_external_swing_not_beyond_tp1_gives_no_tp2():
    rig = _s1_rig()
    rig.engine.on_external_pivot(_pivot("H", 101.4, "LH"))   # below tp1
    [s] = rig.step(
        _candle(2, **_S1_BAR2),
        liq_events=[_shift(0, 2)], choch_event=_choch(2),
        premium_discount="discount",
        zones=[_zone(97.0, 98.0)],
        pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    assert s.tp2 is None


def test_s1_choch_on_earlier_bar_still_pairs():
    """D12.5 allows the shift tag 1-3 bars after the CHOCH bar — the
    engine's ts-addressable CHOCH record must span that window."""
    rig = _s1_rig()
    # CHOCH observed on bar 2, shift arrives on bar 3 citing bar 2's ts
    rig.step(_candle(2, **_S1_BAR2), choch_event=_choch(2))
    sweep = SweepEvent("BTCUSDT", T0, 0, "LOW", "EQL", 95.0)
    shift = SweepShift(sweep, T0 + timedelta(minutes=2),
                       T0 + timedelta(minutes=3))
    [s] = rig.step(
        _candle(3, o=98.0, c=98.2, h=98.4, l=97.6),
        liq_events=[shift], premium_discount="discount",
        zones=[_zone(97.4, 98.4)],
        pools=[LiquidityPool("EQH", 103.0, 2, 1.0, (T0,))])
    assert (s.strategy, s.direction) == ("S1", "LONG")


def test_s1_non_triggers():
    base = dict(liq_events=[_shift(0, 2)], choch_event=_choch(2),
                premium_discount="discount",
                zones=[_zone(97.0, 98.0)],
                pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    bar2 = _candle(2, **_S1_BAR2)
    # paired CHOCH broke DOWN (the continuation story, not a reversal):
    # a LOW sweep must pair with an UP CHOCH
    kw = dict(base, choch_event=_choch(2, "DOWN"))
    assert _s1_rig().step(bar2, **kw) == []
    # paired CHOCH unknown (choch_ts never observed) -> no signal (D7)
    kw = dict(base, choch_event=None)
    assert _s1_rig().step(bar2, **kw) == []
    # no context at all (5m trend None, no A8 extreme)
    kw = dict(base, premium_discount=None)
    assert _s1_rig().step(bar2, **kw) == []
    # misaligned context (LOW sweep in premium)
    kw = dict(base, premium_discount="premium")
    assert _s1_rig().step(bar2, **kw) == []
    # zone count 1 (< 2 confluence)
    kw = dict(base, zones=[_zone(97.0, 98.0, count=1)])
    assert _s1_rig().step(bar2, **kw) == []
    # zone direction mismatch (BEAR zone for a LONG)
    kw = dict(base, zones=[_zone(97.0, 98.0, direction="BEAR")])
    assert _s1_rig().step(bar2, **kw) == []
    # zone too far from the CHOCH close (gap 1.0 > 0.3*1.5 = 0.45); the
    # far pool keeps every other gate passing so ONLY proximity rejects
    # (entry 99.5, r 5.875, entry+r 105.375 <= 107)
    kw = dict(base, zones=[_zone(99.0, 100.0)],
              pools=[LiquidityPool("EQH", 107.0, 2, 1.0, (T0,))])
    assert _s1_rig().step(bar2, **kw) == []
    # no opposing pool at all
    kw = dict(base, pools=[])
    assert _s1_rig().step(bar2, **kw) == []
    # pool closer than 1R (entry 97.5, r 3.875 -> needs >= 101.375)
    kw = dict(base, pools=[LiquidityPool("EQH", 101.0, 2, 1.0, (T0,))])
    assert _s1_rig().step(bar2, **kw) == []
    # unmatched sweep ts (never observed) -> no signal (D7 fail-closed)
    kw = dict(base, liq_events=[_shift(-20, 2)])
    assert _s1_rig().step(bar2, **kw) == []


def test_s1_pool_selection_nearest_and_above_entry_only():
    """D20.2: TP1 = the CHEAPEST opposing pool above entry; below-entry
    pools of the right kind are never targets (an unfiltered min would
    pick 95.0 and wrongly 1R-reject the whole signal)."""
    base = dict(liq_events=[_shift(0, 2)], choch_event=_choch(2),
                premium_discount="discount", zones=[_zone(97.0, 98.0)])
    bar2 = _candle(2, **_S1_BAR2)
    kw = dict(base, pools=[LiquidityPool("EQH", 103.0, 2, 1.0, (T0,)),
                           LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    [s] = _s1_rig().step(bar2, **kw)
    assert s.tp1 == 101.5                           # nearest, not farthest
    kw = dict(base, pools=[LiquidityPool("EQH", 95.0, 2, 1.0, (T0,)),
                           LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    [s] = _s1_rig().step(bar2, **kw)
    assert s.tp1 == 101.5                           # below-entry filtered


def test_s1_recent_window_eviction_fails_closed():
    """A sweep candle that has aged out of the 8-bar recent window can
    no longer anchor an SL -> no signal (D7), even with a fresh CHOCH."""
    rig = _Rig()
    rig.step(_candle(0, o=97.0, c=96.5, h=97.5, l=94.0))    # the sweep bar
    for i in range(1, 10):                                  # bars 1..9
        rig.step(_candle(i, o=96.8, c=96.9, h=97.3, l=96.4))
    sweep = SweepEvent("BTCUSDT", T0, 0, "LOW", "EQL", 95.0)
    ts10 = T0 + timedelta(minutes=10)
    shift = SweepShift(sweep, ts10, ts10)
    out = rig.step(_candle(10, **_S1_BAR2),
                   liq_events=[shift], choch_event=_choch(10),
                   premium_discount="discount", zones=[_zone(97.0, 98.0)],
                   pools=[LiquidityPool("EQH", 105.0, 2, 1.0, (T0,))])
    assert out == []                                # bar 0 evicted (10 > 8)


def test_s1_choch_lag_with_intervening_chochs_still_pairs():
    """The ts-addressable CHOCH record survives the D12.5 defensive
    3-bar lag with other CHOCHs recorded in between."""
    rig = _s1_rig()
    rig.step(_candle(2, o=96.8, c=96.9, h=97.3, l=96.4),
             choch_event=_choch(2))                 # the UP CHOCH
    rig.step(_candle(3, o=96.9, c=96.8, h=97.2, l=96.5),
             choch_event=_choch(3, "DOWN"))         # intervening
    rig.step(_candle(4, o=96.8, c=96.7, h=97.1, l=96.4),
             choch_event=_choch(4, "DOWN"))         # intervening
    sweep = SweepEvent("BTCUSDT", T0, 0, "LOW", "EQL", 95.0)
    shift = SweepShift(sweep, T0 + timedelta(minutes=2),
                       T0 + timedelta(minutes=5))   # cites bar 2's CHOCH
    [s] = rig.step(_candle(5, **_S1_BAR2),
                   liq_events=[shift], premium_discount="discount",
                   zones=[_zone(97.0, 98.0)],
                   pools=[LiquidityPool("EQH", 105.0, 2, 1.0, (T0,))])
    assert (s.strategy, s.direction) == ("S1", "LONG")


def test_s1_external_swing_equal_to_tp1_gives_no_tp2():
    """D20.2: TP2 must be strictly beyond TP1 — equality drops it."""
    rig = _s1_rig()
    rig.engine.on_external_pivot(_pivot("H", 101.5, "HH"))
    [s] = rig.step(
        _candle(2, **_S1_BAR2),
        liq_events=[_shift(0, 2)], choch_event=_choch(2),
        premium_discount="discount", zones=[_zone(97.0, 98.0)],
        pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    assert s.tp1 == 101.5 and s.tp2 is None


def test_s1_short_mirror():
    rig = _Rig()
    rig.step(_candle(0, o=103.0, c=103.5, h=106.0, l=102.5))  # sweep hi 106
    rig.step(_candle(1, o=103.5, c=103.2, h=104.2, l=102.7))
    # bar-2 TR = max(103.5-102, |103.5-103.2|, |102-103.2|) = 1.5
    [s] = rig.step(
        _candle(2, o=103.2, c=102.0, h=103.5, l=102.0),
        liq_events=[_shift(0, 2, side="HIGH", target="EQH")],
        choch_event=_choch(2, "DOWN"),
        premium_discount="premium",
        zones=[_zone(102.0, 103.0, direction="BEAR")],
        pools=[LiquidityPool("EQL", 96.0, 2, 1.0, (T0,))])
    assert (s.strategy, s.direction) == ("S1", "SHORT")
    assert s.entry == 102.5
    assert s.sl == 106.0 + 0.25 * 1.5               # sweep high + buffer
    assert s.tp1 == 96.0                            # r = 3.875, 1R ok
    assert any("swept EQH" in f for f in s.facts)


# ------------------------------------------------------------------ S2


def _s2_arm():
    """Bars 0-2: L pivot 100.0, BOS UP close 110.0 at bar 2 (rvol 2.0).

    Impulse: lo=100.0, hi=110.0, rng=10.0.
    """
    rig = _Rig()
    rig.step(_candle(0, o=100.0, c=100.5))
    rig.step(_candle(1, o=100.5, c=101.0))
    rig.engine.on_pivot(_pivot("L", 100.0, "HL"))
    bos = BosEvent("BTCUSDT", "1m", T0 + timedelta(minutes=2), "UP",
                   _pivot("H", 105.0, "HH"), 110.0, True)
    rig.step(_candle(2, o=101.0, c=110.0, h=110.5, l=100.9),
             bos_event=bos, rvol=2.0)
    return rig


_S2_CTX = dict(trend_5m="BULLISH", session_vwap=101.0)
_S2_OB = OrderBlock("BULL", 104.5, 105.5, T0, 0, T0)
# pullback bar 3: low 105.0 -> depth (110-105)/10 = 0.5, overlaps the OB,
# rvol 1.0 < 2.0 (declining) -> SETUP
_S2_PULLBACK = dict(o=110.0, c=105.4, h=110.2, l=105.0)
# confirm bar 4: TR = max(106.9-105.3, |106.9-105.4|, |105.3-105.4|) = 1.6
# -> ATR 1.6; body 1.4 > 0.8*1.6 = 1.28; entry 106.8, sl 105.0 - 0.4 =
# 104.6, r 2.2, tp1 110 >= 109.0 (1R ok)
_S2_CONFIRM = dict(o=105.4, c=106.8, h=106.9, l=105.3)


def test_s2_long_trigger_hand_computed():
    rig = _s2_arm()
    assert rig.step(_candle(3, **_S2_PULLBACK),
                    blocks=[_S2_OB], rvol=1.0, **_S2_CTX) == []
    [s] = rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX)
    assert (s.strategy, s.direction) == ("S2", "LONG")
    assert s.entry == 106.8                         # confirm close
    atr = rig.atr.value
    assert abs(atr - 1.6) < 1e-9
    assert s.sl == 105.0 - 0.25 * atr               # pullback low - buffer
    assert s.tp1 == 110.0                           # impulse high
    assert s.tp1 >= s.entry + (s.entry - s.sl)      # §5: 1R minimum
    assert s.tp2 == 105.0 + 1.618 * (110.0 - 100.0)  # fib extension
    assert any("depth 0.50" in f for f in s.facts)


def test_s2_one_r_minimum_rejects_entry_near_impulse_high():
    """§5 'TP1 (1R min)': a confirm that storms back to the impulse high
    leaves tp1 == entry -> no signal, and the impulse is consumed.

    The tail proves consumption with a bar-6 vector that would fire on a
    surviving impulse (TR 1.3, body 1.2 > 1.04, sl 104.675, r 2.525,
    tp1 110 >= 109.725 — every gate passes except the impulse is gone).
    """
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    # body 4.6 > 0.8*TR(5.1) = 4.08 and rvol ok -> confirm fires, but
    # entry 110.0 == tp1 110.0 fails the 1R minimum
    assert rig.step(_candle(4, o=105.4, c=110.0, h=110.3, l=105.2),
                    rvol=1.3, **_S2_CTX) == []
    # red drift, no confirm attempt (extreme stays 105.0)
    assert rig.step(_candle(5, o=110.0, c=106.0, h=110.05, l=105.9),
                    rvol=1.2, **_S2_CTX) == []
    # a fully-qualifying confirm — silent only because of consumption
    assert rig.step(_candle(6, o=106.0, c=107.2, h=107.25, l=105.95),
                    rvol=1.3, **_S2_CTX) == []


def test_s2_non_triggers():
    # depth too shallow (< 30%): low 108.0 -> depth 0.2, never a setup.
    # Bar 4 is an otherwise-valid confirm (TR 0.8, body 0.7 > 0.64,
    # rvol ok, and 1R would pass) -- only the depth gate stops it.
    rig = _s2_arm()
    shallow_ob = OrderBlock("BULL", 107.5, 108.5, T0, 0, T0)
    rig.step(_candle(3, o=110.0, c=108.4, h=110.2, l=108.0),
             blocks=[shallow_ob], rvol=1.0, **_S2_CTX)
    assert rig.step(_candle(4, o=108.15, c=108.85, h=108.9, l=108.1),
                    rvol=1.3, **_S2_CTX) == []
    # rvol rising during the pullback: no setup
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=2.5,
             **_S2_CTX)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX) == []
    # no zone touch (no OB/FVG/VWAP/trendline contact): no setup
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), rvol=1.0, **_S2_CTX)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX) == []
    # 5m trend misaligned: no setup
    rig = _s2_arm()
    kw = dict(_S2_CTX, trend_5m="RANGE")
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0, **kw)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **kw) == []
    # VWAP unwarm (None): no setup (D7)
    rig = _s2_arm()
    kw = dict(_S2_CTX, session_vwap=None)
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0, **kw)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **kw) == []
    # full retrace AFTER a recorded setup cancels the impulse (the
    # confirm path never re-checks depth, so cancellation is the only
    # gate left: without it, bar 5 fires at entry 100.9 with sl 98.9)
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    rig.step(_candle(4, o=105.4, c=99.5, h=105.5, l=99.3),
             rvol=1.0, **_S2_CTX)
    assert rig.step(_candle(5, o=99.5, c=100.9, h=101.0, l=99.4),
                    rvol=1.3, **_S2_CTX) == []
    # opposite BOS cancels the armed impulse (pullback+confirm follow)
    rig = _s2_arm()
    down = BosEvent("BTCUSDT", "1m", T0 + timedelta(minutes=3), "DOWN",
                    _pivot("L", 104.0, "LL"), 103.0, True)
    rig.step(_candle(3, o=110.0, c=103.0, h=110.1, l=102.9),
             bos_event=down, rvol=1.0, **_S2_CTX)
    rig.step(_candle(4, **_S2_PULLBACK), blocks=[_S2_OB], rvol=0.8,
             **_S2_CTX)
    assert rig.step(_candle(5, **_S2_CONFIRM), rvol=1.3, **_S2_CTX) == []
    # confirm rvol below 1.2: no signal on that bar
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.19, **_S2_CTX) == []
    # confirm body not > 0.8*ATR: no signal (body 0.5, TR 1.0)
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    assert rig.step(_candle(4, o=105.4, c=105.9, h=106.4, l=105.4),
                    rvol=1.3, **_S2_CTX) == []


def test_s2_zone_touch_direction_pin_and_all_arms():
    """D20.3 addendum: OB/FVG arms are direction-matched (a BEAR OB is
    not pullback-support for a LONG); the VWAP-cross and TOUCH arms also
    arm the setup independently."""
    from marketscalper.engines.fvg import FairValueGap

    # BEAR OB at the same coords never arms a LONG setup
    rig = _s2_arm()
    bear = OrderBlock("BEAR", 104.5, 105.5, T0, 0, T0)
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[bear], rvol=1.0,
             **_S2_CTX)
    assert rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX) == []
    # BULL FVG arm
    rig = _s2_arm()
    gap = FairValueGap("BULL", 104.5, 105.5, 0, T0)
    rig.step(_candle(3, **_S2_PULLBACK), gaps=[gap], rvol=1.0, **_S2_CTX)
    out = rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX)
    assert [s.strategy for s in out] == ["S2"]
    # VWAP-cross arm (vwap 105.2 inside the pullback bar's range)
    rig = _s2_arm()
    ctx = dict(_S2_CTX, session_vwap=105.2)
    rig.step(_candle(3, **_S2_PULLBACK), rvol=1.0, **ctx)
    out = rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **ctx)
    assert [s.strategy for s in out] == ["S2"]
    # trendline TOUCH arm (side-blind per the addendum)
    rig = _s2_arm()
    touch = TrendlineEvent("TOUCH", "resistance", 1, 5, 3,
                           T0 + timedelta(minutes=3), 105.4)
    rig.step(_candle(3, **_S2_PULLBACK), tl_events=[touch], rvol=1.0,
             **_S2_CTX)
    out = rig.step(_candle(4, **_S2_CONFIRM), rvol=1.3, **_S2_CTX)
    assert [s.strategy for s in out] == ["S2"]


def test_s2_confirm_boundaries_exact():
    """rvol == 1.2 exactly passes (inclusive); body == 0.8*ATR exactly
    fails (strict), with dyadic-exact arithmetic (TR 2.5, body 2.0)."""
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    [s] = rig.step(_candle(4, **_S2_CONFIRM), rvol=1.2, **_S2_CTX)
    assert s.strategy == "S2"
    # body boundary: o=104.75, c=106.75 -> body 2.0; h-l = 2.5 = TR;
    # 0.8 * 2.5 == 2.0 exactly -> strict > fails, nothing else blocks
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    assert rig.step(_candle(4, o=104.75, c=106.75, h=107.25, l=104.75),
                    rvol=1.3, **_S2_CTX) == []
    # positive twin: one tick more body on the same geometry fires
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)
    out = rig.step(_candle(4, o=104.65, c=106.75, h=107.25, l=104.65),
                   rvol=1.3, **_S2_CTX)
    assert [s.strategy for s in out] == ["S2"]


def test_s2_tp2_clipped_to_none_and_setup_depth_fact():
    """A deep post-setup wick pulls the 1.618 extension back inside the
    impulse (tp2 <= tp1 -> None), SL uses the live extreme (D20.3
    letter), and the §8 fact cites the SETUP-bar depth the rule actually
    evaluated (addendum) — never the later deepened value."""
    rig = _s2_arm()
    rig.step(_candle(3, **_S2_PULLBACK), blocks=[_S2_OB], rvol=1.0,
             **_S2_CTX)                             # setup depth 0.50
    # deep wick: low 93.0, close 100.5 > lo 100 -> no cancel; extreme 93
    rig.step(_candle(4, o=105.4, c=100.5, h=105.5, l=93.0),
             rvol=1.0, **_S2_CTX)
    # confirm: TR = max(0.9, 0.85, 0.05) = 0.9; body 0.8 > 0.72;
    # sl 93 - 0.225 = 92.775, r 8.525, tp1 110 >= 109.825 (1R ok);
    # tp2 = 93 + 16.18 = 109.18 <= 110 -> None
    [s] = rig.step(_candle(5, o=100.5, c=101.3, h=101.35, l=100.45),
                   rvol=1.3, **_S2_CTX)
    assert s.tp2 is None
    atr = rig.atr.value
    assert s.sl == 93.0 - 0.25 * atr                # live extreme
    assert any("depth 0.50" in f for f in s.facts)  # setup-bar depth


def test_s2_setup_and_confirm_never_same_bar():
    """D20.3: CONFIRM is 'a later close' — a bar that satisfies both the
    setup and the confirm conditions only records the setup.

    Bar 3 is confirm-quality (green, body 3.9 > 0.8*TR(4.5) = 3.6, rvol
    1.3 >= 1.2) AND setup-quality (extreme 105.5 -> depth 0.45, OB touch,
    declining rvol) -> no signal. If the engine wrongly confirmed on bar
    3, entry 109.5 would fail 1R AND consume the impulse, so bar 5 would
    stay empty too — the final assertion catches that mutant.
    """
    rig = _s2_arm()
    assert rig.step(_candle(3, o=105.6, c=109.5, h=109.6, l=105.5),
                    blocks=[_S2_OB], rvol=1.3, **_S2_CTX) == []
    # bar 4: red drift, no confirm attempt (extreme stays 105.5)
    assert rig.step(_candle(4, o=109.4, c=106.0, h=109.4, l=105.9),
                    rvol=1.2, **_S2_CTX) == []
    # bar 5: TR = max(106.9-105.9, |106.9-106|, |105.9-106|) = 1.0;
    # body 0.85 > 0.8; entry 106.85, sl 105.5-0.25 = 105.25, r 1.6,
    # tp1 110 >= 108.45 (1R ok)
    out = rig.step(_candle(5, o=106.0, c=106.85, h=106.9, l=105.9),
                   rvol=1.3, **_S2_CTX)
    assert [s.strategy for s in out] == ["S2"]
    assert out[0].entry == 106.85


def test_s2_short_mirror():
    rig = _Rig()
    rig.step(_candle(0, o=110.0, c=109.5))
    rig.step(_candle(1, o=109.5, c=109.0))
    rig.engine.on_pivot(_pivot("H", 110.0, "LH"))
    bos = BosEvent("BTCUSDT", "1m", T0 + timedelta(minutes=2), "DOWN",
                   _pivot("L", 105.0, "LL"), 100.0, True)
    rig.step(_candle(2, o=109.0, c=100.0, h=109.1, l=99.5),
             bos_event=bos, rvol=2.0)               # impulse hi 110, lo 100
    ctx = dict(trend_5m="BEARISH", session_vwap=109.0)
    bear_ob = OrderBlock("BEAR", 104.5, 105.5, T0, 0, T0)
    # pullback bar 3: high 105.0 -> depth (105-100)/10 = 0.5
    assert rig.step(_candle(3, o=100.0, c=104.6, h=105.0, l=99.8),
                    blocks=[bear_ob], rvol=1.0, **ctx) == []
    # confirm bar 4: TR = max(104.7-103.1, |104.7-104.6|, |103.1-104.6|)
    # = 1.6; red body 1.4 > 1.28; entry 103.2, sl 105.4, r 2.2,
    # tp1 100 <= 101.0 (1R ok)
    [s] = rig.step(_candle(4, o=104.6, c=103.2, h=104.7, l=103.1),
                   rvol=1.3, **ctx)
    assert (s.strategy, s.direction) == ("S2", "SHORT")
    assert s.entry == 103.2
    assert s.sl == 105.0 + 0.25 * rig.atr.value     # pullback high + buffer
    assert s.tp1 == 100.0                           # impulse low
    assert s.tp1 <= s.entry - (s.sl - s.entry)      # 1R minimum (short)
    assert s.tp2 == 105.0 - 1.618 * (110.0 - 100.0)


# ------------------------------------------------------------------ S3


def _fake_break(minute, side="support"):
    return TrendlineEvent("FAKE_BREAK", side, 1, 5, minute,
                          T0 + timedelta(minutes=minute), 0.0)


def _s3_rig(with_target=True):
    """Watch-span lows 97.0 / 96.0 / 96.5 -> extreme 96.0; last H 108.0.

    Event bar 3: TR = max(100.5-98.8, |100.5-99.2|, |98.8-99.2|) = 1.7;
    entry 100.0, sl 96 - 0.425 = 95.575, r 4.425, tp1 108 >= 104.425.
    """
    rig = _Rig()
    if with_target:
        rig.engine.on_pivot(_pivot("H", 108.0, "HH"))
    rig.step(_candle(0, o=100.0, c=99.5, l=97.0))
    rig.step(_candle(1, o=99.5, c=99.0, l=96.0))
    rig.step(_candle(2, o=99.0, c=99.2, l=96.5))
    return rig


_S3_BAR3 = dict(o=99.2, c=100.0, h=100.5, l=98.8)


def test_s3_long_trigger_hand_computed():
    rig = _s3_rig()
    [s] = rig.step(_candle(3, **_S3_BAR3),
                   tl_events=[_fake_break(3)], rvol=1.5)
    assert (s.strategy, s.direction) == ("S3", "LONG")
    assert s.entry == 100.0                         # re-entry close
    atr = rig.atr.value
    assert abs(atr - 1.7) < 1e-9
    assert s.sl == 96.0 - 0.25 * atr                # window extreme - buffer
    assert s.tp1 == 108.0                           # last opposite 1m pivot
    assert s.tp1 >= s.entry + (s.entry - s.sl)      # 1R minimum
    assert s.tp2 is None
    assert s.invalid_after_bars == 5
    assert any("fake break" in f for f in s.facts)


def test_s3_short_mirror():
    rig = _Rig()
    rig.engine.on_pivot(_pivot("L", 92.0, "LL"))
    rig.step(_candle(0, o=100.0, c=100.5, h=103.0))
    rig.step(_candle(1, o=100.5, c=101.0, h=104.0))   # extreme high 104.0
    rig.step(_candle(2, o=101.0, c=100.8, h=103.5))
    [s] = rig.step(_candle(3, o=100.8, c=100.0, h=101.2, l=99.5),
                   tl_events=[_fake_break(3, side="resistance")], rvol=2.0)
    assert (s.strategy, s.direction) == ("S3", "SHORT")
    assert s.entry == 100.0
    assert s.sl == 104.0 + 0.25 * rig.atr.value
    assert s.tp1 == 92.0


def test_s3_non_triggers():
    bar3 = _candle(3, **_S3_BAR3)
    # rvol below 1.5 / unwarm (inclusive boundary: 1.5 itself passes,
    # proven by the trigger test)
    assert _s3_rig().step(bar3, tl_events=[_fake_break(3)],
                          rvol=1.49) == []
    assert _s3_rig().step(bar3, tl_events=[_fake_break(3)],
                          rvol=None) == []
    # opposing key level strictly inside 1R (entry 100, r ~4.425)
    assert _s3_rig().step(bar3, tl_events=[_fake_break(3)], rvol=1.5,
                          levels={"PDH": 102.0}) == []
    # opposing pool strictly inside 1R blocks too
    assert _s3_rig().step(
        bar3, tl_events=[_fake_break(3)], rvol=1.5,
        pools=[LiquidityPool("EQH", 103.0, 2, 1.0, (T0,))]) == []
    # a level on the SL side (below entry, LONG) never blocks
    [s] = _s3_rig().step(bar3, tl_events=[_fake_break(3)], rvol=1.5,
                         levels={"PDL": 90.0})
    assert s.strategy == "S3"
    # no last opposite swing -> no target -> no signal (D7)
    assert _s3_rig(with_target=False).step(
        bar3, tl_events=[_fake_break(3)], rvol=1.5) == []
    # target closer than 1R: last H at 103.0 < entry + r (~104.425)
    rig = _s3_rig(with_target=False)
    rig.engine.on_pivot(_pivot("H", 103.0, "HH"))
    assert rig.step(bar3, tl_events=[_fake_break(3)], rvol=1.5) == []
    # TOUCH / BREAK events never arm S3
    for kind in ("TOUCH", "BREAK"):
        ev = TrendlineEvent(kind, "support", 1, 5, 3, bar3.ts, 0.0)
        assert _s3_rig().step(bar3, tl_events=[ev], rvol=1.5) == []


def test_s3_level_beyond_one_r_does_not_block():
    """The barrier scan is (entry, entry+R) exclusive — levels in the
    target region beyond 1R do not veto the trade."""
    rig = _s3_rig()
    [s] = rig.step(_candle(3, **_S3_BAR3), tl_events=[_fake_break(3)],
                   rvol=1.5, levels={"PWH": 107.0})   # 107 > 104.425
    assert s.strategy == "S3"


# ------------------------------------------------------- template/system


def test_multiple_strategies_same_bar_deterministic_order():
    """S1 and S3 both firing on one bar emit in the pinned S1->S3 order.

    Pool at 102.5: satisfies S1's 1R (>= 101.375) while sitting beyond
    S3's 1R barrier scan (entry 98.0, r 4.375 -> scan tops at 102.375).
    """
    rig = _s1_rig()
    rig.engine.on_pivot(_pivot("H", 108.0, "HH"))
    out = rig.step(
        _candle(2, **_S1_BAR2),
        liq_events=[_shift(0, 2)], choch_event=_choch(2),
        premium_discount="discount",
        zones=[_zone(97.0, 98.0)],
        pools=[LiquidityPool("EQH", 102.5, 2, 1.0, (T0,))],
        tl_events=[_fake_break(2)], rvol=1.5)
    assert [s.strategy for s in out] == ["S1", "S3"]


def test_signals_are_immutable_and_deterministic():
    def run():
        rig = _s1_rig()
        return rig.step(
            _candle(2, **_S1_BAR2),
            liq_events=[_shift(0, 2)], choch_event=_choch(2),
            premium_discount="discount",
            zones=[_zone(97.0, 98.0)],
            pools=[LiquidityPool("EQH", 101.5, 2, 1.0, (T0,))])
    a, b = run(), run()
    assert a == b and isinstance(a[0], Signal)
    with pytest.raises(FrozenInstanceError):
        a[0].entry = 0.0

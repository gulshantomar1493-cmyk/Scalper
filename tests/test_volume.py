"""Tests for the Volume Engine (§4.6; Decision D19; roadmap P2.1–P2.7),
including the P2.2 seam boundaries in the trendline/liquidity engines."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sqrt

import pytest

from marketscalper.engines.liquidity import LiquidityEngine
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import Pivot
from marketscalper.engines.trendline import TrendlineBook, TrendlineDetector
from marketscalper.engines.volume import (
    RVOL_WINDOW_DAYS,
    VolumeEngine,
    candle_delta,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
D0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)        # a 00:00 UTC day start


def _candle(ts, o=100.0, h=None, l=None, c=None, v=1.0, tbv=None):
    c = o if c is None else c
    h = max(o, c) + 1 if h is None else h
    l = min(o, c) - 1 if l is None else l
    tbv = v / 2 if tbv is None else tbv
    return Candle(symbol="BTCUSDT", tf="1m", ts=ts, o=o, h=h, l=l, c=c,
                  v=v, qv=v * o, n_trades=1, taker_buy_v=tbv)


def _rig(atr_period=1):
    atr = IncrementalATR(period=atr_period)
    return atr, VolumeEngine("BTCUSDT", atr)


def _step(atr, vol, candle, key_levels=None, pools=None, extremes=None):
    atr.update(candle)
    vol.update(candle)
    vol.classify(candle, key_levels or {}, pools or [], extremes or {})


def _noon(day_offset, **kw):
    return _candle(D0 + timedelta(days=day_offset, hours=12), **kw)


def _seed_noon(vol, volumes):
    vol.seed([_noon(i, v=float(v)) for i, v in enumerate(volumes)])


# --------------------------------------------------------------- RVOL


def test_rvol_requires_a_full_window_then_computes():
    atr, vol = _rig()
    _seed_noon(vol, range(1, 20))                  # 19 observations
    _step(atr, vol, _noon(30, v=21.0))
    assert vol.rvol is None                        # D7: short window
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))                  # volumes 1..20
    _step(atr, vol, _noon(30, v=21.0))
    assert vol.rvol == 21.0 / 10.5                 # median(1..20) = 10.5
    assert vol.spike is True                       # 2.0 inclusive (§4.6)


def test_rvol_window_slides_and_never_self_references():
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))
    _step(atr, vol, _noon(30, v=21.0))             # scored vs {1..20}
    assert vol.rvol == 2.0                         # not vs its own 21
    _step(atr, vol, _candle(D0 + timedelta(days=31, hours=12), v=10.0))
    assert vol.rvol == 10.0 / 11.5                 # window now {2..21}


def test_rvol_zero_median_and_other_minutes_unwarm():
    atr, vol = _rig()
    _seed_noon(vol, [0.0] * 20)
    _step(atr, vol, _noon(30, v=5.0))
    assert vol.rvol is None                        # degenerate median
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))                  # only 12:00 seeded
    _step(atr, vol, _candle(D0 + timedelta(days=30, hours=13), v=5.0))
    assert vol.rvol is None                        # 13:00 bucket empty


def test_seed_equals_stream_fold():
    atr_a, vol_a = _rig()
    _seed_noon(vol_a, range(1, 21))
    atr_b, vol_b = _rig()
    for i in range(20):                            # same history via update
        _step(atr_b, vol_b, _noon(i, v=float(i + 1)))
    probe = _noon(30, v=21.0)
    _step(atr_a, vol_a, probe)
    _step(atr_b, vol_b, probe)
    assert vol_a.rvol == vol_b.rvol == 2.0


def test_seed_touches_buckets_only():
    """D19.2 pin: seeding must never pollute day/anchor/session state."""
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))
    first = _candle(D0, o=100.0, h=102.0, l=98.0, c=100.0, v=2.0, tbv=1.5)
    _step(atr, vol, first)
    assert vol.session_vwap == 100.0               # the day sums start fresh
    assert vol.cum_delta == 1.0                    # 2*1.5-2 only
    assert vol.anchored_vwap is None and vol.anchor_ts is None


def test_spike_boundary_inclusive():
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))
    _step(atr, vol, _noon(30, v=20.9))             # 20.9/10.5 < 2.0
    assert vol.spike is False
    atr, vol = _rig()
    _seed_noon(vol, range(1, 21))
    _step(atr, vol, _noon(30, v=21.0))             # exactly 2.0
    assert vol.spike is True


# ------------------------------------------------------- session VWAP


def test_session_vwap_and_bands_hand_computed():
    atr, vol = _rig()
    _step(atr, vol, _candle(D0, o=100.0, h=102.0, l=98.0, c=100.0, v=2.0))
    assert vol.session_vwap == 100.0               # tp=100
    assert vol.band_1_up == 100.0                  # sigma 0 early is valid
    _step(atr, vol, _candle(D0 + timedelta(minutes=1),
                            o=110.0, h=112.0, l=108.0, c=110.0, v=1.0))
    assert vol.session_vwap == pytest.approx(310.0 / 3)
    _step(atr, vol, _candle(D0 + timedelta(minutes=2),
                            o=100.0, h=102.0, l=98.0, c=100.0, v=1.0))
    assert vol.session_vwap == 102.5               # (200+110+100)/4
    sigma = sqrt(42100.0 / 4 - 102.5 ** 2)         # sqrt(18.75)
    assert vol.band_1_up == pytest.approx(102.5 + sigma)
    assert vol.band_2_dn == pytest.approx(102.5 - 2 * sigma)


def test_session_vwap_requires_day_head_start():
    atr, vol = _rig()
    mid = D0 + timedelta(hours=12)                 # process starts mid-day
    _step(atr, vol, _candle(mid))
    _step(atr, vol, _candle(mid + timedelta(minutes=1)))
    assert vol.session_vwap is None and vol.cum_delta is None
    next_day = D0 + timedelta(days=1)              # next 00:00: recovers
    _step(atr, vol, _candle(next_day, o=50.0, h=51.0, l=49.0, c=50.0))
    assert vol.session_vwap == 50.0


def test_cum_delta_resets_at_the_day_boundary():
    atr, vol = _rig()
    _step(atr, vol, _candle(D0, v=1.0, tbv=1.0))               # delta +1
    _step(atr, vol, _candle(D0 + timedelta(minutes=1),
                            v=1.0, tbv=1.0))                   # delta +1
    assert vol.cum_delta == 2.0
    _step(atr, vol, _candle(D0 + timedelta(days=1), v=1.0, tbv=1.0))
    assert vol.cum_delta == 1.0                    # reset, not carried (3.0)


def test_zero_volume_day_start_has_no_vwap():
    atr, vol = _rig()
    _step(atr, vol, _candle(D0, v=0.0, tbv=0.0))
    assert vol.session_vwap is None                # sum(v) = 0 guard (D19.3)


def test_session_vwap_dies_on_an_intraday_hole():
    atr, vol = _rig()
    _step(atr, vol, _candle(D0))
    _step(atr, vol, _candle(D0 + timedelta(minutes=1)))
    assert vol.session_vwap is not None
    _step(atr, vol, _candle(D0 + timedelta(minutes=3)))   # :02 missing
    assert vol.session_vwap is None                # D7: hole -> false data
    assert vol.cum_delta is None


def test_delta_and_cum_delta():
    assert candle_delta(_candle(D0, v=10.0, tbv=7.0)) == 4.0
    atr, vol = _rig()
    _step(atr, vol, _candle(D0, v=10.0, tbv=7.0))          # +4
    _step(atr, vol, _candle(D0 + timedelta(minutes=1),
                            v=6.0, tbv=1.0))               # -4
    assert vol.delta == -4.0
    assert vol.cum_delta == 0.0


# ------------------------------------------------------ anchored VWAP


def test_anchored_vwap_recompute_and_incremental_fold():
    atr, vol = _rig()
    candles = [
        _candle(D0 + timedelta(minutes=i), o=100.0 + i, h=100.0 + i,
                l=100.0 + i, c=100.0 + i, v=1.0 + i)
        for i in range(8)
    ]
    for candle in candles:
        _step(atr, vol, candle)
    assert vol.anchored_vwap is None               # no anchor yet
    anchor_ts = D0 + timedelta(minutes=5)          # a 5m window start
    vol.on_anchor(Pivot("BTCUSDT", "5m", anchor_ts, anchor_ts, "L",
                        105.0, label="HL"))
    # minutes 5,6,7: tp = close (flat candles), v = 6,7,8
    expected = (105 * 6 + 106 * 7 + 107 * 8) / (6 + 7 + 8)
    assert vol.anchored_vwap == pytest.approx(expected)
    assert vol.anchor_ts == anchor_ts
    nine = _candle(D0 + timedelta(minutes=8), o=110.0, h=110.0, l=110.0,
                   c=110.0, v=9.0)
    _step(atr, vol, nine)                          # incremental fold
    expected = (105 * 6 + 106 * 7 + 107 * 8 + 110 * 9) / 30
    assert vol.anchored_vwap == pytest.approx(expected)


def test_anchor_outside_buffer_or_after_hole_is_none():
    atr, vol = _rig()
    _step(atr, vol, _candle(D0 + timedelta(minutes=10)))
    vol.on_anchor(Pivot("BTCUSDT", "5m", D0, D0, "L", 100.0, label="HL"))
    assert vol.anchored_vwap is None               # anchor predates buffer
    vol.on_anchor(Pivot("BTCUSDT", "5m", D0 + timedelta(minutes=10),
                        D0 + timedelta(minutes=10), "L", 100.0, label="HL"))
    assert vol.anchored_vwap is not None
    _step(atr, vol, _candle(D0 + timedelta(minutes=12)))  # :11 missing
    assert vol.anchored_vwap is None               # D7: coverage broken


# ------------------------------------------------- absorption/exhaustion


def _absorption_rig():
    """Seeded spike rig: rvol = 2.5, huge ATR vs a tiny candle range."""
    atr, vol = _rig()
    _seed_noon(vol, [1.0] * 20)                    # median 1.0 at 12:00
    _step(atr, vol, _candle(D0 + timedelta(days=29, hours=12), o=100.0))
    candle = _candle(D0 + timedelta(days=30, hours=12), o=120.0, h=120.4,
                     l=120.0, c=120.2, v=2.5, tbv=2.25)   # delta +2.0
    return atr, vol, candle


def test_absorption_fires_at_a_key_level():
    atr, vol, candle = _absorption_rig()
    atr.update(candle)                             # TR = 20.4 (gap move)
    vol.update(candle)
    assert vol.rvol == 2.5 and vol.spike
    vol.classify(candle, {"PDH": 120.2}, [], {})
    event = vol.absorption
    assert event is not None
    assert (event.level, event.price, event.delta_sign) == ("PDH", 120.2, 1)


def test_absorption_each_condition_is_necessary():
    # no spike
    atr, vol, candle = _absorption_rig()
    weak = _candle(candle.ts, o=120.0, h=120.4, l=120.0, c=120.2,
                   v=1.9, tbv=1.7)                 # rvol 1.9 < 2.0
    atr.update(weak); vol.update(weak)
    vol.classify(weak, {"PDH": 120.2}, [], {})
    assert vol.absorption is None
    # delta too balanced: |delta| = 0.2v < 0.5v
    atr, vol, candle = _absorption_rig()
    balanced = _candle(candle.ts, o=120.0, h=120.4, l=120.0, c=120.2,
                       v=2.5, tbv=1.5)             # delta 0.5 < 1.25
    atr.update(balanced); vol.update(balanced)
    vol.classify(balanced, {"PDH": 120.2}, [], {})
    assert vol.absorption is None
    # range too wide: h-l = 30 >= 0.5*ATR
    atr, vol, candle = _absorption_rig()
    wide = _candle(candle.ts, o=120.0, h=140.0, l=110.0, c=120.2,
                   v=2.5, tbv=2.25)
    atr.update(wide); vol.update(wide)
    vol.classify(wide, {"PDH": 120.2}, [], {})
    assert vol.absorption is None
    # no level within the band
    atr, vol, candle = _absorption_rig()
    atr.update(candle); vol.update(candle)
    vol.classify(candle, {"PDH": 200.0}, [], {})
    assert vol.absorption is None


def test_absorption_boundary_exactness():
    # |delta| exactly 0.5*v: inclusive -> fires (dyadic floats throughout)
    atr, vol, candle = _absorption_rig()
    exact = _candle(candle.ts, o=120.0, h=120.4, l=120.0, c=120.2,
                    v=2.5, tbv=1.875)              # delta 1.25 == 0.5*2.5
    atr.update(exact); vol.update(exact)
    vol.classify(exact, {"PDH": 120.2}, [], {})
    assert vol.absorption is not None
    # range exactly 0.5*ATR: strict -> does NOT fire. ATR(1)=20 exactly
    # (prev close 100, |h-pc|=20), range 10.0 == 0.5*20
    atr, vol, _ = _absorption_rig()
    edge = _candle(D0 + timedelta(days=30, hours=12), o=118.0, h=120.0,
                   l=110.0, c=119.0, v=2.5, tbv=2.25)
    atr.update(edge); vol.update(edge)
    vol.classify(edge, {"PDH": 115.0}, [], {})
    assert vol.absorption is None                  # 10.0 < 10.0 is False
    # level gap exactly 0.3*ATR: inclusive "within" -> fires. ATR 20
    # (prev close 100.5, |h-pc|=20.0 dyadic), band 6.0, gap 126.5-120.5
    atr, vol, _ = _absorption_rig()
    near = _candle(D0 + timedelta(days=30, hours=12), o=120.375, h=120.5,
                   l=120.25, c=120.4375, v=2.5, tbv=2.25)
    prev = _candle(D0 + timedelta(days=29, hours=12), o=100.5, h=100.5,
                   l=100.5, c=100.5)
    atr2, vol2 = _rig()
    _seed_noon(vol2, [1.0] * 20)
    atr2.update(prev); vol2.update(prev)
    atr2.update(near); vol2.update(near)
    vol2.classify(near, {"PDH": 126.5}, [], {})
    assert vol2.absorption is not None             # gap 6.0 == band 6.0


def test_absorption_key_level_takes_precedence_over_pool():
    from marketscalper.engines.liquidity import LiquidityPool
    atr, vol, candle = _absorption_rig()
    atr.update(candle); vol.update(candle)
    pool = LiquidityPool("EQH", 120.3, 2, 1.0, (candle.ts,))
    vol.classify(candle, {"PDH": 120.2}, [pool], {})
    assert vol.absorption.level == "PDH"           # D19.6: levels first


def test_absorption_pool_tag_when_no_key_level_matches():
    from marketscalper.engines.liquidity import LiquidityPool
    atr, vol, candle = _absorption_rig()
    atr.update(candle); vol.update(candle)
    pool = LiquidityPool("EQH", 120.3, 2, 1.0, (candle.ts,))
    vol.classify(candle, {}, [pool], {})
    assert vol.absorption.level == "EQH"


def test_exhaustion_top_and_bottom_and_guards():
    atr, vol, _ = _absorption_rig()
    top = _candle(D0 + timedelta(days=30, hours=12), o=120.1, h=121.0,
                  l=120.0, c=120.2, v=2.5, tbv=2.0)  # upper wick 0.8/1.0
    atr.update(top); vol.update(top)
    vol.classify(top, {}, [], {"DAY_H": 121.0, "DAY_L": 90.0})
    assert vol.exhaustion == "TOP"
    atr, vol, _ = _absorption_rig()                # fresh rig per case:
    atr.update(top); vol.update(top)               # classify once (D19.8)
    vol.classify(top, {}, [], {"DAY_H": 125.0, "DAY_L": 90.0})
    assert vol.exhaustion is None                  # not at the extreme
    atr, vol, _ = _absorption_rig()
    bottom = _candle(D0 + timedelta(days=30, hours=12), o=120.9, h=121.0,
                     l=120.0, c=120.8, v=2.5, tbv=2.0)  # lower wick 0.8
    atr.update(bottom); vol.update(bottom)
    vol.classify(bottom, {}, [], {"DAY_H": 125.0, "DAY_L": 120.0})
    assert vol.exhaustion == "BOTTOM"


def test_exhaustion_wick_boundary_strict_and_doji_guard():
    # wick fraction exactly 0.6 (1.5/2.5, dyadic): strict > -> no event
    atr, vol, _ = _absorption_rig()
    edge = _candle(D0 + timedelta(days=30, hours=12), o=120.5, h=122.5,
                   l=120.0, c=121.0, v=2.5, tbv=2.0)  # wick 1.5, rng 2.5
    atr.update(edge); vol.update(edge)
    vol.classify(edge, {}, [], {"DAY_H": 122.5, "DAY_L": 90.0})
    assert vol.exhaustion is None
    # spiking zero-range doji: the rng > 0 guard must hold (no crash)
    atr, vol, _ = _absorption_rig()
    doji = _candle(D0 + timedelta(days=30, hours=12), o=120.0, h=120.0,
                   l=120.0, c=120.0, v=2.5, tbv=2.0)
    atr.update(doji); vol.update(doji)
    vol.classify(doji, {}, [], {"DAY_H": 120.0, "DAY_L": 120.0})
    assert vol.exhaustion is None


# ------------------------------------------------------------ P2.2 seams


def test_trendline_break_rvol_seam_boundaries():
    rv = [None]
    book = TrendlineBook(TrendlineDetector(IncrementalATR()),
                         IncrementalATR(), rvol_provider=lambda: rv[0])
    assert book._rvol_ok() is True                 # None -> legacy (D11)
    rv[0] = 1.5
    assert book._rvol_ok() is True                 # inclusive
    rv[0] = 1.4999
    assert book._rvol_ok() is False
    legacy = TrendlineBook(TrendlineDetector(IncrementalATR()),
                           IncrementalATR())
    assert legacy._rvol_ok() is True               # provider absent


def test_sweep_rvol_seam_boundaries_and_or_arm():
    rv = [None]
    atr = IncrementalATR()
    liq = LiquidityEngine("BTCUSDT", atr, rvol_provider=lambda: rv[0])
    assert liq._rvol_arm() is False                # None -> legacy (D12)
    rv[0] = 1.5
    assert liq._rvol_arm() is True                 # inclusive
    rv[0] = 1.4999
    assert liq._rvol_arm() is False
    assert LiquidityEngine("BTCUSDT", atr)._rvol_arm() is False
    # OR-arm end to end on _is_sweep: a 40% wick fails the wick rule;
    # rvol 1.5 rescues it (the §4.4 OR the placeholder suppressed)
    sweep_candle = _candle(D0, o=100.2, h=101.0, l=99.0, c=100.0)
    # wick = 101-100.2 = 0.8, range 2.0 -> 40% <= 60%: wick rule fails
    rv[0] = None
    assert liq._is_sweep(sweep_candle, 100.5, "HIGH") is False
    rv[0] = 1.5
    assert liq._is_sweep(sweep_candle, 100.5, "HIGH") is True


# ----------------------------------------------------------- determinism


def test_determinism_same_seeded_feed_twice():
    def run():
        atr, vol = _rig()
        _seed_noon(vol, range(1, 21))
        out = []
        for i in range(3):
            candle = _noon(30 + i, v=21.0 - i)
            _step(atr, vol, candle, key_levels={"PDH": 120.0},
                  extremes={"DAY_H": candle.h, "DAY_L": candle.l})
            out.append((vol.rvol, vol.delta, vol.spike, vol.session_vwap,
                        vol.cum_delta, vol.anchored_vwap, vol.exhaustion))
        return out
    assert run() == run()

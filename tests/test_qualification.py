"""Tests for the Trade Qualification Engine (§6; Decision D16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.engines.confluence import ConfluenceZone
from marketscalper.engines.liquidity import SweepEvent, SweepShift
from marketscalper.engines.momentum import (
    IncrementalATR,
    MomentumState,
    RegimeClassifier,
)
from marketscalper.engines.qualification import (
    QualificationEngine,
    spread_pct_of,
    verdict_of,
)
from marketscalper.engines.structure import BosEvent, ChochEvent, Pivot, TrendState
from marketscalper.providers.base import Candle

UTC = timezone.utc
T0 = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)      # 09:00 UTC = LONDON (A9)


def _candle(i, o=100.0, c=100.2, h=101.0, l=99.5, tf="1m"):
    return Candle(symbol="BTCUSDT", tf=tf, ts=T0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


class _Rig:
    """The real frozen instances in the pinned composition cadence."""

    def __init__(self, volume=None):
        self.atr = IncrementalATR()
        self.atr_5m = IncrementalATR()
        self.trend = TrendState()
        self.momentum = MomentumState(self.atr)
        self.regime = RegimeClassifier("BTCUSDT", self.atr, self.atr_5m)
        self.qual = QualificationEngine("BTCUSDT", self.atr, self.trend,
                                        self.momentum, self.regime,
                                        volume=volume)

    def step(self, candle, **kw):
        self.atr.update(candle)
        self.momentum.update(candle)
        self.regime.update()
        self.trend.update(candle)
        kw.setdefault("bos_event", None)
        kw.setdefault("choch_event", None)
        kw.setdefault("tl_events", [])
        kw.setdefault("liq_events", [])
        kw.setdefault("zones", [])
        kw.setdefault("spread_pct", None)
        kw.setdefault("clock", None)
        return self.qual.update(candle, **kw)


def _warm(rig, n=30, start=0, **candle_kw):
    result = None
    for i in range(start, start + n):
        result = rig.step(_candle(i, **candle_kw))
    return result


def _pivot(kind, price, label, minute=0):
    ts = T0 + timedelta(minutes=minute)
    return Pivot("BTCUSDT", "1m", ts, ts, kind, price, label=label)


def _sweep(minute, target="EQH"):
    return SweepEvent("BTCUSDT", T0 + timedelta(minutes=minute), minute,
                      "HIGH", target, 105.0)


def _zone(lo, hi, count):
    return ConfluenceZone("OB", "BULL", lo, hi, count,
                          ("OB",) * count, count >= 3, T0)


# ------------------------------------------------------------------ gates

def test_g1_warming_then_contiguous_pass():
    rig = _Rig()
    for i in range(29):
        result = rig.step(_candle(i))
        assert result.verdict == "NO_SIGNAL"
        assert result.score is None and result.components is None
        assert result.data_integrity == "DEGRADED"
        g1 = result.gates[0]
        assert not g1.passed and g1.detail.startswith("warming")
        assert all(r.startswith("✗") for r in result.reasons)
    result = rig.step(_candle(29))                 # 30th candle
    g1 = result.gates[0]
    assert g1.passed and g1.flagged                # clock unmeasured (D16.2)
    assert result.verdict == "BELOW_THRESHOLD"
    assert result.score == 0.0
    assert result.components == {"structure": 0.0, "liquidity": 0.0,
                                 "volume": 0.0, "momentum": 0.0}
    assert result.agreement == "0 of 10 rules aligned"
    assert result.data_integrity == "PASS"


def test_g1_gap_fails_until_it_leaves_the_window():
    rig = _Rig()
    _warm(rig, 30)                                 # minutes 0..29
    for minute in range(31, 60):                   # 30 skipped: gap inside
        result = rig.step(_candle(minute))
        assert result.verdict == "NO_SIGNAL"
        assert result.gates[0].detail == "gap in last 30 candles"
    result = rig.step(_candle(60))                 # window = 31..60
    assert result.gates[0].passed


def test_g1_clock_arm_via_the_d6_surface():
    rig = _Rig()
    _warm(rig, 30)
    result = rig.step(_candle(30), clock=(0.5, True))
    g1 = result.gates[0]
    assert g1.passed and not g1.flagged and "0.5" in g1.detail
    result = rig.step(_candle(31), clock=(3.2, False))
    assert not result.gates[0].passed and result.verdict == "NO_SIGNAL"
    result = rig.step(_candle(32), clock=(None, False))
    assert not result.gates[0].passed              # unknown offset = fail
    result = rig.step(_candle(33), clock=None)     # no sampler wired
    assert result.gates[0].passed and result.gates[0].flagged


def test_g2_spread_strict_boundary():
    rig = _Rig()
    _warm(rig, 30)
    result = rig.step(_candle(30), spread_pct=0.049)
    g2 = result.gates[1]
    assert g2.passed and not g2.flagged
    result = rig.step(_candle(31), spread_pct=0.05)    # strict <
    assert not result.gates[1].passed
    assert result.verdict == "NO_SIGNAL" and result.score is None
    assert result.data_integrity == "DEGRADED"
    result = rig.step(_candle(32), spread_pct=None)
    assert result.gates[1].passed and result.gates[1].flagged


def test_placeholder_gates_flagged_with_pinned_details():
    result = _warm(_Rig(), 30)
    g3, g4, g5, g6 = result.gates[2:6]
    assert [g.name for g in (g3, g4, g5, g6)] == ["G3", "G4", "G5", "G6"]
    assert all(g.passed and g.flagged for g in (g3, g4, g5, g6))
    assert "LONDON" in g3.detail                   # A9 via frozen session_of
    assert g4.detail == "no events calendar yet"
    assert g5.detail == "no journal yet"
    assert g6.detail == "no trade plan yet"


def test_spread_pct_of_formula():
    assert abs(spread_pct_of(99.98, 100.02) - 0.04) < 1e-9
    assert spread_pct_of(0.0, 0.0) is None
    assert spread_pct_of(-1.0, 1.0) is None


# ------------------------------------------------------------------ rubric

def test_structure_component_trend_bos_choch_windows():
    rig = _Rig()
    _warm(rig, 30)
    rig.trend.on_pivot(_pivot("H", 200.0, "HH"))   # far above the candles:
    rig.trend.on_pivot(_pivot("L", 190.0, "HL"))   # band never fires
    result = rig.step(_candle(30))
    assert result.components["structure"] == 50.0  # trend +30, no-CHOCH +20
    assert result.score == 15.0
    assert "✓ established trend BULLISH (+30 structure)" in result.reasons
    bos = BosEvent("BTCUSDT", "1m", _candle(31).ts, "UP",
                   _pivot("H", 200.0, "HH"), 201.0, True)
    result = rig.step(_candle(31), bos_event=bos)
    assert result.components["structure"] == 80.0 and result.score == 24.0
    assert result.aligned == 3
    for minute in range(32, 52):                   # BOS ages 1..20: recent
        result = rig.step(_candle(minute))
        assert result.components["structure"] == 80.0
    result = rig.step(_candle(52))                 # age 21: expired
    assert result.components["structure"] == 50.0
    choch = ChochEvent("BTCUSDT", "1m", _candle(53).ts, "DOWN",
                       _pivot("L", 190.0, "HL"), 189.0, "BULLISH")
    result = rig.step(_candle(53), choch_event=choch)
    assert result.components["structure"] == 30.0  # opposing CHOCH kills +20
    for minute in range(54, 74):                   # CHOCH ages 1..20
        result = rig.step(_candle(minute))
        assert result.components["structure"] == 30.0
    result = rig.step(_candle(74))                 # age 21: +20 restored
    assert result.components["structure"] == 50.0


def test_aligned_choch_does_not_block_after_flip():
    """Freeze-audit fix: only a CHOCH AGAINST the current trend denies the
    +20 (D16.3) — the flip's own CHOCH is aligned with the new trend."""
    rig = _Rig()
    _warm(rig, 30)
    rig.trend.on_pivot(_pivot("H", 200.0, "LH"))
    rig.trend.on_pivot(_pivot("L", 190.0, "LL"))   # trend BEARISH
    choch = ChochEvent("BTCUSDT", "1m", _candle(30).ts, "DOWN",
                       _pivot("L", 190.0, "LL"), 189.0, "BULLISH")
    result = rig.step(_candle(30), choch_event=choch)
    assert result.components["structure"] == 50.0  # DOWN aligns w/ BEARISH
    choch_up = ChochEvent("BTCUSDT", "1m", _candle(31).ts, "UP",
                          _pivot("H", 200.0, "LH"), 201.0, "BEARISH")
    result = rig.step(_candle(31), choch_event=choch_up)
    assert result.components["structure"] == 30.0  # UP opposes BEARISH


def test_windowed_events_are_not_shadowed():
    """Freeze-audit fix: D16.3 quantifies over the whole W_STRUCT window —
    an older agreeing BOS must count even after a newer non-agreeing one,
    and an older opposing CHOCH must block despite a newer aligned one."""
    rig = _Rig()
    _warm(rig, 30)
    rig.trend.on_pivot(_pivot("H", 200.0, "LH"))
    rig.trend.on_pivot(_pivot("L", 190.0, "LL"))   # trend BEARISH
    bos_down = BosEvent("BTCUSDT", "1m", _candle(30).ts, "DOWN",
                        _pivot("L", 190.0, "LL"), 189.0, True)
    rig.step(_candle(30), bos_event=bos_down)      # agrees with BEARISH
    bos_up = BosEvent("BTCUSDT", "1m", _candle(31).ts, "UP",
                      _pivot("H", 200.0, "LH"), 201.0, True)
    result = rig.step(_candle(31), bos_event=bos_up)
    assert result.components["structure"] == 80.0  # DOWN@30 still counts
    # CHOCH universal: opposing UP@32 blocks even after aligned DOWN@33
    choch_up = ChochEvent("BTCUSDT", "1m", _candle(32).ts, "UP",
                          _pivot("H", 200.0, "LH"), 201.0, "BEARISH")
    rig.step(_candle(32), choch_event=choch_up)
    choch_down = ChochEvent("BTCUSDT", "1m", _candle(33).ts, "DOWN",
                            _pivot("L", 190.0, "LL"), 189.0, "BULLISH")
    result = rig.step(_candle(33), choch_event=choch_down)
    assert result.components["structure"] == 60.0  # 30 trend + 30 BOS, no +20
    for minute in range(34, 51):                   # DOWN@30 lives thru bar 50
        result = rig.step(_candle(minute))
        assert result.components["structure"] == 60.0
    result = rig.step(_candle(51))                 # only UP@31 left: no +30
    assert result.components["structure"] == 30.0
    result = rig.step(_candle(52))                 # UP@32 still opposes
    assert result.components["structure"] == 30.0
    result = rig.step(_candle(53))                 # UP@32 pruned; DOWN@33
    assert result.components["structure"] == 50.0  # aligned: +20 restored


def test_liquidity_component_sweep_shift_confluence():
    rig = _Rig()
    _warm(rig, 30)
    result = rig.step(_candle(30), liq_events=[_sweep(30)])
    assert result.components["liquidity"] == 40.0 and result.score == 12.0
    shift = SweepShift(_sweep(30), _candle(31).ts, _candle(31).ts)
    result = rig.step(_candle(31), liq_events=[shift])
    assert result.components["liquidity"] == 70.0  # sweep recent + shift
    assert result.score == 21.0 and result.aligned == 2
    # entry-zone confluence: close 100.2 inside a count>=2 zone; band =
    # 0.3*ATR(1.5) ~ 0.45
    result = rig.step(_candle(32), zones=[_zone(99.0, 101.0, 2),
                                          _zone(200.0, 201.0, 5)])
    assert result.components["liquidity"] == 90.0  # +20, both windows live
    result = rig.step(_candle(33), zones=[_zone(99.0, 101.0, 1)])
    assert result.components["liquidity"] == 70.0  # count 1: no confluence
    result = rig.step(_candle(34), zones=[_zone(200.0, 201.0, 5)])
    assert result.components["liquidity"] == 70.0  # far-only: distance kills


def test_level_target_sweep_does_not_score_the_pool_item():
    rig = _Rig()
    _warm(rig, 30)
    result = rig.step(_candle(30), liq_events=[_sweep(30, target="PDH")])
    assert result.components["liquidity"] == 0.0   # D16.3: pool targets only


def test_sweep_recency_window_expiry():
    rig = _Rig()
    _warm(rig, 30)
    rig.step(_candle(30), liq_events=[_sweep(30)])
    for minute in range(31, 51):                   # ages 1..20
        result = rig.step(_candle(minute))
        assert result.components["liquidity"] == 40.0
    result = rig.step(_candle(51))                 # age 21
    assert result.components["liquidity"] == 0.0


def test_momentum_component_shift_and_body_dominance():
    rig = _Rig()
    # full-body rising candles: body/range = 1.0 -> dominance item on
    closes = [100.0 + 0.2 * i for i in range(31)]
    for i, close in enumerate(closes):
        result = rig.step(_candle(i, o=close - 0.2, c=close, h=close,
                                  l=close - 0.2))
    assert result.components["momentum"] == 30.0   # body only
    assert result.score == 4.5
    crash = closes[-1] - 5.0                       # velocity sign crossing
    result = rig.step(_candle(31, o=closes[-1], c=crash, h=closes[-1],
                              l=crash))
    assert result.components["momentum"] == 60.0   # +30 shift fired
    assert "✓ momentum shift (+30 momentum)" in result.reasons
    level = crash
    for minute in range(32, 37):                   # shift ages 1..5: recent
        level -= 0.2
        result = rig.step(_candle(minute, o=level + 0.2, c=level, h=level + 0.2,
                                  l=level))
        assert result.components["momentum"] == 60.0
    level -= 0.2
    result = rig.step(_candle(37, o=level + 0.2, c=level, h=level + 0.2,
                              l=level))
    assert result.components["momentum"] == 30.0   # age 6: expired


def test_momentum_regime_mapping_all_three_rows():
    # normal: constant TR -> ATR-1m == median == ATR-5m -> +20
    rig = _Rig()
    result = None
    for i in range(260):
        result = rig.step(_candle(i))
        if i % 5 == 4:                             # feed the 5m ATR (D16.5)
            rig.atr_5m.update(_candle(i, tf="5m"))
    assert rig.regime.regime == "normal"
    assert result.components["momentum"] == 20.0
    # expansion: TR jump 1.5 -> 15 lifts ATR-1m above 1.5x the lagging
    # median within a couple of bars -> +40
    for i in range(260, 266):
        result = rig.step(_candle(i, h=108.0, l=93.0))
        if rig.regime.regime == "expansion":
            break
    assert rig.regime.regime == "expansion"
    assert result.components["momentum"] == 40.0
    # coil: fresh rig, big 5m candles lift 0.6xATR-5m above ATR-1m -> +0
    rig = _Rig()
    for i in range(260):
        result = rig.step(_candle(i))
        if i % 5 == 4:
            rig.atr_5m.update(_candle(i, tf="5m"))
    for i in range(260, 270):
        rig.atr_5m.update(_candle(i, h=108.0, l=93.0, tf="5m"))
        result = rig.step(_candle(i))
        if rig.regime.regime == "coil":
            break
    assert rig.regime.regime == "coil"
    assert result.components["momentum"] == 0.0


# --------------------------------------------------------- verdict/display

def test_verdict_thresholds_inclusive():
    assert verdict_of(74.999) == "BELOW_THRESHOLD"
    assert verdict_of(75.0) == "TRADEABLE"
    assert verdict_of(84.999) == "TRADEABLE"
    assert verdict_of(85.0) == "A_PLUS"


def test_full_alignment_era_ceiling():
    """Everything evaluable aligned (regime aside) -> 61.5 and still
    BELOW_THRESHOLD: the recorded Volume-era consequence (D16.3)."""
    rig = _Rig()
    for i in range(30):                            # full-body: dominance 1.0
        rig.step(_candle(i, o=100.0, c=100.4, h=100.4, l=100.0))
    rig.trend.on_pivot(_pivot("H", 200.0, "HH"))
    rig.trend.on_pivot(_pivot("L", 190.0, "HL"))
    from marketscalper.engines.trendline import TrendlineEvent
    touch = TrendlineEvent("TOUCH", "support", 1, 5, 30, _candle(30).ts,
                           100.4)
    bos = BosEvent("BTCUSDT", "1m", _candle(30).ts, "UP",
                   _pivot("H", 200.0, "HH"), 201.0, True)
    sweep = _sweep(30)
    shift = SweepShift(sweep, _candle(30).ts, _candle(30).ts)
    result = rig.step(
        _candle(30, o=100.0, c=100.4, h=100.4, l=100.0),
        bos_event=bos, tl_events=[touch], liq_events=[sweep, shift],
        zones=[_zone(99.0, 101.0, 3)])
    assert result.components == {"structure": 100.0, "liquidity": 90.0,
                                 "volume": 0.0, "momentum": 30.0}
    assert result.score == 61.5
    assert result.verdict == "BELOW_THRESHOLD"
    assert result.agreement == "8 of 10 rules aligned"


def test_determinism_same_feed_twice():
    def run():
        rig = _Rig()
        out = [_warm(rig, 30)]
        out.append(rig.step(_candle(30), liq_events=[_sweep(30)],
                            spread_pct=0.02, clock=(0.1, True)))
        out.append(rig.step(_candle(31), zones=[_zone(99.0, 101.0, 2)]))
        return out
    assert run() == run()


# ------------------------------------------- Volume rubric (D21.3, P3.18)


class _FakeVolume:
    """Duck-typed frozen-VolumeEngine surface for the D21.3 seam."""

    def __init__(self, rvol=None, delta=0.0, session_vwap=None,
                 absorption=None, exhaustion=None):
        self.rvol = rvol
        self.delta = delta
        self.session_vwap = session_vwap
        self.absorption = absorption
        self.exhaustion = exhaustion


def _bullish(rig):
    _warm(rig, 30)
    rig.trend.on_pivot(_pivot("H", 200.0, "HH"))   # far above the candles:
    rig.trend.on_pivot(_pivot("L", 190.0, "HL"))   # band never fires


def test_volume_component_all_items_fire():
    vol = _FakeVolume(rvol=1.5, delta=3.0, session_vwap=99.0)
    rig = _Rig(volume=vol)
    _bullish(rig)                                  # close 100.2 > vwap 99
    result = rig.step(_candle(30))
    assert result.components["volume"] == 100.0
    assert result.evaluable == 14                  # m grows with the seam
    assert "✓ elevated participation rvol (+40 volume)" in result.reasons
    assert "✓ delta aligned with trend (+30 volume)" in result.reasons
    assert "✓ close on trend side of VWAP (+20 volume)" in result.reasons
    assert ("✓ no absorption/exhaustion warning (+10 volume)"
            in result.reasons)
    # structure 50 (trend + no-CHOCH) -> score = 15 + 0.25*100 = 40
    assert result.score == 40.0


def test_volume_component_boundaries_and_none_paths():
    # rvol 1.49 (below the inclusive 1.5): participation item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.49, delta=3.0, session_vwap=99.0))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 60.0
    # rvol None (unseeded, D7): same
    rig = _Rig(volume=_FakeVolume(rvol=None, delta=3.0, session_vwap=99.0))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 60.0
    # zero delta: alignment item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=0.0, session_vwap=99.0))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 70.0
    # opposing delta (negative under BULLISH): item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=-1.0, session_vwap=99.0))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 70.0
    # VWAP None (mid-day start, D7): side item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=3.0, session_vwap=None))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 80.0
    # close on the wrong side of VWAP: side item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=3.0, session_vwap=150.0))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 80.0
    # absorption present: the absence item 0
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=3.0, session_vwap=99.0,
                                  absorption=object()))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 90.0
    # exhaustion present: same
    rig = _Rig(volume=_FakeVolume(rvol=1.5, delta=3.0, session_vwap=99.0,
                                  exhaustion="TOP"))
    _bullish(rig)
    assert rig.step(_candle(30)).components["volume"] == 90.0


def test_volume_alignment_items_need_a_directional_trend():
    # no pivots at all -> trend None: delta/VWAP items are 0 even with
    # perfect values (the D16.3 "nothing to align" precedent)
    rig = _Rig(volume=_FakeVolume(rvol=2.0, delta=5.0, session_vwap=99.0))
    _warm(rig, 30)
    result = rig.step(_candle(30))
    assert result.components["volume"] == 50.0     # rvol 40 + absence 10


def test_volume_detached_stays_legacy():
    rig = _Rig()                                   # volume=None (legacy)
    _bullish(rig)
    result = rig.step(_candle(30))
    assert result.components["volume"] == 0.0
    assert result.evaluable == 10                  # D16.4 unchanged
    assert not any("volume)" in r for r in result.reasons)


def _bearish(rig):
    # a clean downtrend: LH/LL far below the candles so the band never
    # fires and the memoryless machine reads BEARISH
    for i in range(30):
        rig.step(_candle(i, o=100.0, c=99.8, h=100.1, l=99.5))
    rig.trend.on_pivot(_pivot("H", 10.0, "LH"))
    rig.trend.on_pivot(_pivot("L", 5.0, "LL"))


def test_volume_bearish_arms_mirror():
    # BEARISH: delta must be negative, close must be BELOW vwap
    vol = _FakeVolume(rvol=1.5, delta=-3.0, session_vwap=105.0)
    rig = _Rig(volume=vol)
    _bearish(rig)                                  # close 99.8 < vwap 105
    result = rig.step(_candle(30, o=100.0, c=99.8, h=100.1, l=99.5))
    assert result.components["volume"] == 100.0
    assert "✓ delta aligned with trend (+30 volume)" in result.reasons
    assert "✓ close on trend side of VWAP (+20 volume)" in result.reasons
    # positive delta under BEARISH does NOT align; close above vwap is
    # the wrong side — both items drop
    vol2 = _FakeVolume(rvol=1.5, delta=+3.0, session_vwap=95.0)
    rig2 = _Rig(volume=vol2)
    _bearish(rig2)
    result2 = rig2.step(_candle(30, o=100.0, c=99.8, h=100.1, l=99.5))
    assert result2.components["volume"] == 50.0    # rvol 40 + absence 10


def test_volume_vwap_boundary_is_strict():
    # close exactly AT vwap is not "on the trend side" (strict >/<)
    vol = _FakeVolume(rvol=1.0, delta=0.0, session_vwap=100.2)
    rig = _Rig(volume=vol)
    _bullish(rig)                                  # close == 100.2 == vwap
    result = rig.step(_candle(30))
    assert "✓ close on trend side of VWAP (+20 volume)" not in result.reasons
    assert result.components["volume"] == 10.0     # only the absence item

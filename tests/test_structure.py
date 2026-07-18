"""Tests for Structure Engine pivot detection (roadmap P1.5; §4.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketscalper import db
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import (
    BOS_DISPLACEMENT_ATR_RATIO,
    K_BY_TF,
    BosDetector,
    BosEvent,
    ChochDetector,
    ChochEvent,
    ConfirmedFlip,
    Pivot,
    PivotDetector,
    PivotLabeler,
    TrendState,
    pivot_to_row,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(h, l, i=0, tf="1m", ts=None):
    step = 5 if tf == "5m" else 1
    return Candle(symbol="BTCUSDT", tf=tf,
                  ts=ts if ts is not None else M0 + timedelta(minutes=i * step),
                  o=float(l), h=float(h), l=float(l), c=float(h),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _run(detector, candles):
    out = []
    for c in candles:
        out.append(detector.update(c))
    return out


# ------------------------------------------------------------ basic detection


def test_high_pivot_confirms_exactly_at_bar_i_plus_k():
    # k=3: highs 10,11,12,15,12,11,10 — center (index 3) strictly highest;
    # lows (h-1 each) never make the center a strict low.
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(h, h - 1, i) for i, h in enumerate([10, 11, 12, 15, 12, 11, 10])]
    out = _run(d, candles)
    assert out[:6] == [[]] * 6                     # nothing before bar i+3 closes
    assert out[6] == [Pivot("BTCUSDT", "1m", candles[3].ts, candles[6].ts, "H", 15.0)]


def test_low_pivot_mirrored():
    # k=3: lows 10,9,8,5,8,9,10 — center strictly lowest; highs (l+1) inert.
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(l + 1, l, i) for i, l in enumerate([10, 9, 8, 5, 8, 9, 10])]
    out = _run(d, candles)
    assert out[:6] == [[]] * 6
    assert out[6] == [Pivot("BTCUSDT", "1m", candles[3].ts, candles[6].ts, "L", 5.0)]


def test_equal_neighbor_blocks_pivot_strict_rule():
    # highs 10,11,15,15,12,11,10,9 — both 15s tie; no H pivot ever emitted.
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(h, h - 1, i) for i, h in enumerate([10, 11, 15, 15, 12, 11, 10, 9])]
    assert [p for step in _run(d, candles) for p in step if p.kind == "H"] == []


def test_outside_bar_emits_both_h_and_l():
    highs = [10, 11, 12, 20, 12, 11, 10]
    lows = [5, 4, 3, 1, 3, 4, 5]
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(h, l, i) for i, (h, l) in enumerate(zip(highs, lows))]
    out = _run(d, candles)
    assert out[6] == [
        Pivot("BTCUSDT", "1m", candles[3].ts, candles[6].ts, "H", 20.0),
        Pivot("BTCUSDT", "1m", candles[3].ts, candles[6].ts, "L", 1.0),
    ]


def test_k2_on_5m_window_of_five():
    # k=2: highs 10,11,14,11,10 — H at index 2, confirmed on the 5th candle.
    d = PivotDetector("BTCUSDT", "5m")
    candles = [_candle(h, h - 1, i, tf="5m") for i, h in enumerate([10, 11, 14, 11, 10])]
    out = _run(d, candles)
    assert out[:4] == [[]] * 4
    assert out[4] == [Pivot("BTCUSDT", "5m", candles[2].ts, candles[4].ts, "H", 14.0)]


def test_monotonic_staircase_never_pivots():
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(100 + i, 99 + i, i) for i in range(20)]
    assert all(step == [] for step in _run(d, candles))


def test_unsupported_timeframe_rejected():
    with pytest.raises(ValueError):
        PivotDetector("BTCUSDT", "15m")


# --------------------------------------------------------------- properties


def _varied_candles(n, gap_after=None, gap_minutes=30):
    """Deterministic oscillating sequence; optional ts gap (values unchanged)."""
    out = []
    minute = 0
    for i in range(n):
        if gap_after is not None and i == gap_after:
            minute += gap_minutes
        c = 100 + ((i * 7) % 13) - 6
        h = c + ((i * 5) % 7)
        l = c - ((i * 3) % 5)
        out.append(_candle(h, l, ts=M0 + timedelta(minutes=minute)))
        minute += 1
    return out


def test_adjacent_same_kind_pivots_are_impossible():
    d = PivotDetector("BTCUSDT", "1m")
    candles = _varied_candles(80)
    pivots = [p for step in _run(d, candles) for p in step]
    assert pivots                                          # non-vacuous
    index_of = {c.ts: i for i, c in enumerate(candles)}
    for kind in ("H", "L"):
        idxs = sorted(index_of[p.ts] for p in pivots if p.kind == kind)
        assert all(b - a >= 2 for a, b in zip(idxs, idxs[1:]))


def test_gap_in_ts_is_positionally_irrelevant():
    plain = _varied_candles(40)
    gapped = _varied_candles(40, gap_after=17)
    da, dg = PivotDetector("BTCUSDT", "1m"), PivotDetector("BTCUSDT", "1m")
    pa = [p for step in _run(da, plain) for p in step]
    pg = [p for step in _run(dg, gapped) for p in step]
    index_a = {c.ts: i for i, c in enumerate(plain)}
    index_g = {c.ts: i for i, c in enumerate(gapped)}
    assert [(index_a[p.ts], p.kind, p.price) for p in pa] == \
           [(index_g[p.ts], p.kind, p.price) for p in pg]


def test_prefix_property_no_repaint():
    candles = _varied_candles(40)
    d_full = PivotDetector("BTCUSDT", "1m")
    full = [p for step in _run(d_full, candles) for p in step]
    for n in range(len(candles) + 1):
        d = PivotDetector("BTCUSDT", "1m")
        prefix = [p for step in _run(d, candles[:n]) for p in step]
        assert prefix == full[:len(prefix)]
        assert all(p.confirmed_ts <= candles[n - 1].ts for p in prefix) if n else True


def test_determinism_same_stream_twice():
    candles = _varied_candles(60)
    r1 = _run(PivotDetector("BTCUSDT", "1m"), candles)
    r2 = _run(PivotDetector("BTCUSDT", "1m"), candles)
    assert r1 == r2


# ------------------------------------------------------ PivotLabeler (P1.6)


def _pivot(kind, price, i=0):
    ts = M0 + timedelta(minutes=i)
    return Pivot("BTCUSDT", "1m", ts, ts + timedelta(minutes=3), kind, float(price))


def test_h_chain_seed_hh_lh_and_state_advances():
    lab = PivotLabeler()
    labels = [lab.label(_pivot("H", p, i)).label
              for i, p in enumerate([10, 15, 12, 13])]
    # 13 > 12 (the LAST H, not the max) proves the chain advanced on LH too
    assert labels == [None, "HH", "LH", "HH"]


def test_l_chain_seed_hl_ll_mirrored():
    lab = PivotLabeler()
    labels = [lab.label(_pivot("L", p, i)).label
              for i, p in enumerate([10, 12, 9, 11])]
    assert labels == [None, "HL", "LL", "HL"]


def test_equality_labels_lh_ll_strict_rule():
    lab = PivotLabeler()
    assert lab.label(_pivot("H", 10, 0)).label is None
    assert lab.label(_pivot("H", 10, 1)).label == "LH"
    assert lab.label(_pivot("L", 5, 2)).label is None
    assert lab.label(_pivot("L", 5, 3)).label == "LL"


def test_h_and_l_chains_are_independent():
    lab = PivotLabeler()
    assert lab.label(_pivot("H", 10, 0)).label is None    # seeds H chain
    assert lab.label(_pivot("L", 50, 1)).label is None    # seeds L chain
    # 40 < last L (50) but > last H (10): kind isolation -> HH
    assert lab.label(_pivot("H", 40, 2)).label == "HH"
    # 20 > last H (40)? irrelevant — vs last L (50) it is lower -> LL
    assert lab.label(_pivot("L", 20, 3)).label == "LL"


def test_outside_bar_pair_labels_both_chains():
    lab = PivotLabeler()
    lab.label(_pivot("H", 20, 0))
    lab.label(_pivot("L", 1, 1))
    assert lab.label(_pivot("H", 25, 2)).label == "HH"    # same-bar pair,
    assert lab.label(_pivot("L", 0.5, 2)).label == "LL"   # H first (P1.5)


def test_labelers_are_independent():
    a, b = PivotLabeler(), PivotLabeler()
    a.label(_pivot("H", 10, 0))
    assert a.label(_pivot("H", 15, 1)).label == "HH"
    assert b.label(_pivot("H", 15, 1)).label is None      # b never seeded


def test_input_pivot_is_never_mutated():
    lab = PivotLabeler()
    lab.label(_pivot("H", 10, 0))
    original = _pivot("H", 15, 1)
    labeled = lab.label(original)
    assert original.label is None and labeled.label == "HH"
    assert labeled is not original


def test_detector_to_labeler_integration_end_to_end():
    # highs 10,11,12,15,12,11,10,11,12,14,12,11,10 (lows = h-1), k=3:
    #   H pivot 15 at idx3 (confirmed update 7)   -> seed, label None
    #   L pivot  9 at idx6 (confirmed update 10)  -> seed, label None
    #   H pivot 14 at idx9 (confirmed update 13)  -> 14 < 15 -> LH
    d, lab = PivotDetector("BTCUSDT", "1m"), PivotLabeler()
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 12, 14, 12, 11, 10]
    labeled = []
    for i, h in enumerate(highs):
        for p in d.update(_candle(h, h - 1, i)):
            labeled.append(lab.label(p))
    assert [(p.kind, p.price, p.label) for p in labeled] == [
        ("H", 15.0, None), ("L", 9.0, None), ("H", 14.0, "LH")]


def test_labeler_determinism_same_stream_twice():
    pivots = [_pivot(k, p, i) for i, (k, p) in enumerate(
        [("H", 10), ("L", 5), ("H", 12), ("L", 4), ("H", 11), ("L", 6)])]
    lab1, lab2 = PivotLabeler(), PivotLabeler()
    out1 = [lab1.label(p) for p in pivots]
    out2 = [lab2.label(p) for p in pivots]
    assert out1 == out2


async def test_labeled_pivot_persistence_round_trip(db_conn):
    lab = PivotLabeler()
    lab.label(_pivot("H", 10, 0))
    labeled = lab.label(_pivot("H", 15, 1))
    await db.insert_pivot(db_conn, **pivot_to_row(labeled))
    rows = await db.select_pivots(db_conn, "BTCUSDT", "1m")
    assert [r["label"] for r in rows] == ["HH"]


# -------------------------------------------------------- TrendState (P1.8)
# Cadence contract (pinned): per closed candle — detector -> labeler ->
# on_pivot(each labeled pivot) -> update(candle). The just-closed candle is
# part of the last-20 window at its own evaluation.


def _lp(kind, price, label, i=0, tf="1m"):
    """A labeled pivot as the P1.6 labeler would emit it."""
    ts = M0 + timedelta(minutes=i)
    return Pivot("BTCUSDT", tf, ts, ts, kind, float(price), label)


def _body_candle(o, c, i=0, tf="1m"):
    """Candle with an exact body [min(o,c), max(o,c)]; wicks poke 0.5
    beyond both body ends — proving wicks are irrelevant to the band."""
    h, l = max(o, c) + 0.5, min(o, c) - 0.5
    step = 5 if tf == "5m" else 1
    return Candle(symbol="BTCUSDT", tf=tf, ts=M0 + timedelta(minutes=i * step),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _bullish_machine():
    """TrendState with labels HH/HL and band [100, 110]."""
    tm = TrendState()
    tm.on_pivot(_lp("H", 110, "HH"))
    tm.on_pivot(_lp("L", 100, "HL"))
    return tm


def test_trend_warmup_ladder():
    tm = TrendState()
    assert tm.update(_body_candle(105, 106, 0)) is None      # no pivots
    tm.on_pivot(_lp("H", 110, None, 1))                      # H seed
    assert tm.update(_body_candle(105, 106, 1)) is None
    tm.on_pivot(_lp("L", 100, None, 2))                      # L seed
    assert tm.update(_body_candle(105, 106, 2)) is None
    tm.on_pivot(_lp("H", 112, "HH", 3))                      # L still seed
    assert tm.update(_body_candle(105, 106, 3)) is None
    tm.on_pivot(_lp("L", 101, "HL", 4))                      # both labeled
    assert tm.update(_body_candle(105, 106, 4)) == "BULLISH"


def test_trend_bullish_with_bodies_outside_band():
    tm = _bullish_machine()
    for i in range(20):
        state = tm.update(_body_candle(120, 121, i))         # outside [100,110]
    assert state == "BULLISH"


def test_trend_bearish_mirrored():
    tm = TrendState()
    tm.on_pivot(_lp("H", 110, "LH"))
    tm.on_pivot(_lp("L", 100, "LL"))
    for i in range(20):
        state = tm.update(_body_candle(90, 89, i))           # outside band
    assert state == "BEARISH"


def test_trend_mixed_labels_are_range():
    for h_label, l_label in (("HH", "LL"), ("LH", "HL")):
        tm = TrendState()
        tm.on_pivot(_lp("H", 110, h_label))
        tm.on_pivot(_lp("L", 100, l_label))
        assert tm.update(_body_candle(120, 121, 0)) == "RANGE"


def test_trend_band_overrides_bullish_labels():
    tm = _bullish_machine()
    states = [tm.update(_body_candle(104, 105, i)) for i in range(20)]
    assert states[18] == "BULLISH"                # band asleep at 19 candles
    assert states[19] == "RANGE"                  # 20/20 inside -> override


def test_trend_band_boundary_12_vs_11():
    tm = _bullish_machine()                       # 12 inside + 8 outside
    for i in range(12):
        tm.update(_body_candle(104, 105, i))
    for i in range(8):
        state = tm.update(_body_candle(120, 121, 12 + i))
    assert state == "RANGE"
    tm = _bullish_machine()                       # 11 inside + 9 outside
    for i in range(11):
        tm.update(_body_candle(104, 105, i))
    for i in range(9):
        state = tm.update(_body_candle(120, 121, 11 + i))
    assert state == "BULLISH"


def test_trend_band_edges_inclusive():
    tm = _bullish_machine()                       # 11 clearly inside...
    for i in range(11):
        tm.update(_body_candle(104, 105, i))
    tm.update(_body_candle(100, 110, 11))         # ...12th spans band exactly
    for i in range(8):
        state = tm.update(_body_candle(120, 121, 12 + i))
    assert state == "RANGE"


def test_trend_doji_on_edge_counts_inside():
    tm = _bullish_machine()
    for i in range(11):
        tm.update(_body_candle(104, 105, i))
    tm.update(_body_candle(110, 110, 11))         # doji exactly on band_hi
    for i in range(8):
        state = tm.update(_body_candle(120, 121, 12 + i))
    assert state == "RANGE"


def test_trend_band_normalized_when_pivots_crossed():
    tm = TrendState()
    tm.on_pivot(_lp("H", 100, "LH"))              # H price BELOW L price
    tm.on_pivot(_lp("L", 110, "LL"))
    for i in range(20):
        state = tm.update(_body_candle(104, 105, i))   # inside [100,110]
    assert state == "RANGE"                       # min/max normalization


def test_trend_memoryless_flips_without_stickiness():
    tm = _bullish_machine()
    for i in range(20):
        tm.update(_body_candle(104, 105, i))
    assert tm.state == "RANGE"
    for i in range(9):                            # 9 outside -> 11/20 inside
        tm.update(_body_candle(120, 121, 20 + i))
    assert tm.state == "BULLISH"                  # flips straight back


def test_trend_timeframe_generic_on_5m():
    tm = TrendState()
    tm.on_pivot(_lp("H", 110, "HH", tf="5m"))
    tm.on_pivot(_lp("L", 100, "HL", tf="5m"))
    states = [tm.update(_body_candle(104, 105, i, tf="5m")) for i in range(20)]
    assert states[18] == "BULLISH" and states[19] == "RANGE"


def test_trend_determinism_same_feed_twice():
    def run():
        tm = _bullish_machine()
        out = [tm.update(_body_candle(104 + (i % 3), 105, i)) for i in range(25)]
        tm.on_pivot(_lp("L", 108, "LL", 30))
        out.append(tm.update(_body_candle(120, 121, 30)))
        return out
    assert run() == run()


def test_trend_end_to_end_detector_labeler_machine():
    # highs 10,11,12,15,12,11,10,11,12,14,12,11,10,9,8,9,10,11,12 (lows h-1):
    #   H 15 @ idx3 (upd 7, seed) · L 9 @ idx6 (upd 10, seed)
    #   H 14 @ idx9 (upd 13, LH)  · L 7 @ idx14 (upd 18, LL)
    # -> None until update 18, then BEARISH (band asleep: < 20 candles).
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 12, 14, 12, 11, 10, 9, 8, 9, 10, 11, 12]
    d, lab, tm = PivotDetector("BTCUSDT", "1m"), PivotLabeler(), TrendState()
    states = []
    for i, h in enumerate(highs):
        candle = _candle(h, h - 1, i)
        for p in d.update(candle):
            tm.on_pivot(lab.label(p))
        states.append(tm.update(candle))
    assert states[:17] == [None] * 17
    assert states[17] == "BEARISH" and states[18] == "BEARISH"


# ------------------------------------------------------- BosDetector (P1.9)
# Cadence (pinned): ATR update -> pivots -> on_pivot fan-out ->
# trend.update(candle) -> bos.update(candle).


def _bos_rig(h_label="HH", l_label="HL", h=110.0, l=100.0, atr_period=2):
    tm, atr = TrendState(), IncrementalATR(period=atr_period)
    bos = BosDetector(tm, atr)
    for p in (_lp("H", h, h_label), _lp("L", l, l_label, 1)):
        tm.on_pivot(p)
        bos.on_pivot(p)
    return tm, atr, bos


def _bos_step(tm, atr, bos, candle):
    atr.update(candle)
    tm.update(candle)
    return bos.update(candle)


def _warm(tm, atr, bos):
    """Three small candles: sets prev_close=105 and seeds ATR(2) = 2.0."""
    _bos_step(tm, atr, bos, _body_candle(105, 105, 0))
    _bos_step(tm, atr, bos, _body_candle(105, 106, 1))   # TR 2.0
    _bos_step(tm, atr, bos, _body_candle(106, 105, 2))   # TR 2.0 -> ATR 2.0
    assert atr.value == pytest.approx(2.0)


def test_bos_bullish_fires_with_all_fields():
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    breaker = _body_candle(104, 111, 3)                  # close 111 > H 110
    event = _bos_step(tm, atr, bos, breaker)
    assert isinstance(event, BosEvent)
    assert (event.symbol, event.tf, event.ts) == ("BTCUSDT", "1m", breaker.ts)
    assert event.direction == "UP" and event.close == 111.0
    assert event.broken_pivot.price == 110.0


def test_bos_strict_equality_and_below_do_not_fire():
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    assert _bos_step(tm, atr, bos, _body_candle(105, 110, 3)) is None  # == pivot
    assert _bos_step(tm, atr, bos, _body_candle(105, 109, 4)) is None  # below


def test_bos_bearish_mirrored():
    tm, atr, bos = _bos_rig(h_label="LH", l_label="LL")
    _warm(tm, atr, bos)
    event = _bos_step(tm, atr, bos, _body_candle(105, 99, 3))   # close < L 100
    assert event.direction == "DOWN" and event.broken_pivot.price == 100.0


def test_bos_once_per_pivot_then_rearms_on_new_pivot():
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    assert _bos_step(tm, atr, bos, _body_candle(104, 111, 3)) is not None
    assert _bos_step(tm, atr, bos, _body_candle(111, 112, 4)) is None  # latched
    new_h = _lp("H", 115, "HH", 10)                      # new confirmed swing
    tm.on_pivot(new_h)
    bos.on_pivot(new_h)
    assert _bos_step(tm, atr, bos, _body_candle(112, 114, 5)) is None  # 114 < 115
    event = _bos_step(tm, atr, bos, _body_candle(114, 116, 6))
    assert event is not None and event.broken_pivot.price == 115.0


def test_bos_never_fires_outside_trend():
    # RANGE via mixed labels: same break geometry -> nothing
    tm, atr, bos = _bos_rig(h_label="HH", l_label="LL")
    _warm(tm, atr, bos)
    assert tm.state == "RANGE"
    assert _bos_step(tm, atr, bos, _body_candle(104, 111, 3)) is None
    # warm-up unknown: seeds only -> nothing
    tm, atr, bos = _bos_rig(h_label=None, l_label=None)
    assert _bos_step(tm, atr, bos, _body_candle(104, 111, 3)) is None
    # against-trend break: BEARISH but close ABOVE the high -> CHOCH turf
    tm, atr, bos = _bos_rig(h_label="LH", l_label="LL")
    _warm(tm, atr, bos)
    assert _bos_step(tm, atr, bos, _body_candle(104, 111, 3)) is None


def test_bos_displacement_true_weak_and_equality():
    # displacement True: warm ATR 2.0; breaker o=104 c=111 h=111.5 l=103.5:
    # TR = max(8, 6.5, 1.5) = 8 -> ATR = (2+8)/2 = 5, thr 6.0, body 7 > 6
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    event = _bos_step(tm, atr, bos, _body_candle(104, 111, 3))
    assert atr.value == pytest.approx(5.0) and event.displacement is True
    # weak: breaker o=106 c=111: TR = max(6, 6.5, 0.5) = 6.5 -> ATR 4.25,
    # thr 5.1, body 5 <= 5.1
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    event = _bos_step(tm, atr, bos, _body_candle(106, 111, 3))
    assert atr.value == pytest.approx(4.25) and event.displacement is False
    # exact equality = weak: custom candle body 6, TR 8 -> ATR 5, thr 6.0
    assert BOS_DISPLACEMENT_ATR_RATIO * 5.0 == 6.0      # float precondition
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    breaker = Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=3),
                     o=105.0, h=112.0, l=104.0, c=111.0,
                     v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)
    event = _bos_step(tm, atr, bos, breaker)
    assert atr.value == pytest.approx(5.0) and event.displacement is False


def test_bos_displacement_none_while_atr_unwarm():
    tm, atr, bos = _bos_rig(atr_period=14)
    _bos_step(tm, atr, bos, _body_candle(105, 105, 0))
    event = _bos_step(tm, atr, bos, _body_candle(104, 111, 1))
    assert event is not None and event.displacement is None


def test_bos_displacement_uses_body_not_range():
    # monstrous wicks, tiny body: ATR explodes but body is 0.5 -> weak
    tm, atr, bos = _bos_rig()
    _warm(tm, atr, bos)
    breaker = Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=3),
                     o=110.5, h=140.0, l=90.0, c=111.0,
                     v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)
    event = _bos_step(tm, atr, bos, breaker)
    assert event is not None and event.displacement is False


def test_bos_timeframe_generic_on_5m():
    tm, atr = TrendState(), IncrementalATR(period=2)
    bos = BosDetector(tm, atr)
    for p in (_lp("H", 110, "HH", tf="5m"), _lp("L", 100, "HL", 1, tf="5m")):
        tm.on_pivot(p)
        bos.on_pivot(p)
    for o, c, i in ((105, 105, 0), (105, 106, 1), (106, 105, 2)):
        _bos_step(tm, atr, bos, _body_candle(o, c, i, tf="5m"))
    event = _bos_step(tm, atr, bos, _body_candle(104, 111, 3, tf="5m"))
    assert event is not None and event.tf == "5m" and event.direction == "UP"


def test_bos_determinism_same_feed_twice():
    def run():
        tm, atr, bos = _bos_rig()
        _warm(tm, atr, bos)
        out = [_bos_step(tm, atr, bos, _body_candle(104 + i, 108 + i, 3 + i))
               for i in range(6)]
        new_h = _lp("H", 115, "HH", 20)
        tm.on_pivot(new_h)
        bos.on_pivot(new_h)
        out.append(_bos_step(tm, atr, bos, _body_candle(114, 116, 20)))
        return out
    assert run() == run()


def test_bos_end_to_end_full_chain():
    # highs 10,11,12,15,12,11,10,11,14,11,10,9,8,9,10,11 (lows h-1), k=3:
    #   H 15 @3 (upd 7, seed) · L 9 @6 (upd 10, seed)
    #   H 14 @8 (upd 12, LH)  · L 7 @12 (upd 16, LL) -> BEARISH at upd 16
    # break candle idx16 close 6.5 < L 7 -> BOS DOWN (17 candles: band asleep)
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 14, 11, 10, 9, 8, 9, 10, 11]
    d, lab = PivotDetector("BTCUSDT", "1m"), PivotLabeler()
    tm, atr = TrendState(), IncrementalATR()
    bos = BosDetector(tm, atr)
    events = []
    for i, h in enumerate(highs + [6.5]):
        candle = _candle(h, h - 1, i)
        atr.update(candle)
        for p in d.update(candle):
            labeled = lab.label(p)
            tm.on_pivot(labeled)
            bos.on_pivot(labeled)
        tm.update(candle)
        events.append(bos.update(candle))
    assert events[:16] == [None] * 16
    event = events[16]
    assert event is not None and event.direction == "DOWN"
    assert event.broken_pivot.price == 7.0 and event.close == 6.5
    assert event.displacement is not None                # ATR(14) warm by now


# ----------------------------------------------------- ChochDetector (P1.10)
# Cadence (pinned): ... -> trend.update -> bos.update -> on_bos(event if
# any) -> choch.update(candle) — a CHOCH can never confirm on its own bar.


def _choch_rig(h_label="HH", l_label="HL", h=110.0, l=100.0):
    tm = TrendState()
    ch = ChochDetector(tm)
    for p in (_lp("H", h, h_label), _lp("L", l, l_label, 1)):
        tm.on_pivot(p)
        ch.on_pivot(p)
    return tm, ch


def _choch_step(tm, ch, candle):
    tm.update(candle)
    return ch.update(candle)


def _bos_ev(direction, i=50):
    kind = "H" if direction == "UP" else "L"
    return BosEvent("BTCUSDT", "1m", M0 + timedelta(minutes=i), direction,
                    _lp(kind, 100, "HH"), 100.0, False)


def test_choch_fires_in_bullish_with_all_fields():
    tm, ch = _choch_rig()
    assert _choch_step(tm, ch, _body_candle(101, 100.5, 0)) is None  # above L
    assert _choch_step(tm, ch, _body_candle(101, 100.0, 1)) is None  # equality
    breaker = _body_candle(101, 99.0, 2)                             # close 99 < 100
    event = _choch_step(tm, ch, breaker)
    assert isinstance(event, ChochEvent)
    assert (event.symbol, event.tf, event.ts) == ("BTCUSDT", "1m", breaker.ts)
    assert event.direction == "DOWN" and event.close == 99.0
    assert event.broken_pivot.price == 100.0 and event.prior_trend == "BULLISH"
    assert ch.pending_flip == "DOWN"


def test_choch_bearish_mirrored():
    tm, ch = _choch_rig(h_label="LH", l_label="LL")
    event = _choch_step(tm, ch, _body_candle(109, 111, 0))           # close 111 > 110
    assert event.direction == "UP" and event.broken_pivot.price == 110.0
    assert event.prior_trend == "BEARISH" and ch.pending_flip == "UP"


def test_choch_never_fires_without_trend():
    tm, ch = _choch_rig(h_label="HH", l_label="LL")                  # RANGE (mixed)
    assert _choch_step(tm, ch, _body_candle(101, 99, 0)) is None
    tm, ch = _choch_rig(h_label=None, l_label=None)                  # unknown
    assert _choch_step(tm, ch, _body_candle(101, 99, 0)) is None


def test_choch_ignores_with_trend_break():
    tm, ch = _choch_rig()                                            # BULLISH
    assert _choch_step(tm, ch, _body_candle(104, 111, 0)) is None    # that's BOS


def test_choch_once_per_pivot_then_rearms_on_new_pivot():
    tm, ch = _choch_rig()
    assert _choch_step(tm, ch, _body_candle(101, 99, 0)) is not None
    assert _choch_step(tm, ch, _body_candle(99, 98, 1)) is None      # latched
    new_l = _lp("L", 105, "HL", 10)          # higher low keeps BULLISH labels
    tm.on_pivot(new_l)
    ch.on_pivot(new_l)
    event = _choch_step(tm, ch, _body_candle(105, 104, 2))           # 104 < 105
    assert event is not None and event.broken_pivot.price == 105.0


def test_flip_confirmed_by_same_direction_bos():
    tm, ch = _choch_rig()
    choch = _choch_step(tm, ch, _body_candle(101, 99, 0))
    bos = _bos_ev("DOWN")
    flip = ch.on_bos(bos)
    assert isinstance(flip, ConfirmedFlip)
    assert flip.direction == "DOWN" and flip.ts == bos.ts
    assert flip.choch is choch and flip.bos is bos
    assert ch.pending_flip is None


def test_flip_cancelled_by_opposite_bos_and_stays_cancelled():
    tm, ch = _choch_rig()
    _choch_step(tm, ch, _body_candle(101, 99, 0))
    assert ch.on_bos(_bos_ev("UP")) is None                # old trend resumed
    assert ch.pending_flip is None
    assert ch.on_bos(_bos_ev("DOWN", 51)) is None          # stale warning gone


def test_choch_alone_never_flips():
    tm, ch = _choch_rig()
    _choch_step(tm, ch, _body_candle(101, 99, 0))
    for i in range(5):                                     # candles, no BOS
        _choch_step(tm, ch, _body_candle(99, 98.5, 1 + i))
    assert ch.pending_flip == "DOWN"                       # still just pending


def test_newer_choch_replaces_pending():
    tm, ch = _choch_rig()
    _choch_step(tm, ch, _body_candle(101, 99, 0))
    new_l = _lp("L", 105, "HL", 10)
    tm.on_pivot(new_l)
    ch.on_pivot(new_l)
    second = _choch_step(tm, ch, _body_candle(105, 104, 1))
    flip = ch.on_bos(_bos_ev("DOWN"))
    assert flip.choch is second                            # latest warning wins
    assert flip.choch.broken_pivot.price == 105.0


def test_same_bar_bos_cannot_confirm_that_bars_choch():
    tm, ch = _choch_rig()
    assert ch.on_bos(_bos_ev("DOWN")) is None              # BOS first: no pending
    choch = _choch_step(tm, ch, _body_candle(101, 99, 0))  # CHOCH after
    assert choch is not None and ch.pending_flip == "DOWN" # unconfirmed
    assert ch.on_bos(_bos_ev("DOWN", 51)) is not None      # next bar confirms


def test_choch_timeframe_generic_on_5m():
    tm = TrendState()
    ch = ChochDetector(tm)
    for p in (_lp("H", 110, "LH", tf="5m"), _lp("L", 100, "LL", 1, tf="5m")):
        tm.on_pivot(p)
        ch.on_pivot(p)
    event = _choch_step(tm, ch, _body_candle(109, 111, 0, tf="5m"))
    assert event is not None and event.tf == "5m" and event.direction == "UP"


def test_choch_determinism_same_feed_twice():
    def run():
        tm, ch = _choch_rig()
        out = [_choch_step(tm, ch, _body_candle(101, 100.5 - 0.5 * i, i))
               for i in range(4)]
        out.append(ch.on_bos(_bos_ev("DOWN")))
        return out
    assert run() == run()


def test_choch_end_to_end_full_chain():
    # P1.9 end-to-end scenario + one more candle closing above last H (14)
    # while BEARISH -> a real-chain CHOCH UP with the flip pending.
    highs = [10, 11, 12, 15, 12, 11, 10, 11, 14, 11, 10, 9, 8, 9, 10, 11]
    d, lab = PivotDetector("BTCUSDT", "1m"), PivotLabeler()
    tm, atr = TrendState(), IncrementalATR()
    bos, ch = BosDetector(tm, atr), ChochDetector(tm)
    chochs = []
    for i, h in enumerate(highs + [6.5, 15]):
        candle = _candle(h, h - 1, i)
        atr.update(candle)
        for p in d.update(candle):
            labeled = lab.label(p)
            tm.on_pivot(labeled)
            bos.on_pivot(labeled)
            ch.on_pivot(labeled)
        tm.update(candle)
        bos_event = bos.update(candle)
        if bos_event is not None:
            ch.on_bos(bos_event)
        chochs.append(ch.update(candle))
    assert chochs[:17] == [None] * 17          # incl. the BOS bar at idx16
    event = chochs[17]
    assert event is not None and event.direction == "UP"
    assert event.broken_pivot.price == 14.0 and event.prior_trend == "BEARISH"
    assert ch.pending_flip == "UP"


# -------------------------------------------------------------- persistence


async def test_pivot_persistence_round_trip(db_conn):
    d = PivotDetector("BTCUSDT", "1m")
    candles = [_candle(h, h - 1, i) for i, h in enumerate([10, 11, 12, 15, 12, 11, 10])]
    [pivot] = [p for step in _run(d, candles) for p in step]
    pivot_id = await db.insert_pivot(db_conn, **pivot_to_row(pivot))
    assert isinstance(pivot_id, int)
    rows = await db.select_pivots(db_conn, "BTCUSDT", "1m")
    assert len(rows) == 1
    r = rows[0]
    assert (r["symbol"], r["tf"], r["kind"]) == ("BTCUSDT", "1m", "H")
    assert r["ts"] == pivot.ts and r["confirmed_ts"] == pivot.confirmed_ts
    assert float(r["price"]) == pivot.price
    assert r["label"] is None

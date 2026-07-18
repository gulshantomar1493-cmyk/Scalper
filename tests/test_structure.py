"""Tests for Structure Engine pivot detection (roadmap P1.5; §4.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from marketscalper import db
from marketscalper.engines.structure import (
    K_BY_TF,
    Pivot,
    PivotDetector,
    PivotLabeler,
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

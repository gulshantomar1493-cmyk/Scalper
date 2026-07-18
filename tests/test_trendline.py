"""Tests for the Trendline Detector (roadmap P1.13; §4.3 Steps 1-2 + score
formula; rules per Decision D11).

Geometry note: the main dataset places L-pivot anchors 100@3, 110@9, 121@15
on ONE log-space line (1.1 ratio per 6 bars) with an engineered mid touch
at bars 6 and 12 — so the three anchor pairs share a line and their touch
sets differ only by starting anchor: (3,15) and (3,9) see 5 touches
{3,6,9,12,15}, (9,15) sees 3 {9,12,15}. All distances/tolerances were
hand-verified against tol = 0.15*ATR/close with a period-1 ATR.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import log

import pytest

from marketscalper import db
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import Pivot
from marketscalper.engines.trendline import (
    ARCHIVE_AGE_BARS,
    CAP_PER_SIDE,
    MIN_TOUCHES,
    N_PIVOTS,
    TrendlineBook,
    TrendlineCandidate,
    TrendlineDetector,
    line_to_row,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _ts(i):
    return M0 + timedelta(minutes=i)


def _candle(i, l, h=None, c=None, o=None):
    h = l + 4 if h is None else h
    c = l + 3 if c is None else c
    o = l + 1 if o is None else o
    return Candle(symbol="BTCUSDT", tf="1m", ts=_ts(i), o=float(o),
                  h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _res_candle(i, h):
    """Candle shaped for resistance tests: body/close below the high."""
    return _candle(i, l=h - 4, h=h, c=h - 3, o=h - 1)


def _pivot(kind, price, i):
    return Pivot("BTCUSDT", "1m", _ts(i), _ts(i + 3), kind, float(price))


def _rig():
    atr = IncrementalATR(period=1)
    return atr, TrendlineDetector(atr)


def _feed(atr, det, candles, pivots_after=()):
    """Feed candles in cadence (ATR first); inject (bar, pivot) after bars."""
    inject = dict(pivots_after)
    for idx, c in enumerate(candles):
        atr.update(c)
        det.update(c)
        if idx in inject:
            for p in inject[idx]:
                det.on_pivot(p)


# Main support dataset (see module docstring). lows per bar 0..18:
_LOWS = [104, 103, 102, 100, 106, 107, 105, 108, 109, 110,
         116, 117, 115, 116, 120.5, 121, 122, 123, 124]


def _support_scenario(lows=_LOWS, pivots=None):
    atr, det = _rig()
    candles = [_candle(i, l) for i, l in enumerate(lows)]
    if pivots is None:
        pivots = {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)],
                  18: [_pivot("L", 121, 15)]}
    _feed(atr, det, candles, pivots.items())
    return det


def test_candidate_generation_touches_scores_and_ordering():
    cands = _support_scenario().candidates()
    keyed = {(c.a_index, c.b_index): c for c in cands}
    assert [(c.a_index, c.b_index) for c in cands] == [(3, 15), (3, 9), (9, 15)]
    assert all(c.side == "support" for c in cands)
    assert keyed[(3, 15)].touches == 5 and keyed[(3, 9)].touches == 5
    assert keyed[(9, 15)].touches == 3
    assert all(c.last_touch_index == 15 for c in cands)      # age = 18-15 = 3
    assert keyed[(3, 15)].score == pytest.approx(10.0 + 12 / 20 - 0.03)
    assert keyed[(3, 9)].score == pytest.approx(10.0 + 6 / 20 - 0.03)
    assert keyed[(9, 15)].score == pytest.approx(6.0 + 6 / 20 - 0.03)


def test_log_space_slope_and_intercept():
    cands = _support_scenario().candidates()
    keyed = {(c.a_index, c.b_index): c for c in cands}
    assert keyed[(3, 9)].slope == pytest.approx(log(1.1) / 6, rel=1e-12)
    assert keyed[(3, 15)].slope == pytest.approx(log(1.21) / 12, rel=1e-12)
    assert keyed[(3, 9)].intercept == pytest.approx(log(100.0), rel=1e-12)
    # collinearity in LOG space (a 1.1 price ratio per 6 bars) is exactly
    # what a linear-price fit would NOT produce — proves the space used
    assert keyed[(3, 9)].slope == pytest.approx(keyed[(3, 15)].slope, rel=1e-12)


def test_resistance_detection_mirrored():
    highs = [126, 127, 128, 130, 122, 121, 125, 119, 118, 120, 116, 115, 114]
    atr, det = _rig()
    _feed(atr, det, [_res_candle(i, h) for i, h in enumerate(highs)],
          {6: [_pivot("H", 130, 3)], 12: [_pivot("H", 120, 9)]}.items())
    [cand] = det.candidates()
    assert cand.side == "resistance"
    assert (cand.a_index, cand.b_index) == (3, 9)
    assert cand.slope == pytest.approx(log(120 / 130) / 6, rel=1e-12)
    assert cand.slope < 0
    assert cand.touches == 3                       # anchors + the bar-6 high
    assert cand.score == pytest.approx(6.0 + 6 / 20 - 0.03)


def test_direction_filter():
    flat = [111] * 13
    # descending support pair -> slope < 0 -> excluded
    atr, det = _rig()
    _feed(atr, det, [_candle(i, l) for i, l in enumerate(flat)],
          {6: [_pivot("L", 110, 3)], 12: [_pivot("L", 100, 9)]}.items())
    assert det.candidates() == []
    # equal anchors -> slope 0 -> excluded (EQ is P2 territory)
    atr, det = _rig()
    _feed(atr, det, [_candle(i, l) for i, l in enumerate(flat)],
          {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 100, 9)]}.items())
    assert det.candidates() == []
    # ascending resistance pair -> slope > 0 -> excluded
    atr, det = _rig()
    _feed(atr, det, [_res_candle(i, 130) for i in range(13)],
          {6: [_pivot("H", 120, 3)], 12: [_pivot("H", 130, 9)]}.items())
    assert det.candidates() == []


def test_close_cutting_anchor_segment_invalidates():
    lows = list(_LOWS[:13])
    atr, det = _rig()
    candles = [_candle(i, l) for i, l in enumerate(lows)]
    candles[7] = _candle(7, l=89, h=93, c=92, o=90)   # close 92 < line ~106.6
    _feed(atr, det, candles,
          {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)]}.items())
    assert det.candidates() == []


def test_minimum_three_touches_required():
    lows = [104, 103, 102, 100, 106, 107, 109, 108, 109, 110, 111, 112, 113]
    det = _support_scenario(lows, {6: [_pivot("L", 100, 3)],
                                   12: [_pivot("L", 110, 9)]})
    assert det.candidates() == []                  # anchors only: 2 < 3
    assert MIN_TOUCHES == 3


def test_atr_tolerance_boundary_inside_vs_outside():
    inside = _support_scenario(_LOWS[:13], {6: [_pivot("L", 100, 3)],
                                            12: [_pivot("L", 110, 9)]})
    [cand] = inside.candidates()                   # low 105 @6 within tol
    assert cand.touches == 4                       # {3, 6, 9, 12}: D11.7
    assert cand.last_touch_index == 12             # projects past anchor b
    lows = list(_LOWS[:13])
    lows[6] = 102                                  # 2.8% off the line
    outside = _support_scenario(lows, {6: [_pivot("L", 100, 3)],
                                       12: [_pivot("L", 110, 9)]})
    [cand] = outside.candidates()
    assert cand.touches == 3                       # bar 6 out of tol (~1.1%):
    assert cand.last_touch_index == 12             # only {3, 9, 12} remain


def test_candidates_empty_while_atr_unwarm():
    atr = IncrementalATR(period=50)                # never warms here
    det = TrendlineDetector(atr)
    candles = [_candle(i, l) for i, l in enumerate(_LOWS)]
    _feed(atr, det, candles, {6: [_pivot("L", 100, 3)],
                              12: [_pivot("L", 110, 9)]}.items())
    assert atr.value is None and det.candidates() == []


def test_twelve_pivot_window_slides_out_old_anchor():
    lows = _LOWS[:13] + [112 + i for i in range(12)]     # bars 13..24 benign
    atr, det = _rig()
    _feed(atr, det, [_candle(i, l) for i, l in enumerate(lows)],
          {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)]}.items())
    assert det.candidates() != []                  # (3,9) alive
    for n, i in enumerate(range(13, 23)):          # 10 junk pivots: 12 total
        det.on_pivot(_pivot("L", 99 - n, i))
    assert any((c.a_index, c.b_index) == (3, 9) for c in det.candidates())
    det.on_pivot(_pivot("L", 88, 23))              # 13th: 100@3 slides out
    assert det.candidates() == []                  # descending junk: no lines


def test_deterministic_replay_and_idempotent_candidates():
    d1 = _support_scenario()
    d2 = _support_scenario()
    c1, c2 = d1.candidates(), d2.candidates()
    assert c1 == c2                                # fresh replay identical
    assert d1.candidates() == c1                   # on-demand + idempotent
    assert all(isinstance(c, TrendlineCandidate) for c in c1)
    assert N_PIVOTS == 12


# ===================================================== TrendlineBook (P1.15)
# Cadence per bar: atr.update -> det.update -> [on_pivot...] -> book.refresh.


def _book_rig():
    atr = IncrementalATR(period=1)
    det = TrendlineDetector(atr)
    return atr, det, TrendlineBook(det, atr)


def _book_feed(atr, det, book, candles, pivots_after=()):
    inject = dict(pivots_after)
    for idx, c in enumerate(candles):
        atr.update(c)
        det.update(c)
        for p in inject.get(idx, ()):
            det.on_pivot(p)
        book.refresh(c)


def _key(line):
    return (line.side, line.a_index, line.b_index)


def test_dedup_collapses_collinear_family_and_evicts_terminally():
    # The _LOWS collinear family: (3,9) is accepted first (bar 12); when
    # (3,15) appears (bar 18) with a higher score on the SAME line, greedy
    # dedup keeps only it — (3,9) is evicted (terminal) and (9,15) never
    # enters. Detector sees 3 candidates; the book keeps exactly 1.
    atr, det, book = _book_rig()
    candles = [_candle(i, l) for i, l in enumerate(_LOWS)]
    _book_feed(atr, det, book, candles,
               {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)],
                18: [_pivot("L", 121, 15)]}.items())
    assert len(det.candidates()) == 3
    [line] = book.active
    assert _key(line) == ("support", 3, 15) and line.status == "active"
    assert line.touches == 5
    assert book.archived_keys == frozenset({("support", 3, 9)})


def test_tiebreak_equal_scores_resistance_before_support():
    # Mirrored support+resistance lines sharing anchor bars (3, 9): equal
    # touches {3,6,9,10}, equal span, equal age -> equal scores; the frozen
    # tie-break (side ascending, after newer-b/newer-a) orders resistance
    # first. Wide-range anchor candles keep tolerances safe (hand-checked).
    spec = [(112, 118), (112, 118), (112, 118), (100, 130), (112, 118),
            (112, 118), (105, 125), (112, 118), (112, 118), (110, 120),
            (112, 118), (112, 118), (112, 118)]
    candles = [_candle(i, l, h=h, c=115, o=114) for i, (l, h) in enumerate(spec)]
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, candles,
               {6: [_pivot("L", 100, 3), _pivot("H", 130, 3)],
                12: [_pivot("L", 110, 9), _pivot("H", 120, 9)]}.items())
    assert [(_key(l)) for l in book.active] == [
        ("resistance", 3, 9), ("support", 3, 9)]
    assert book.active[0].touches == book.active[1].touches == 4


def _band_lows(base):
    scale = base / 100.0
    rel = [104, 103, 102, 100, 106, 107, 105, 108, 109, 110]
    return [r * scale for r in rel]


def _band_candles_and_pivots(n_bands):
    """Band k (bars 10k..10k+9): the base support pattern scaled by 2**k —
    equal slopes (near-parallel) but far-apart intercepts (NOT clustered:
    the dedup AND-rule keeps them all distinct)."""
    candles, pivots = [], {}
    for k in range(n_bands):
        base = 100.0 * (2 ** k)
        s = base / 100.0
        for j, low in enumerate(_band_lows(base)):
            i = 10 * k + j
            candles.append(_candle(i, low, h=low + 4 * s, c=low + 3 * s,
                                   o=low + s))
        pivots[10 * k + 3] = [_pivot("L", base, 10 * k + 3)]
        pivots[10 * k + 9] = [_pivot("L", base * 1.1, 10 * k + 9)]
    return candles, pivots


def test_cap_three_per_side_with_terminal_eviction():
    # The x2-per-band scaling makes the SECOND anchors collinear in log
    # space (110@9, 220@19, 440@29, 880@39): a valid high-span diagonal the
    # engine rightly ranks first — and each longer diagonal pair dedup-
    # evicts its shorter predecessor (same line), proving terminal dedup.
    # The FIRST-anchor diagonal is correctly absent: it passes above the
    # intermediate closes, so the anchor-segment validity filter kills it.
    candles, pivots = _band_candles_and_pivots(4)      # bands 0..3, bars 0-39
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, candles, pivots.items())
    assert CAP_PER_SIDE == 3 and len(book.active) == 3
    assert [_key(l) for l in book.active] == [
        ("support", 9, 39),                            # diagonal, 4 touches
        ("support", 33, 39), ("support", 23, 29)]      # freshest band lines
    assert book.archived_keys == frozenset({
        ("support", 3, 9),                             # cap-evicted (bar 29)
        ("support", 9, 29),                            # dedup-evicted by (9,39)
        ("support", 13, 19)})                          # cap-evicted (bar 39)
    # a 5th band extends the diagonal and evicts the same way
    more, more_pivots = _band_candles_and_pivots(5)
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, more, more_pivots.items())
    assert [_key(l) for l in book.active] == [
        ("support", 9, 49), ("support", 43, 49), ("support", 33, 39)]
    assert book.archived_keys == frozenset({
        ("support", 3, 9), ("support", 13, 19), ("support", 23, 29),
        ("support", 9, 29), ("support", 9, 39)})


def test_archive_after_300_bars_inactivity_is_terminal():
    candles, pivots = _band_candles_and_pivots(3)      # bands 0..2, bars 0-29
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, candles, pivots.items())
    assert len(book.active) == 3
    last_touches = {_key(l): l.last_touch_index for l in book.active}
    # flat bars far BELOW every line: no touches, no crossings that matter
    flat = [_candle(30 + i, 50.0) for i in range(ARCHIVE_AGE_BARS + 5)]
    _book_feed(atr, det, book, flat)
    assert book.active == []                           # all stale-archived
    assert set(last_touches) <= book.archived_keys
    # terminal: detector still offers the same keys, book never re-accepts
    assert det.candidates() != []
    assert book.active == []


def test_ordering_stability_and_deterministic_replay():
    def run():
        candles, pivots = _band_candles_and_pivots(4)
        atr, det, book = _book_rig()
        _book_feed(atr, det, book, candles, pivots.items())
        return ([(_key(l), l.touches, l.last_touch_index, l.status)
                 for l in book.active], book.archived_keys)
    assert run() == run()


async def test_persistence_capability_lines_only(db_conn):
    atr, det, book = _book_rig()
    candles = [_candle(i, l) for i, l in enumerate(_LOWS[:13])]
    _book_feed(atr, det, book, candles,
               {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)]}.items())
    [line] = book.active
    row = line_to_row(line)
    assert row["kind"] == "TRENDLINE"                  # only lines persist;
    # channels do not exist in this engine and never will persist (D11.10)
    level_id = await db.insert_level(db_conn, **row)
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert len(rows) == 1 and rows[0]["status"] == "active"
    assert float(rows[0]["p1"]) == 100.0 and float(rows[0]["p2"]) == 110.0
    # lifecycle transition capability: archived via the existing helper
    await db.update_level_lifecycle(db_conn, level_id, touches=line.touches,
                                    status="archived", status_ts=_ts(400))
    rows = await db.select_levels(db_conn, "BTCUSDT", "1m")
    assert rows[0]["status"] == "archived"
    assert rows[0]["touches"] == line.touches

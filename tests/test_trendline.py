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
    RVOL_PLACEHOLDER_PASSES,
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
    # price climbing ABOVE every line faster than the lines rise (8%/bar vs
    # the steepest diagonal's ~7%/bar): valid side, always out of tolerance
    # — no touches and no close-throughs, so staleness is the only path
    riser = []
    level = 600.0
    for i in range(ARCHIVE_AGE_BARS + 5):
        riser.append(_candle(30 + i, level, h=level * 1.01,
                             c=level * 1.005, o=level * 1.002))
        level *= 1.08
    _book_feed(atr, det, book, riser)
    assert book.active == []                           # all stale-archived
    assert set(last_touches) <= book.archived_keys
    assert book.broken_keys == frozenset()             # nothing ever broke
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


# ============================================ break episodes + role flip
# Kept line (support, 3, 9): y(t) = 100 * 1.1^((t-3)/6); hand values:
# y13=117.216 y14=119.093 y15=121.000 y16=122.937 y17=124.906 y18=126.906
# y19=128.938 y20=131.002 y21=133.100 y22=135.231. All TR/tolerance margins
# hand-verified with the period-1 ATR (freeze audit re-verified them).


def _kept_book():
    """Book with the single kept support line (3,9): touches 4, last 12."""
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, [_candle(i, l) for i, l in enumerate(_LOWS[:13])],
               {6: [_pivot("L", 100, 3)], 12: [_pivot("L", 110, 9)]}.items())
    [line] = book.active
    assert line.touches == 4 and line.watch_remaining is None
    return atr, det, book


def _bars(atr, det, book, specs, start):
    """Feed (l, h, o, c) specs from bar `start`; returns events per bar."""
    out = []
    for j, (l, h, o, c) in enumerate(specs):
        candle = _candle(start + j, l, h=h, c=c, o=o)
        atr.update(candle)
        det.update(candle)
        out.append(book.refresh(candle))
    return out

_QUAL_BREAKER = (114.5, 121.5, 121, 115)   # body 6 > 0.8*TR(7) = 5.6
_BELOW = [(113, 117, 116, 115), (114, 118, 117, 116), (115, 119, 118, 117)]


def test_break_watch_opens_on_strict_close_through():
    atr, det, book = _kept_book()
    [events] = _bars(atr, det, book, [_QUAL_BREAKER], 13)
    assert events == []                            # opening a watch: no event
    [line] = book.active                           # line stays ACTIVE
    assert line.status == "active" and line.watch_remaining == 3
    assert line.watch_qualified is True            # body 6 > 5.6 & RVOL True
    assert line.touches == 4                       # no touch on the through-bar


def test_fake_break_reentry_keeps_line_and_resumes_touches():
    atr, det, book = _kept_book()
    events = _bars(atr, det, book, [
        _QUAL_BREAKER,
        (118, 122, 119, 121),                      # close 121 >= y14: re-entry
        (121, 125, 122, 124),                      # low on y15=121: touch
    ], 13)
    assert [[e.kind for e in bar] for bar in events] == [[], ["FAKE_BREAK"], ["TOUCH"]]
    [line] = book.active
    assert line.status == "active" and line.watch_remaining is None
    assert line.touches == 5 and line.last_touch_index == 15
    assert book.broken_keys == frozenset()


def test_confirmed_qualified_break_emits_event_and_breaks_line():
    atr, det, book = _kept_book()
    [line] = book.active
    events = _bars(atr, det, book, [_QUAL_BREAKER] + _BELOW, 13)
    assert [[e.kind for e in bar] for bar in events] == [[], [], [], ["BREAK"]]
    [ev] = events[3]
    assert (ev.side, ev.a_index, ev.b_index, ev.bar_index) == ("support", 3, 9, 16)
    assert book.active == [] and line.status == "broken"
    assert book.broken_keys == frozenset({("support", 3, 9)})
    assert line.touches == 4                       # frozen during the watch
    [flip] = book.flip_candidates                  # role flip registered
    assert flip.side == "resistance" and flip.touches == 0


def test_unqualified_break_is_silent_and_body_boundary_is_strict():
    atr, det, book = _kept_book()
    # body 4 == 0.8 * TR(5) exactly -> NOT qualified (strict >)
    events = _bars(atr, det, book, [(114, 119, 119, 115)] + _BELOW, 13)
    assert [e.kind for bar in events for e in bar] == []   # zero events ever
    assert book.active == []                       # ...but the line still broke
    assert book.broken_keys == frozenset({("support", 3, 9)})


def test_rvol_placeholder_is_flagged_true():
    assert RVOL_PLACEHOLDER_PASSES is True         # until P2.2 swaps real RVOL


def test_multiple_simultaneous_watches_resolve_independently():
    # Mirrored support+resistance rig: their lines CROSS (support above
    # resistance from bar ~13), so one close can be beyond both at once.
    spec = [(112, 118), (112, 118), (112, 118), (100, 130), (112, 118),
            (112, 118), (105, 125), (112, 118), (112, 118), (110, 120),
            (112, 118), (112, 118), (112, 118)]
    candles = [_candle(i, l, h=h, c=115, o=114) for i, (l, h) in enumerate(spec)]
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, candles,
               {6: [_pivot("L", 100, 3), _pivot("H", 130, 3)],
                12: [_pivot("L", 110, 9), _pivot("H", 120, 9)]}.items())
    events = _bars(atr, det, book, [
        (114, 121, 121, 115),   # beyond BOTH: two watches, body 6 qualifies
        (118, 122, 119, 120),   # support re-enters (FAKE); resistance stays out
        (119, 123, 120, 122),   # resistance still beyond
        (120, 124, 121, 123),   # resistance watch expires -> BREAK
    ], 13)
    assert [[(e.kind, e.side) for e in bar] for bar in events] == [
        [], [("FAKE_BREAK", "support")], [], [("BREAK", "resistance")]]
    assert [_key(l) for l in book.active] == [("support", 3, 9)]
    assert book.broken_keys == frozenset({("resistance", 3, 9)})
    [flip] = book.flip_candidates
    assert flip.side == "support" and flip.slope < 0   # direction-EXEMPT flip


def test_role_flip_restarts_touches_and_earns_keep_independently():
    atr, det, book = _kept_book()
    _bars(atr, det, book, [_QUAL_BREAKER] + _BELOW, 13)      # broken @16
    events = _bars(atr, det, book, [
        (121, 125, 123, 122),                      # flip high on y17: touch 1
        (123, 127, 125, 124),                      # touch 2
        (125, 129, 127, 126),                      # touch 3 -> promoted
    ], 17)
    assert [e.kind for bar in events for e in bar] == []   # earning is silent
    [line] = book.active
    assert _key(line) == ("resistance", 3, 9)
    assert line.status == "active" and line.touches == 3   # NO inheritance
    assert line.slope > 0                          # ascending resistance:
    assert book.flip_candidates == []              # direction-exempt path
    # the original support key stays terminally broken (never re-accepted)
    assert ("support", 3, 9) in book.broken_keys
    assert det.candidates() != []                  # detector still offers it


def test_full_lifecycle_and_replay_determinism():
    def run():
        atr, det, book = _kept_book()
        log = []
        log += _bars(atr, det, book, [_QUAL_BREAKER], 13)          # WATCH
        log += _bars(atr, det, book, [(118, 122, 119, 121)], 14)   # FAKE->ACTIVE
        log += _bars(atr, det, book, [(121, 125, 122, 124)], 15)   # TOUCH
        # second cross, weak body (4 <= 0.8*TR(9)=7.2): silent break path
        log += _bars(atr, det, book, [(115, 120, 120, 116)], 16)   # WATCH
        log += _bars(atr, det, book, [(114, 118, 117, 116),
                                      (115, 119, 118, 117),
                                      (116, 120, 119, 118)], 17)   # BROKEN @19
        log += _bars(atr, det, book, [(127, 131, 129, 128),
                                      (129, 133, 131, 130),
                                      (131, 135, 133, 132)], 20)   # flip earns
        snapshot = [(_key(l), l.status, l.touches, l.watch_remaining)
                    for l in book.active]
        return ([[(e.kind, e.side, e.bar_index) for e in bar] for bar in log],
                snapshot, book.broken_keys, book.archived_keys)

    events, snapshot, broken, archived = run()
    flat = [e for bar in events for e in bar]
    assert flat == [("FAKE_BREAK", "support", 14), ("TOUCH", "support", 15)]
    assert len(flat) == len(set(flat))             # no duplicate events
    assert snapshot == [(("resistance", 3, 9), "active", 3, None)]
    assert broken == frozenset({("support", 3, 9)})
    assert run() == (events, snapshot, broken, archived)   # replay-identical


def test_channels_from_parallel_flip_pair_and_geometric_mid():
    # Channels need same-direction parallel lines; the only in-engine path
    # to an ascending resistance is a role flip — exactly what this builds:
    # the flipped resistance (3,9) plus a new parallel support (20,26), both
    # slope ln(1.1)/6 (delta 0 < 8%). Mid-line = log midpoint (geometric
    # mean in price space). Channels are derived only — never persisted.
    atr, det, book = _kept_book()
    _bars(atr, det, book, [_QUAL_BREAKER] + _BELOW, 13)      # broken @16
    _bars(atr, det, book, [(121, 125, 123, 122), (123, 127, 125, 124),
                           (125, 129, 127, 126)], 17)        # flip kept @19
    assert book.channels() == []                             # no support yet
    ext = [_candle(20, 120, h=124, o=121, c=123),
           _candle(21, 127.2, h=131.2, o=128.2, c=130.2),
           _candle(22, 128.4, h=132.4, o=129.4, c=131.4),
           _candle(23, 126, h=130, o=127, c=129),            # touch on y(23)
           _candle(24, 129.6, h=133.6, o=130.6, c=132.6),
           _candle(25, 131.4, h=135.4, o=132.4, c=134.4),
           _candle(26, 132, h=136, o=133, c=135)]
    _book_feed(atr, det, book, ext,
               {0: [_pivot("L", 120, 20)], 6: [_pivot("L", 132, 26)]}.items())
    keys = {_key(l) for l in book.active}
    # cross-pivot pairs (3,20)/(9,20) legitimately fill the support cap but
    # are NOT 8%-parallel to the resistance — the channel set stays single
    assert {("resistance", 3, 9), ("support", 20, 26)} <= keys
    [ch] = book.channels()
    assert _key(ch.support) == ("support", 20, 26)
    assert _key(ch.resistance) == ("resistance", 3, 9)
    assert ch.mid_slope == pytest.approx(log(1.1) / 6, rel=1e-12)
    # mid-line at bar 26 = geometric mean of the two line prices there
    y_res_26 = log(100.0) + (log(1.1) / 6) * 23
    expected_mid = (log(132.0) + y_res_26) / 2
    assert ch.mid_value(26) == pytest.approx(expected_mid, rel=1e-12)
    from math import exp, sqrt
    assert exp(ch.mid_value(26)) == pytest.approx(
        sqrt(132.0 * exp(y_res_26)), rel=1e-9)
    # determinism: derived on demand, identical on repeat
    assert book.channels() == [ch]


def test_no_channel_for_opposite_slope_pair():
    # ordinary geometry: ascending support + descending resistance -> the
    # 8% parallelism test fails -> no channel (tie-break rig reused)
    spec = [(112, 118), (112, 118), (112, 118), (100, 130), (112, 118),
            (112, 118), (105, 125), (112, 118), (112, 118), (110, 120),
            (112, 118), (112, 118), (112, 118)]
    candles = [_candle(i, l, h=h, c=115, o=114) for i, (l, h) in enumerate(spec)]
    atr, det, book = _book_rig()
    _book_feed(atr, det, book, candles,
               {6: [_pivot("L", 100, 3), _pivot("H", 130, 3)],
                12: [_pivot("L", 110, 9), _pivot("H", 120, 9)]}.items())
    assert len(book.active) == 2 and book.channels() == []


# --------------------------------------------- freeze-audit regression tests


def test_open_watch_survives_staleness_and_resolves_as_broken():
    # Fix: archive paths exempt watch-open lines (D11.9 "stays active").
    # Line (3,9), last touch 12. 297 riser bars (no touches, no crossing);
    # the close-through at bar 310 opens a watch; staleness (cur-12 >= 300)
    # would hit at bar 312 MID-WATCH — the line must survive to resolve as
    # BROKEN at bar 313, never archived.
    atr, det, book = _kept_book()
    level = 600.0
    risers = []
    for i in range(297):                                   # bars 13..309
        risers.append(_candle(13 + i, level, h=level * 1.01,
                              c=level * 1.005, o=level * 1.002))
        level *= 1.02
    _book_feed(atr, det, book, risers)
    [line] = book.active
    assert line.last_touch_index == 12                     # starved, not stale yet
    events = _bars(atr, det, book, [
        (12000, 13000, 12800, 12500),                      # bar 310: through
        (11800, 12400, 12200, 12000),                      # watch...
        (11600, 12200, 12000, 11800),                      # (staleness bar 312
        (11400, 12000, 11800, 11600),                      #  passes harmlessly)
    ], 310)
    assert book.active == []
    assert book.broken_keys == frozenset({("support", 3, 9)})
    assert book.archived_keys == frozenset()               # NOT archived
    assert len(book.flip_candidates) == 1                  # episode completed


def test_flip_candidate_expires_after_staleness_age():
    # Fix: flips that never earn their touches are dropped after the frozen
    # 300-bar staleness age — no unbounded state in a 24/7 process.
    atr, det, book = _kept_book()
    _bars(atr, det, book, [_QUAL_BREAKER] + _BELOW, 13)    # broken @16, flip born
    assert len(book.flip_candidates) == 1
    level = 600.0
    risers = []
    for i in range(ARCHIVE_AGE_BARS + 2):                  # bars 17..318
        risers.append(_candle(17 + i, level, h=level * 1.01,
                              c=level * 1.005, o=level * 1.002))
        level *= 1.02
    _book_feed(atr, det, book, risers)
    assert book.flip_candidates == []                      # expired, dropped


def test_flip_back_onto_terminal_key_is_never_created():
    # Fix: a promoted flip that later breaks would flip BACK onto the
    # original (terminally broken) key — dead on arrival, so never created.
    atr, det, book = _kept_book()
    _bars(atr, det, book, [_QUAL_BREAKER] + _BELOW, 13)    # support broken @16
    _bars(atr, det, book, [(121, 125, 123, 122), (123, 127, 125, 124),
                           (125, 129, 127, 126)], 17)      # flip kept @19
    assert [_key(l) for l in book.active] == [("resistance", 3, 9)]
    _bars(atr, det, book, [(137, 141, 138, 140),           # close 140 > y20:
                           (138, 142, 139, 141),           # resistance watch
                           (139, 143, 140, 142),
                           (140, 144, 141, 143)], 20)      # broken @23
    assert book.active == []
    assert book.broken_keys == frozenset({("support", 3, 9),
                                          ("resistance", 3, 9)})
    assert book.flip_candidates == []                      # no dead flip-back


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

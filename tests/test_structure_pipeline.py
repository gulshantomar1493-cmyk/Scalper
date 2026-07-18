"""P1.11 — dedicated structure validation suite (roadmap: all transitions +
repaint test). Exercises the COMPLETE pipeline exactly as it exists:

    PivotDetector -> PivotLabeler -> TrendState -> BosDetector -> ChochDetector

wired per the pinned cadence, over engineered datasets with hand-computed
expected timelines (all D10 band arithmetic worked by hand — the datasets are
therefore also band-rule regression tests). Centerpiece: the prefix-replay /
no-repaint proof. Zero production code is added or changed by this task.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import (
    BosDetector,
    ChochDetector,
    PivotDetector,
    PivotLabeler,
    TrendState,
)
from marketscalper.providers.base import Candle

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


# ------------------------------------------------------------- test-local rig


class _Pipeline:
    """The five production components in the pinned cadence; nothing more."""

    def __init__(self, symbol="BTCUSDT", tf="1m"):
        self.detector = PivotDetector(symbol, tf)
        self.labeler = PivotLabeler()
        self.trend = TrendState()
        self.atr = IncrementalATR()
        self.bos = BosDetector(self.trend, self.atr)
        self.choch = ChochDetector(self.trend)

    def step(self, candle):
        self.atr.update(candle)
        pivots = []
        for p in self.detector.update(candle):
            labeled = self.labeler.label(p)
            pivots.append(labeled)
            self.trend.on_pivot(labeled)
            self.bos.on_pivot(labeled)
            self.choch.on_pivot(labeled)
        state = self.trend.update(candle)
        bos_event = self.bos.update(candle)
        flip = self.choch.on_bos(bos_event) if bos_event is not None else None
        choch_event = self.choch.update(candle)
        return (tuple(pivots), state, bos_event, choch_event, flip)

    def run(self, candles):
        return [self.step(c) for c in candles]


def _candle(h, l, i, tf="1m"):
    """Full-body candle (o=l, c=h) — body == [l, h]."""
    step = 5 if tf == "5m" else 1
    return Candle(symbol="BTCUSDT", tf=tf, ts=M0 + timedelta(minutes=i * step),
                  o=float(l), h=float(h), l=float(l), c=float(h),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _mirror_candle(h, l, i):
    """Price-mirror around 30: highs<->lows, closes mirrored (o=h', c=l')."""
    hi, lo = 30.0 - l, 30.0 - h
    return Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=i),
                  o=hi, h=hi, l=lo, c=lo,
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _from_hl(pairs, tf="1m"):
    return [_candle(h, l, i, tf) for i, (h, l) in enumerate(pairs)]


def _transitions(states):
    out, prev = [], None
    for s in states:
        if s != prev:
            out.append((prev, s))
            prev = s
    return out


def _pivots_by_bar(timeline):
    return {i: rec[0] for i, rec in enumerate(timeline) if rec[0]}


# --------------------------------------------------------- engineered datasets
# DS1 — flip journey (l = h-1 throughout). Hand-computed expectations:
#   pivots: bar6 (H,15,None) · bar9 (L,9,None) · bar13 (H,17,HH) ·
#           bar16 (L,11,HL) · bar20 (H,18,HH) · bar23 (L,7,LL) ·
#           bar25 (H,10,LH)
#   trend:  None 0-15 · BULLISH 16-18 · RANGE 19-24 (band counts 12,14,14,
#           13,20,20) · BEARISH 25-26 (counts 8,7)
#   events: BOS UP bar17 (broke 17, close 18) · CHOCH DOWN bar18 (broke 11,
#           close 10) · BOS DOWN bar26 (broke 7, close 6) + ConfirmedFlip.

_DS1 = [(10, 9), (11, 10), (12, 11), (15, 14), (12, 11), (11, 10), (10, 9),
        (11, 10), (12, 11), (13, 12), (17, 16), (14, 13), (13, 12), (12, 11),
        (13, 12), (14, 13), (15, 14), (18, 17), (10, 9), (9, 8), (8, 7),
        (9, 8), (10, 9), (9, 8), (8, 7), (7, 6), (6, 5)]

# DS3 — whipsaw/cancellation: DS1 through the CHOCH (bar18), then a shallow
# dip (keeping bar17's high a pivot and tying lows so bar18 never becomes
# one) and an escape rally with bodies above the band. Hand-computed:
#   extra pivot: bar20 (H,18,HH) — re-arms the BOS latch
#   trend: BULLISH 16-18 · RANGE 19-23 (counts 12,14,14,13,12) ·
#          BULLISH 24-25 (count 11)
#   BOS UP bar24 (broke 18, close 22) CANCELS the pending CHOCH; no flip.

_DS3 = _DS1[:19] + [(11, 9), (13, 12), (19, 18.5), (20, 19.5), (21, 20.5),
                    (22, 21.5), (23, 22.5)]

# DS4 — range chop: equal-extreme oscillation. Labels go LH/LL via the
# strict-equality rule (weak-bearish reading) until the band wakes at bar19
# and pins RANGE — exactly the rescue D10's band exists for. No events ever.
_DS4 = [(12, 11), (11, 10), (10, 9), (11, 10)] * 10

# DS6 — straight to RANGE: chains complete with mixed labels (HH + LL) at
# bar18, under 20 candles so the band stays asleep. Pivots: bar6 (H,12,None),
# bar8 (L,5,None), bar14 (H,16,HH), bar18 (L,4,LL).
_DS6 = [(8, 5), (9, 5.5), (10, 6), (12, 7), (10, 6), (9, 5), (8, 5.5),
        (9, 6), (13, 6.5), (14, 7), (15, 7.5), (16, 8), (15, 7.5), (14, 7),
        (13, 6.5), (6, 4), (7, 4.5), (8, 5), (9, 5.5)]


def _varied_stream(n, tf="1m"):
    """Deterministic varied stream (no RNG): drifting bases, mixed candle
    colors, wicks beyond bodies — the property-test workhorse."""
    step = 5 if tf == "5m" else 1
    out = []
    for i in range(n):
        base = 100.0 + 12.0 * ((i // 60) % 3)
        c = base + ((i * 7) % 13) - 6
        o = c - ((i * 5) % 7) + 3
        hi = max(o, c) + ((i * 3) % 4)
        lo = min(o, c) - ((i * 2) % 3) - 1
        out.append(Candle(symbol="BTCUSDT", tf=tf,
                          ts=M0 + timedelta(minutes=i * step),
                          o=float(o), h=float(hi), l=float(lo), c=float(c),
                          v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5))
    return out


# ------------------------------------------------------- engineered scenarios


def test_flip_journey_full_timeline():
    timeline = _Pipeline().run(_from_hl(_DS1))
    states = [rec[1] for rec in timeline]
    assert states == ([None] * 16 + ["BULLISH"] * 3 + ["RANGE"] * 6
                      + ["BEARISH"] * 2)
    piv = {i: [(p.kind, p.price, p.label) for p in ps]
           for i, ps in _pivots_by_bar(timeline).items()}
    assert piv == {6: [("H", 15.0, None)], 9: [("L", 9.0, None)],
                   13: [("H", 17.0, "HH")], 16: [("L", 11.0, "HL")],
                   20: [("H", 18.0, "HH")], 23: [("L", 7.0, "LL")],
                   25: [("H", 10.0, "LH")]}
    bos_bars = {i: rec[2] for i, rec in enumerate(timeline) if rec[2]}
    assert sorted(bos_bars) == [17, 26]
    assert (bos_bars[17].direction, bos_bars[17].broken_pivot.price,
            bos_bars[17].close) == ("UP", 17.0, 18.0)
    assert bos_bars[17].displacement is not None          # ATR warm by then
    assert (bos_bars[26].direction, bos_bars[26].broken_pivot.price,
            bos_bars[26].close) == ("DOWN", 7.0, 6.0)
    chochs = {i: rec[3] for i, rec in enumerate(timeline) if rec[3]}
    assert sorted(chochs) == [18]
    ch = chochs[18]
    assert (ch.direction, ch.broken_pivot.price, ch.close,
            ch.prior_trend) == ("DOWN", 11.0, 10.0, "BULLISH")
    flips = {i: rec[4] for i, rec in enumerate(timeline) if rec[4]}
    assert sorted(flips) == [26]
    flip = flips[26]
    assert flip.direction == "DOWN" and flip.choch is ch
    assert flip.bos is bos_bars[26] and flip.ts == flip.bos.ts


def test_mirror_flip_journey():
    candles = [_mirror_candle(h, l, i) for i, (h, l) in enumerate(_DS1)]
    timeline = _Pipeline().run(candles)
    states = [rec[1] for rec in timeline]
    assert states == ([None] * 16 + ["BEARISH"] * 3 + ["RANGE"] * 6
                      + ["BULLISH"] * 2)
    piv = {i: [(p.kind, p.price, p.label) for p in ps]
           for i, ps in _pivots_by_bar(timeline).items()}
    assert piv == {6: [("L", 15.0, None)], 9: [("H", 21.0, None)],
                   13: [("L", 13.0, "LL")], 16: [("H", 19.0, "LH")],
                   20: [("L", 12.0, "LL")], 23: [("H", 23.0, "HH")],
                   25: [("L", 20.0, "HL")]}
    bos_bars = {i: rec[2] for i, rec in enumerate(timeline) if rec[2]}
    assert sorted(bos_bars) == [17, 26]
    assert (bos_bars[17].direction, bos_bars[17].broken_pivot.price,
            bos_bars[17].close) == ("DOWN", 13.0, 12.0)
    chochs = {i: rec[3] for i, rec in enumerate(timeline) if rec[3]}
    assert sorted(chochs) == [18]
    assert (chochs[18].direction, chochs[18].broken_pivot.price,
            chochs[18].close, chochs[18].prior_trend) == \
        ("UP", 19.0, 20.0, "BEARISH")
    flips = {i: rec[4] for i, rec in enumerate(timeline) if rec[4]}
    assert sorted(flips) == [26] and flips[26].direction == "UP"


def test_whipsaw_cancellation_no_flip():
    pipeline = _Pipeline()
    timeline = pipeline.run(_from_hl(_DS3))
    states = [rec[1] for rec in timeline]
    assert states == ([None] * 16 + ["BULLISH"] * 3 + ["RANGE"] * 5
                      + ["BULLISH"] * 2)
    chochs = {i: rec[3] for i, rec in enumerate(timeline) if rec[3]}
    assert sorted(chochs) == [18]                        # warning fired...
    bos_bars = {i: rec[2] for i, rec in enumerate(timeline) if rec[2]}
    assert sorted(bos_bars) == [17, 24]
    assert (bos_bars[24].direction, bos_bars[24].broken_pivot.price,
            bos_bars[24].close) == ("UP", 18.0, 22.0)    # old trend resumed
    assert all(rec[4] is None for rec in timeline)       # ...never confirmed
    assert pipeline.choch.pending_flip is None           # cancelled at bar 24


def test_range_chop_no_events():
    timeline = _Pipeline().run(_from_hl(_DS4))
    states = [rec[1] for rec in timeline]
    assert states == [None] * 13 + ["BEARISH"] * 6 + ["RANGE"] * 21
    assert all(rec[2] is None and rec[3] is None and rec[4] is None
               for rec in timeline)                      # zero BOS/CHOCH/flip


def test_straight_to_range_dataset():
    timeline = _Pipeline().run(_from_hl(_DS6))
    states = [rec[1] for rec in timeline]
    assert states == [None] * 18 + ["RANGE"]
    piv = {i: [(p.kind, p.price, p.label) for p in ps]
           for i, ps in _pivots_by_bar(timeline).items()}
    assert piv == {6: [("H", 12.0, None)], 8: [("L", 5.0, None)],
                   14: [("H", 16.0, "HH")], 18: [("L", 4.0, "LL")]}


# ------------------------------------------------------- transition coverage


def test_transition_coverage_is_complete():
    datasets = [_from_hl(_DS1),
                [_mirror_candle(h, l, i) for i, (h, l) in enumerate(_DS1)],
                _from_hl(_DS3), _from_hl(_DS4), _from_hl(_DS6)]
    seen = set()
    labels_seen = set()
    for candles in datasets:
        timeline = _Pipeline().run(candles)
        seen.update(_transitions([rec[1] for rec in timeline]))
        for ps in _pivots_by_bar(timeline).values():
            labels_seen.update(p.label for p in ps)
    required = {(None, "BULLISH"), (None, "BEARISH"), (None, "RANGE"),
                ("BULLISH", "RANGE"), ("RANGE", "BEARISH"),
                ("BEARISH", "RANGE"), ("RANGE", "BULLISH")}
    assert required <= seen
    assert labels_seen == {None, "HH", "HL", "LH", "LL"}
    # CHOCH pending lifecycle: set->confirmed (DS1), set->cancelled (DS3);
    # set->replaced is unit-covered in test_structure.py (P1.10).


# --------------------------------------------- prefix replay (the centerpiece)


def _assert_prefix_property(candles):
    full = _Pipeline().run(candles)
    for n in range(len(candles) + 1):
        assert _Pipeline().run(candles[:n]) == full[:n]


def test_prefix_property_flip_journey():
    _assert_prefix_property(_from_hl(_DS1))


def test_prefix_property_varied_300():
    _assert_prefix_property(_varied_stream(300))


def test_split_feed_equals_uninterrupted_run():
    candles = _varied_stream(300)
    full = _Pipeline().run(candles)
    for n in range(0, len(candles) + 1, 1):
        pipeline = _Pipeline()
        assert pipeline.run(candles[:n]) + pipeline.run(candles[n:]) == full


# ------------------------------------------------------------- repaint proof


def test_repaint_guarantees_on_varied_stream():
    candles = _varied_stream(300)
    timeline = _Pipeline().run(candles)
    some_pivot = False
    for i, (pivots, _state, bos, choch, flip) in enumerate(timeline):
        bar_ts = candles[i].ts
        for p in pivots:
            some_pivot = True
            assert p.confirmed_ts == bar_ts        # confirmed by THIS bar
            assert p.ts <= p.confirmed_ts          # lag, never foresight
        if bos is not None:
            assert bos.ts == bar_ts
            assert bos.broken_pivot.ts <= bar_ts   # only confirmed history
        if choch is not None:
            assert choch.ts == bar_ts
            assert choch.broken_pivot.ts <= bar_ts
        if flip is not None:
            assert flip.ts == bar_ts
            assert flip.choch.ts < flip.ts         # never same-bar confirmed
    assert some_pivot                              # non-vacuous


# -------------------------------------------------------------- determinism


def test_determinism_all_datasets():
    runs = [_from_hl(_DS1),
            [_mirror_candle(h, l, i) for i, (h, l) in enumerate(_DS1)],
            _from_hl(_DS3), _from_hl(_DS4), _from_hl(_DS6),
            _varied_stream(300)]
    for candles in runs:
        assert _Pipeline().run(candles) == _Pipeline().run(candles)


def test_determinism_5m_pipeline():
    candles = _varied_stream(200, tf="5m")
    t1 = _Pipeline(tf="5m").run(candles)
    t2 = _Pipeline(tf="5m").run(candles)
    assert t1 == t2
    assert any(rec[0] for rec in t1)               # pivots exist (k=2)


# ------------------------------------------------------------------ warm-up


def test_warmup_exact_first_bars():
    candles = _varied_stream(300)
    timeline = _Pipeline().run(candles)
    pivots_at = _pivots_by_bar(timeline)
    assert min(pivots_at) >= 6                     # first pivot: bar 2k = 6
    # first classifiable bar: both chains hold their SECOND pivot
    h_bars = [i for i, ps in sorted(pivots_at.items())
              for p in ps if p.kind == "H"]
    l_bars = [i for i, ps in sorted(pivots_at.items())
              for p in ps if p.kind == "L"]
    first_classifiable = max(h_bars[1], l_bars[1])
    states = [rec[1] for rec in timeline]
    assert all(s is None for s in states[:first_classifiable])
    assert states[first_classifiable] is not None  # rules 2-5 are total
    first_trend = next(i for i, s in enumerate(states) if s is not None)
    for i, (_p, _s, bos, choch, _f) in enumerate(timeline):
        if bos is not None or choch is not None:
            assert i >= first_trend                # no events before a trend


# ---------------------------------------------------------- restart / replay


def test_restart_rewarns_from_scratch():
    candles = _varied_stream(300)
    tail = candles[150:]
    restarted = _Pipeline().run(tail)
    # a restart IS a fresh stream beginning at bar 150 (memory-only state):
    assert restarted == _Pipeline().run(tail)
    # and it re-warms: no pivots before its own bar 6, no trend before its
    # own chains are labeled, no events before its own trend exists
    assert all(not rec[0] for rec in restarted[:6])
    first_trend = next((i for i, rec in enumerate(restarted)
                        if rec[1] is not None), len(restarted))
    assert all(rec[2] is None and rec[3] is None for rec in restarted[:first_trend])

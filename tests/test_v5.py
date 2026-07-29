"""V5 price-action engine — the guarantees that make it different from V4.

V4 resolved 11 live trades as 11 stop-outs, ten of them shorts issued while the
daily was bullish, because direction came from where a level sat relative to
price rather than from any view of the market. These tests pin the properties
that make that impossible.
"""
from __future__ import annotations

import numpy as np

from marketscalper.v5 import (bias as B, engine, fold, geometry as G,
                              narrate, regime as R, structure as S)


def bars(seq, tf_s=3600, start=0):
    """seq: list of (o,h,l,c)."""
    n = len(seq)
    return dict(ts=np.arange(n, dtype=np.int64) * tf_s + start,
                o=np.array([x[0] for x in seq], float),
                h=np.array([x[1] for x in seq], float),
                l=np.array([x[2] for x in seq], float),
                c=np.array([x[3] for x in seq], float),
                v=np.ones(n), tf_s=tf_s)


def ramp(n, slope, base=100.0, tf_s=3600, wobble=0.6):
    """A trend WITH pullbacks, so it actually has swings.

    A monotonic line has no fractal pivots at all — every bar's high exceeds the
    last — so it reads as RANGE. Real markets always retrace; test data that
    does not is degenerate and proves nothing about a structure engine.
    """
    seq, v = [], base
    for i in range(n):
        # eight bars with the trend, three against: prints HH and HL cleanly
        v += slope if (i % 11) < 8 else -slope * 1.6
        seq.append((v - slope / 2, v + wobble, v - wobble, v))
    return bars(seq, tf_s)


# ------------------------------------------------------------- structure ---

def test_a_swing_is_confirmed_late_and_never_repaints():
    """A pivot is only known k bars after it printed. An engine that treats the
    current bar as a swing is reading the future."""
    seq = [(1, 1, 1, 1)] * 3 + [(1, 9, 1, 5)] + [(1, 1, 1, 1)] * 3
    b = bars(seq)
    sw = S.find_swings(b)
    assert len(sw) == 1
    assert sw[0].i == 3 and sw[0].confirmed_i == 5
    assert S.analyse(b, upto=4).last_high is None
    assert S.analyse(b, upto=5).last_high is not None


def test_a_tie_is_not_a_pivot():
    """Inclusive comparison clusters pivots at one price on quiet bars, and the
    cluster then labels as a fake HH/HL pair."""
    seq = [(1, 1, 1, 1)] * 2 + [(1, 5, 1, 3), (1, 5, 1, 3)] + [(1, 1, 1, 1)] * 3
    assert S.find_swings(bars(seq)) == []


def test_bullish_needs_both_a_higher_high_and_a_higher_low():
    """A higher high with a lower low is an expanding range, not an uptrend."""
    assert S.analyse(ramp(80, 1.0)).state == S.BULLISH
    assert S.analyse(ramp(80, -1.0)).state == S.BEARISH


def test_a_break_is_classified_by_the_structure_in_force_at_the_time():
    """Classifying with today's structure would relabel history every time the
    trend flipped — the repaint this module exists to prevent."""
    st = S.analyse(ramp(120, 1.0))
    assert st.last_bos is not None and st.last_bos.direction == 1
    assert all(b.kind in ("BOS", "CHOCH") for b in st.breaks)


def test_the_impulse_survives_a_pullback_printing_its_own_swing():
    """The leg being retraced is the one that ended at the last high, whatever
    has printed since. Requiring the high to be the newest swing returns None
    at exactly the moment a pullback becomes tradeable."""
    # a dip first (so a swing low exists), then the leg, then a pullback that
    # prints its own swing low
    dip = [(v, v + 1, v - 1, v) for v in range(110, 98, -1)]
    up = [(v, v + 1, v - 1, v) for v in range(98, 140)]
    down = [(v, v + 1, v - 1, v) for v in range(140, 120, -1)]
    imp = S.last_impulse(S.analyse(bars(dip + up + down)), 1)
    assert imp is not None, "mid-pullback the up-leg must still be found"
    origin, extreme = imp
    assert origin.i < extreme.i and extreme.price > origin.price


# ----------------------------------------------------------------- bias ---

def _st(state):
    return S.Structure(state, [], None, None, [], None, None)


def test_the_daily_decides_and_the_one_hour_gets_no_vote():
    """The 1H flips constantly inside a healthy trend — that is what a pullback
    is — and giving it a vote is how V4 authorised shorts during a rally."""
    b = B.decide(_st(S.BULLISH), _st(S.BULLISH), _st(S.BEARISH))
    assert b.direction == B.LONG and b.tradeable


def test_a_four_hour_contradicting_the_daily_stops_everything():
    b = B.decide(_st(S.BULLISH), _st(S.BEARISH), _st(S.BULLISH))
    assert b.direction == B.NONE and b.reason == B.TIMEFRAME_CONFLICT


def test_two_ranging_timeframes_mean_nobody_has_a_view():
    b = B.decide(_st(S.RANGE), _st(S.RANGE), _st(S.BULLISH))
    assert b.direction == B.NONE and b.reason == B.NO_DIRECTIONAL_EDGE


def test_a_ranging_daily_lets_the_four_hour_speak():
    b = B.decide(_st(S.RANGE), _st(S.BEARISH), _st(S.RANGE))
    assert b.direction == B.SHORT and b.source == "4H"


def test_unwarm_data_is_never_tradeable():
    b = B.decide(_st(S.BULLISH), _st(S.BULLISH), _st(S.BULLISH), warm=False)
    assert not b.tradeable and b.reason == B.NOT_ENOUGH_DATA


# ------------------------------------------------------------- geometry ---

def test_a_target_closer_than_two_r_is_rejected_not_stretched():
    """The whole point. V4 needed 10R to justify a volatility stop; V5 walks
    away rather than inventing a target that makes the ratio look acceptable."""
    assert G.build(100.0, 98.0, 1, [(101.0, "near")]) is None


def test_the_nearest_qualifying_structural_target_wins():
    p = G.build(100.0, 98.0, 1, [(101.0, "too near"), (107.0, "swing"), (140.0, "far")])
    assert p is not None
    assert p.target == 107.0 and p.target_label == "swing"
    assert 2.0 <= p.rr <= G.MAX_RR


def test_fees_are_charged_into_the_advertised_rr():
    """gross is what the chart shows; net is what the trader collects."""
    p = G.build(100.0, 98.0, 1, [(107.0, "swing")])
    assert p.rr < p.gross_rr and p.fee_r > 0


def test_a_target_beyond_the_cap_is_capped_not_chased():
    p = G.build(100.0, 99.0, 1, [(200.0, "moon")])
    assert p is not None and p.rr == G.MAX_RR and "capped" in p.target_label


def test_a_stop_on_the_wrong_side_of_entry_is_refused():
    assert G.build(100.0, 101.0, 1, [(120.0, "x")]) is None
    assert G.build(100.0, 99.0, -1, [(80.0, "x")]) is None


# --------------------------------------------------------------- regime ---

def test_a_straight_line_is_trending_and_a_round_trip_is_ranging():
    assert R.classify(np.arange(100.0, 140.0))[0] == R.TRENDING
    chop = np.array([100 + (i % 2) for i in range(60)], float)
    assert R.classify(chop)[0] == R.RANGING


def test_unwarm_data_is_ranging_not_trending():
    """The conservative side: it disables the continuation playbooks rather
    than authorising a trade on evidence we do not have."""
    assert R.classify(np.arange(5.0))[0] == R.RANGING


# ----------------------------------------------------------------- fold ---

def test_only_complete_buckets_are_folded():
    """A candle assembled from four minutes out of five is a lie about the high
    and the low, and structure is read from exactly those."""
    ts = np.array([0, 60, 120, 180, 240, 300, 360, 480, 540, 600], np.int64)
    m1 = dict(ts=ts, o=np.ones(10), h=np.ones(10) * 2, l=np.ones(10) * 0.5,
              c=np.ones(10), v=np.ones(10), tf_s=60)
    assert list(fold.fold(m1, "5m")["ts"]) == [0]   # the 300 bucket has a hole


def test_slice_upto_never_reveals_an_unclosed_bar():
    cut = fold.slice_upto(ramp(10, 1.0, tf_s=3600), 3 * 3600)
    assert len(cut["c"]) == 3


# --------------------------------------------------------------- engine ---

def _tf_set(slope=1.0):
    return dict(d1=ramp(90, slope, tf_s=86400),
                h4=ramp(200, slope / 6, tf_s=14400),
                h1=ramp(300, slope / 24, tf_s=3600),
                m15=ramp(300, slope / 96, tf_s=900))


def test_the_engine_always_produces_a_read_even_with_no_setup():
    """A bare "no setup" is indistinguishable from a broken engine — which V4
    was, the day it silently returned zero setups for a full history."""
    t = _tf_set()
    r = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    assert isinstance(r, engine.Read)
    assert r.bias.label in ("LONG", "SHORT", "FLAT")
    assert r.setup is not None or r.blocked or r.attempts


def test_a_setup_can_never_oppose_the_bias():
    """Not arbitrated away — never constructed. Every playbook is handed the
    bias and only builds in it."""
    t = _tf_set()
    r = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    if r.setup is not None:
        assert r.setup.direction == r.bias.direction


def test_an_open_trade_blocks_a_second_setup_on_the_same_symbol():
    t = _tf_set()
    base = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    assert base.bias.tradeable, "fixture must have a direction for this to mean anything"
    r = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"], in_trade=True)
    assert r.setup is None and r.blocked == "ALREADY_IN_A_TRADE"


def test_a_ranging_market_disables_the_continuation_playbooks():
    """A breakout system bleeding inside a range is exactly what killed V4."""
    flat = [(100, 100.5, 99.5, 100 + (i % 2) * 0.2) for i in range(300)]
    r = engine.read("ETHUSDT", ramp(90, 1.0, tf_s=86400), bars(flat, 14400),
                    bars(flat, 3600), bars(flat, 900))
    tried = {a.strategy_id for a in r.attempts}
    assert "trend_pullback" not in tried and "bos_retest" not in tried


def test_the_engine_is_deterministic():
    t = _tf_set()
    a = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    b = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    assert (a.bias.direction, a.blocked, a.regime) == (b.bias.direction, b.blocked, b.regime)
    assert [x.reason for x in a.attempts] == [x.reason for x in b.attempts]


# -------------------------------------------------------------- narrate ---

def test_the_trader_speaks_even_when_there_is_no_trade():
    t = _tf_set()
    r = engine.read("ETHUSDT", t["d1"], t["h4"], t["h1"], t["m15"])
    text = narrate.narrate(r)
    assert "ETHUSDT" in text
    assert "Daily:" in text and "4H:" in text
    assert len(text.splitlines()) >= 3
    assert narrate.headline(r).startswith("ETHUSDT")


def test_every_stand_aside_reason_has_words_not_a_code():
    """A reason the owner cannot read is not a reason."""
    from marketscalper.v5 import strategies as ST
    codes = [v for k, v in vars(ST).items()
             if k.isupper() and isinstance(v, str)]
    for c in codes:
        assert c in narrate._ATTEMPT or c in narrate._BLOCKED, f"{c} has no wording"

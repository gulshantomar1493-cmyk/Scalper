"""HTF analysis core (core/htf.py) — unit tests.

Post Phase-2.1 redesign: DIRECTION is price action only (market structure ->
BOS/CHOCH); indicators (EMA / momentum / 200-EMA) only move CONVICTION and can
NEVER flip bias. There is no fabricated score — bias + a conviction level
(STRONG/MODERATE/WEAK) + timeframe-agreement confidence. Pure (no DB).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from marketscalper.core import htf

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _wave(n, start, drift, amp, period=20, tf_minutes=60):
    """Oscillating price with drift — too smooth to leave labelled swing structure
    (used for the no-crash / range paths)."""
    out = []
    for i in range(n):
        mid = start + drift * i + amp * math.sin(2 * math.pi * i / period)
        nxt = start + drift * (i + 1) + amp * math.sin(2 * math.pi * (i + 1) / period)
        o, c = mid, nxt
        h = max(o, c) + amp * 0.15
        l = min(o, c) - amp * 0.15
        ts = _BASE + timedelta(minutes=tf_minutes * i)
        out.append({"ts": ts.isoformat(), "o": o, "h": h, "l": l, "c": c,
                    "v": 100.0 + (i % 7), "n": tf_minutes, "complete": True})
    return out


def _stairs(direction="up", steps=9, per=14, tf_minutes=60):
    """A stepped zigzag -> genuine HH/HL (up) or LH/LL (down) swings the frozen
    pivot/BOS engines actually label. REAL price-action structure, no indicators."""
    anchors = []
    for s in range(steps):
        if direction == "up":
            anchors += [100 + s * 16, 100 + s * 16 - 8]
        else:
            anchors += [260 - s * 16, 260 - s * 16 + 8]
    out, i = [], 0
    for a, b in zip(anchors, anchors[1:]):
        for k in range(per):
            p = a + (b - a) * k / per
            ts = _BASE + timedelta(minutes=tf_minutes * i)
            i += 1
            out.append({"ts": ts.isoformat(), "o": p, "h": p + 0.6, "l": p - 0.6,
                        "c": p, "v": 100.0, "n": tf_minutes, "complete": True})
    return out


_APPROVED_FIELDS = (
    "trend", "bias", "conviction", "structure", "bos", "choch", "swing_high",
    "swing_low", "liquidity", "liquidity_sweep", "supply", "demand", "support",
    "resistance", "trendlines", "ema_alignment", "momentum",
)


# ----------------------------------------- direction is price action ONLY

def test_pa_bias_comes_from_structure_then_events():
    assert htf._pa_bias("HH / HL", None, None) == "BULLISH"
    assert htf._pa_bias("LH / LL", None, None) == "BEARISH"
    assert htf._pa_bias("HH / LL", None, None) == "NEUTRAL"        # broadening = no clean read
    # a mixed / forming structure -> the latest structural EVENT breaks the tie
    assert htf._pa_bias("forming", {"direction": "UP"}, None) == "BULLISH"
    assert htf._pa_bias("forming", None, {"direction": "DOWN"}) == "BEARISH"
    assert htf._pa_bias("forming", None, None) == "NEUTRAL"


def test_indicators_can_never_flip_direction():
    """The core of Phase 2.1: bullish structure with BEARISH EMA + down momentum +
    price below the 200-EMA stays BULLISH — indicators only cut conviction. (The
    old weighted score would have flipped this bearish.)"""
    a = {"bos": None, "choch": None, "ema_alignment": "bearish",
         "momentum": {"direction": "down"}, "demand": [], "supply": []}
    bias = htf._pa_bias("HH / HL", a["bos"], a["choch"])
    assert bias == "BULLISH"                                       # structure wins
    frac = htf._conviction_fraction(a, 100.0, 200.0, bias)        # every indicator disagrees
    assert frac < 0.5 and htf._conviction_label(frac) in ("WEAK", "MODERATE")


def test_confirmations_raise_conviction_without_touching_bias():
    a = {"bos": {"direction": "UP"}, "choch": None, "ema_alignment": "bullish",
         "momentum": {"direction": "up"}, "demand": [{"lo": 99, "hi": 101}], "supply": []}
    frac = htf._conviction_fraction(a, 100.0, 90.0, "BULLISH")    # all confirm
    assert frac >= htf._STRONG and htf._conviction_label(frac) == "STRONG"


# --------------------------------------------------- end-to-end structure

def test_bullish_structure_reads_bullish():
    a = htf.analyze_timeframe("BTCUSDT", "1h", _stairs("up"))
    assert a["ready"] is True
    for f in _APPROVED_FIELDS:
        assert f in a, f
    assert "score" not in a and "_signed" not in a               # no fabricated score
    assert a["structure"] == "HH / HL"
    assert a["bias"] == "BULLISH" and a["trend"] == "Uptrend"     # consistent
    assert a["conviction"] in ("STRONG", "MODERATE")


def test_bearish_structure_reads_bearish():
    a = htf.analyze_timeframe("BTCUSDT", "1h", _stairs("down"))
    assert a["bias"] == "BEARISH" and a["trend"] == "Downtrend"


def test_structureless_data_reads_neutral():
    """Smooth drift with no labelled swings -> NEUTRAL (no indicator rescue)."""
    a = htf.analyze_timeframe("BTCUSDT", "1h", _wave(300, 100.0, 0.5, 4.0))
    assert a["ready"] is True and a["bias"] == "NEUTRAL" and a["conviction"] == "WEAK"


def test_empty_and_short_input_never_raise():
    for candles in ([], _wave(10, 100.0, 0.5, 4.0)):
        a = htf.analyze_timeframe("BTCUSDT", "1h", candles)
        assert a["ready"] is False and a["bias"] == "NEUTRAL" and a["conviction"] == "WEAK"
        assert "score" not in a


def test_momentum_and_zone_shapes():
    a = htf.analyze_timeframe("BTCUSDT", "1h", _stairs("up"))
    assert set(a["momentum"]) == {"velocity", "acceleration", "shift", "body_dominance", "direction"}
    for zone in a["supply"] + a["demand"]:
        assert zone["lo"] <= zone["hi"] and zone["status"] in ("active", "mitigated")
    assert a["support"] <= a["resistance"]


# ------------------------------------------------- overall roll-up (vote)

def _agg(**biases):
    """Build a per_tf dict with just the fields the roll-up + story read."""
    per = {}
    for tf, (bias, frac) in biases.items():
        per[tf] = {"ready": True, "bias": bias, "_frac": frac, "conviction": "STRONG",
                   "structure": "HH / HL" if bias == "BULLISH" else "LH / LL",
                   "bos": None, "choch": None}
    return per


def test_aggregate_is_a_timeframe_weighted_vote():
    # 1d(4)+4h(3) bullish vs 1h(2)+15m(1) bearish -> BULLISH by weight, 7/10 agree
    o = htf.aggregate_htf(_agg(**{"1d": ("BULLISH", 0.8), "4h": ("BULLISH", 0.7),
                                  "1h": ("BEARISH", 0.5), "15m": ("BEARISH", 0.4)}))
    assert o["bias"] == "BULLISH" and o["confidence"] == 70
    assert o["conviction"] in ("STRONG", "MODERATE", "WEAK")
    assert "score" not in o


def test_all_aligned_full_confidence_and_story():
    o = htf.aggregate_htf(_agg(**{tf: ("BULLISH", 0.8) for tf in htf.HTF_TIMEFRAMES}))
    assert o["bias"] == "BULLISH" and o["confidence"] == 100
    assert "Daily" in o["market_story"] and "bias (" not in o["market_story"]   # narrative, not dump
    assert o["explanation"]


def test_tie_reads_neutral():
    # 1d(4)+15m(1)=5 bull vs 4h(3)+1h(2)=5 bear -> tie -> NEUTRAL (no forced bias)
    o = htf.aggregate_htf(_agg(**{"1d": ("BULLISH", 0.7), "15m": ("BULLISH", 0.5),
                                  "4h": ("BEARISH", 0.7), "1h": ("BEARISH", 0.5)}))
    assert o["bias"] == "NEUTRAL"


# ------------------------------------------------- analyze() + purity

def test_analyze_end_to_end_shape_and_purity():
    cbt = {tf: _stairs("up") for tf in htf.HTF_TIMEFRAMES}
    r1 = htf.analyze("BTCUSDT", cbt)
    r2 = htf.analyze("BTCUSDT", cbt)
    assert r1 == r2                                              # pure / deterministic
    assert set(r1["timeframes"]) == set(htf.HTF_TIMEFRAMES)
    assert set(r1["overall"]) == {"bias", "conviction", "confidence", "market_story", "explanation"}
    assert r1["overall"]["bias"] == "BULLISH"
    for tf in htf.HTF_TIMEFRAMES:
        assert "_frac" not in r1["timeframes"][tf] and "_signed" not in r1["timeframes"][tf]


def test_missing_timeframe_handled():
    r = htf.analyze("BTCUSDT", {"1h": _stairs("up")})
    assert r["timeframes"]["1d"]["ready"] is False
    assert r["timeframes"]["1h"]["ready"] is True
    assert r["overall"]["bias"] in ("BULLISH", "BEARISH", "NEUTRAL")


# ------------------------------------------------------------- HtfService

class _FakeChart:
    def __init__(self, candles_by_tf: dict) -> None:
        self._cbt = candles_by_tf
        self.calls = 0

    async def get_chart(self, symbol, tf, start, end, **kw):
        self.calls += 1
        return {"candles": self._cbt.get(tf, [])}


def _cbt():
    return {tf: _stairs("up") for tf in htf.HTF_TIMEFRAMES}


async def test_service_caches_within_ttl():
    fake = _FakeChart(_cbt())
    svc = htf.HtfService(fake, ttl_seconds=999.0)
    r1 = await svc.analyze("BTCUSDT", now=_BASE)
    r2 = await svc.analyze("BTCUSDT", now=_BASE)
    assert r1 == r2 and r1["overall"]["bias"] == "BULLISH"
    assert fake.calls == len(htf.HTF_TIMEFRAMES)


async def test_service_recomputes_after_ttl():
    fake = _FakeChart(_cbt())
    svc = htf.HtfService(fake, ttl_seconds=0.0)
    await svc.analyze("BTCUSDT", now=_BASE)
    await svc.analyze("BTCUSDT", now=_BASE)
    assert fake.calls == 2 * len(htf.HTF_TIMEFRAMES)

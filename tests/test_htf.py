"""HTF V1.1 analysis core (core/htf.py) — unit tests.

Pure (no DB): the module reuses the frozen engines on aggregated candle dicts
and is isolated from the decision engine / determinism stream. These tests prove
the analysis runs end-to-end, surfaces every approved field, scores directionally,
rolls up an overall bias/confidence/story, and never raises on empty/short input.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from marketscalper.core import htf

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _wave(n: int, start: float, drift: float, amp: float, period: int = 20,
          tf_minutes: int = 60) -> list[dict]:
    """Oscillating price with a linear drift -> real HH/HL (or LH/LL) swings so
    the frozen pivot/trend/BOS engines actually fire. drift>0 uptrend, <0 down,
    ==0 range."""
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


_APPROVED_FIELDS = (
    "trend", "structure", "bos", "choch", "swing_high", "swing_low",
    "liquidity", "liquidity_sweep", "supply", "demand", "support", "resistance",
    "trendlines", "ema_alignment", "momentum", "score", "bias",
)


def test_uptrend_reads_bullish_with_all_fields():
    a = htf.analyze_timeframe("BTCUSDT", "1h", _wave(300, 100.0, 0.5, 4.0))
    assert a["ready"] is True
    for field in _APPROVED_FIELDS:
        assert field in a, field
    assert a["ema_alignment"] == "bullish"          # clean 20>50>200 stack
    assert a["trend"] == "Uptrend"                  # structure/EMA-derived (not RANGE)
    assert a["bias"] == "BULLISH" and a["score"] > 50.0
    assert "_signed" not in htf.analyze("BTCUSDT", {"1h": _wave(300, 100.0, 0.5, 4.0)})["timeframes"]["1h"]


def test_downtrend_reads_bearish():
    a = htf.analyze_timeframe("BTCUSDT", "4h", _wave(300, 500.0, -0.6, 5.0))
    assert a["ema_alignment"] == "bearish"
    assert a["trend"] == "Downtrend"
    assert a["bias"] == "BEARISH" and a["score"] < 50.0


def test_range_data_produces_no_runaway_conviction():
    a = htf.analyze_timeframe("BTCUSDT", "15m", _wave(300, 200.0, 0.0, 6.0))
    # a drift-free oscillation: the final swing still has a direction, but the
    # conviction must stay moderate — no runaway score either way.
    assert a["ready"] is True and 15.0 < a["score"] < 85.0


def test_empty_and_short_input_never_raise():
    for candles in ([], _wave(10, 100.0, 0.5, 2.0)):
        a = htf.analyze_timeframe("BTCUSDT", "1d", candles)
        assert a["ready"] is False and a["bias"] == "NEUTRAL" and a["score"] == 50.0


def test_momentum_and_zone_shapes():
    a = htf.analyze_timeframe("BTCUSDT", "1h", _wave(300, 100.0, 0.5, 4.0))
    mom = a["momentum"]
    assert set(mom) == {"velocity", "acceleration", "shift", "body_dominance", "direction"}
    assert mom["direction"] in ("up", "down", "flat")
    for zone in a["supply"] + a["demand"]:
        assert zone["lo"] <= zone["hi"] and zone["status"] in ("active", "mitigated")
    assert a["support"] <= a["resistance"]          # range floor <= ceiling


def test_aggregate_overall_bias_confidence_story():
    per_tf = {
        "1d": htf.analyze_timeframe("BTCUSDT", "1d", _wave(300, 100.0, 0.5, 4.0)),
        "4h": htf.analyze_timeframe("BTCUSDT", "4h", _wave(300, 100.0, 0.5, 4.0)),
        "1h": htf.analyze_timeframe("BTCUSDT", "1h", _wave(300, 100.0, 0.5, 4.0)),
        "15m": htf.analyze_timeframe("BTCUSDT", "15m", _wave(300, 100.0, 0.5, 4.0)),
    }
    overall = htf.aggregate_htf(per_tf)
    assert overall["bias"] == "BULLISH"
    assert 0 <= overall["confidence"] <= 100 and overall["confidence"] >= 75   # all aligned
    assert isinstance(overall["market_story"], str) and overall["market_story"]
    assert isinstance(overall["explanation"], str) and overall["explanation"]
    assert "Daily" in overall["market_story"]                        # top-down narrative


def test_conflicting_timeframes_lower_confidence():
    per_tf = {
        "1d": htf.analyze_timeframe("BTCUSDT", "1d", _wave(300, 100.0, 0.5, 4.0)),    # up
        "4h": htf.analyze_timeframe("BTCUSDT", "4h", _wave(300, 500.0, -0.6, 5.0)),   # down
        "1h": htf.analyze_timeframe("BTCUSDT", "1h", _wave(300, 500.0, -0.6, 5.0)),   # down
        "15m": htf.analyze_timeframe("BTCUSDT", "15m", _wave(300, 100.0, 0.5, 4.0)),  # up
    }
    overall = htf.aggregate_htf(per_tf)
    assert overall["confidence"] < 100                    # not fully aligned


def test_analyze_end_to_end_shape_and_purity():
    candles_by_tf = {tf: _wave(300, 100.0, 0.5, 4.0) for tf in htf.HTF_TIMEFRAMES}
    r1 = htf.analyze("BTCUSDT", candles_by_tf)
    r2 = htf.analyze("BTCUSDT", candles_by_tf)
    assert r1 == r2                                       # pure / deterministic
    assert set(r1["timeframes"]) == set(htf.HTF_TIMEFRAMES)
    assert r1["symbol"] == "BTCUSDT"
    assert set(r1["overall"]) == {"score", "bias", "confidence", "market_story", "explanation"}
    for tf in htf.HTF_TIMEFRAMES:
        assert "_signed" not in r1["timeframes"][tf]      # internal weight never surfaced


def test_missing_timeframe_handled():
    # only one tf supplied; the rest are absent -> ready=False, no crash
    r = htf.analyze("BTCUSDT", {"1h": _wave(300, 100.0, 0.5, 4.0)})
    assert r["timeframes"]["1d"]["ready"] is False
    assert r["timeframes"]["1h"]["ready"] is True
    assert r["overall"]["bias"] in ("BULLISH", "BEARISH", "NEUTRAL")


# ------------------------------------------------------------- HtfService

class _FakeChart:
    """Stand-in for ChartService.get_chart — counts fetches, returns preset
    candles per tf (ignores the range, like a warm cache would)."""

    def __init__(self, candles_by_tf: dict) -> None:
        self._cbt = candles_by_tf
        self.calls = 0

    async def get_chart(self, symbol, tf, start, end, **kw):
        self.calls += 1
        return {"candles": self._cbt.get(tf, [])}


def _cbt():
    return {tf: _wave(300, 100.0, 0.5, 4.0) for tf in htf.HTF_TIMEFRAMES}


async def test_service_caches_within_ttl():
    fake = _FakeChart(_cbt())
    svc = htf.HtfService(fake, ttl_seconds=999.0)
    r1 = await svc.analyze("BTCUSDT", now=_BASE)
    r2 = await svc.analyze("BTCUSDT", now=_BASE)
    assert r1 == r2 and r1["overall"]["bias"] == "BULLISH"
    assert fake.calls == len(htf.HTF_TIMEFRAMES)          # fetched once, then cache hit


async def test_service_recomputes_after_ttl():
    fake = _FakeChart(_cbt())
    svc = htf.HtfService(fake, ttl_seconds=0.0)           # always stale
    await svc.analyze("BTCUSDT", now=_BASE)
    await svc.analyze("BTCUSDT", now=_BASE)
    assert fake.calls == 2 * len(htf.HTF_TIMEFRAMES)      # recomputed both times


async def test_service_isolates_symbols():
    fake = _FakeChart(_cbt())
    svc = htf.HtfService(fake, ttl_seconds=999.0)
    a = await svc.analyze("BTCUSDT", now=_BASE)
    b = await svc.analyze("ETHUSDT", now=_BASE)
    assert a["symbol"] == "BTCUSDT" and b["symbol"] == "ETHUSDT"
    assert fake.calls == 2 * len(htf.HTF_TIMEFRAMES)      # a per-symbol cache each

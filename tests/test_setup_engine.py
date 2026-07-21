"""Trade Engine V2 (core/setup_engine.py) — the discretionary setup logic.

Hand-constructed HTF + LTF inputs (the shapes the frozen engines emit) drive the
gate: top-down alignment, the sweep->shift->zone pillars, the high-probability
bar, and the confident "no setup". Pure — no DB, no server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.core.setup_engine import MIN_CONFIDENCE, build_setups

T0 = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _sig(direction="LONG", entry=100.0, sl=98.0, tp1=104.0, tp2=108.0,
         strategy="S1", created=T0, bars=5):
    return {"strategy": strategy, "direction": direction, "entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2, "created_ts": created.isoformat(),
            "invalid_after_bars": bars, "facts": []}


def _htf(bias="BULLISH", conf=0.7, story="4H bullish; price drawing to buy-side liquidity."):
    return {"symbol": "BTCUSDT", "overall": {"bias": bias, "confidence": conf,
                                             "market_story": story}}


def _ltf(direction="LONG", pd="DISCOUNT", integrity="PASS", trend="BULLISH",
         sweeps=True, shifts=True, confl=True, rvol=1.5, cum_delta=120.0, signals=None):
    liq = {"premium_discount": pd,
           "sweeps": [{"ts": T0.isoformat(), "side": "LOW", "target": "EQL", "price": 97.9}] if sweeps else [],
           "shifts": [{"sweep_ts": T0.isoformat(), "ts": T0.isoformat()}] if shifts else [],
           "pools": [], "levels": {}}
    conf_zones = ([{"direction": "BULL" if direction == "LONG" else "BEAR",
                    "lo": 99.5, "hi": 100.5, "count": 3, "members": ["OB"],
                    "htf_magnet": True, "created_ts": T0.isoformat()}] if confl else [])
    return {"trend": trend, "liquidity": liq, "confluence": conf_zones,
            "volume": {"rvol": rvol, "cum_delta": cum_delta},
            "qualification": {"data_integrity": integrity, "verdict": "TRADEABLE"},
            "signals": signals if signals is not None else [_sig(direction=direction)]}


# ---------------------------------------------------------- the happy path

def test_high_probability_htf_aligned_long():
    out = build_setups("BTCUSDT", _htf(), _ltf(), now_ts=T0 + timedelta(minutes=2))
    assert len(out) == 1
    s = out[0]
    assert s.direction == "LONG" and s.htf_bias == "BULLISH"
    assert s.confidence >= MIN_CONFIDENCE                 # all pillars -> ~95.5
    assert s.rr == 2.0                                     # |104-100| / |100-98|
    assert s.risk_level == "LOW"                           # aligned + strong conviction
    # explainability: all six "why" questions answered + pillar reasons
    for k in ("why_exists", "why_now", "why_entry", "why_sl", "why_targets", "why_edge"):
        assert s.why.get(k)
    assert any("HTF" in r for r in s.reasons)
    assert any("swept" in r for r in s.reasons)
    assert str(s.sl) in s.invalidation


def test_short_mirror():
    out = build_setups("BTCUSDT", _htf(bias="BEARISH"),
                       _ltf(direction="SHORT", pd="PREMIUM", trend="BEARISH",
                            signals=[_sig(direction="SHORT", entry=100, sl=102, tp1=96, tp2=92)]),
                       now_ts=T0 + timedelta(minutes=2))
    assert len(out) == 1 and out[0].direction == "SHORT" and out[0].rr == 2.0


# ---------------------------------------------- the confident "no setup"

def test_counter_trend_to_convinced_htf_is_rejected():
    """Never fight a convinced higher timeframe (top-down discipline)."""
    out = build_setups("BTCUSDT", _htf(bias="BEARISH", conf=0.7),   # HTF down
                       _ltf(direction="LONG"),                       # 1m long trigger
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_no_signals_returns_no_setup():
    out = build_setups("BTCUSDT", _htf(), _ltf(signals=[]), now_ts=T0)
    assert out == []


def test_degraded_data_integrity_is_a_hard_gate():
    out = build_setups("BTCUSDT", _htf(), _ltf(integrity="DEGRADED"),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_insufficient_rr_rejected():
    out = build_setups("BTCUSDT", _htf(),
                       _ltf(signals=[_sig(entry=100, sl=98, tp1=101)]),   # rr 0.5
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_stale_trigger_rejected():
    """A trigger past its validity window is not a 'now' setup."""
    out = build_setups("BTCUSDT", _htf(), _ltf(),
                       now_ts=T0 + timedelta(minutes=20))     # >> 5 bars
    assert out == []


def test_below_threshold_not_surfaced():
    """Aligned + sweep/shift but no discount / confluence / volume -> ~65.5 < 70
    -> not high-probability -> not surfaced."""
    out = build_setups("BTCUSDT", _htf(conf=0.7),
                       _ltf(pd=None, confl=False, rvol=0.9, cum_delta=None),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_none_inputs_safe():
    assert build_setups("BTCUSDT", None, None) == []
    assert build_setups("BTCUSDT", None, _ltf(), now_ts=T0) == []   # no HTF -> no alignment

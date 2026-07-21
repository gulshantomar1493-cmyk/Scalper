"""Trade Engine V2 (core/setup_engine.py) — post-audit design.

Confidence is an emergent GRADE (A+/A/B) from confluence agreement, not a fake %.
Necessary gates (HTF, location, sweep->shift, net R:R, data integrity) must all
hold or there is no setup. Every setup carries the trader-card fields. Pure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.core.setup_engine import TradeSetupV2, build_setups

T0 = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _sig(direction="LONG", entry=100.0, sl=98.0, tp1=104.0, tp2=108.0,
         strategy="S1", created=T0, bars=5):
    return {"strategy": strategy, "direction": direction, "entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2, "created_ts": created.isoformat(),
            "invalid_after_bars": bars, "facts": []}


def _htf(bias="BULLISH", conf=70, story="4H bullish; drawing to buy-side liquidity."):
    # HtfService gives confidence as 0..100
    return {"symbol": "BTCUSDT",
            "overall": {"bias": bias, "confidence": conf, "market_story": story},
            "timeframes": {}}


def _ltf(direction="LONG", pd="DISCOUNT", integrity="PASS", trend="BULLISH",
         sweeps=True, shifts=True, zone=True, rvol=1.5, cum_delta=120.0, signals=None):
    long = direction == "LONG"
    liq = {"premium_discount": pd,
           "sweeps": [{"ts": T0.isoformat(), "side": "LOW" if long else "HIGH",
                       "target": "EQL" if long else "EQH", "price": 97.9 if long else 102.1}] if sweeps else [],
           "shifts": [{"sweep_ts": T0.isoformat(), "ts": T0.isoformat()}] if shifts else [],
           "pools": [{"kind": "EQH" if long else "EQL", "price": 104.5 if long else 95.5,
                      "size": 3, "strength": 1.0}],
           "levels": {}}
    obs = {"blocks": ([{"direction": "BULL" if long else "BEAR", "lo": 99.5, "hi": 100.5,
                        "status": "active", "created_ts": T0.isoformat()}] if zone else []),
           "breakers": []}
    return {"trend": trend, "liquidity": liq, "orderblocks": obs, "fvgs": [], "choch": [],
            "volume": {"rvol": rvol, "cum_delta": cum_delta},
            "qualification": {"data_integrity": integrity},
            "signals": signals if signals is not None else [_sig(direction=direction)]}


# ---------------------------------------------------------- the happy path

def test_a_plus_setup_all_confluences():
    out = build_setups("BTCUSDT", _htf(), _ltf(), now_ts=T0 + timedelta(minutes=2))
    assert len(out) == 1
    s = out[0]
    assert isinstance(s, TradeSetupV2)
    assert s.direction == "LONG" and s.htf_bias == "BULLISH"
    assert s.grade == "A+" and s.confluences == 5 and s.confluences_total == 5
    assert s.id == "BTCUSDT:S1:" + T0.isoformat()         # stable key
    assert s.grade_reason.startswith("Grade A+") and "confluences agree" in s.grade_reason
    assert "HTF bias alignment" in s.grade_reason          # names the agreeing factors
    assert not hasattr(s, "confidence")                  # no fabricated %
    assert 1.8 <= s.rr <= 1.9                             # NET of fees (~1.86), not 2.0
    assert s.risk_level == "LOW"
    assert s.setup_type == "Liquidity Sweep Reversal"
    for k in ("why_exists", "why_now", "why_entry", "why_sl", "why_targets", "why_edge"):
        assert s.why.get(k)
    assert s.reasons_to_avoid and s.early_exit and s.management_notes   # the trader card
    assert s.holding_time == "INTRADAY"
    assert not any(r.startswith("✓") for r in s.reasons)     # clean strings (UI adds ✓)


def test_short_mirror():
    out = build_setups("BTCUSDT", _htf(bias="BEARISH"),
                       _ltf(direction="SHORT", pd="PREMIUM", trend="BEARISH",
                            signals=[_sig(direction="SHORT", entry=100, sl=102, tp1=96, tp2=92)]),
                       now_ts=T0 + timedelta(minutes=2))
    assert len(out) == 1 and out[0].direction == "SHORT" and out[0].grade == "A+"


# ---------------------------------------- emergent grade / no fake confidence

def test_grade_emerges_from_confluence_count():
    """HTF neutral (not aligned), location only via a zone, no volume -> just one
    confluence -> grade B (valid but thin), not a manufactured number."""
    out = build_setups("BTCUSDT", _htf(bias="NEUTRAL", conf=0),
                       _ltf(pd=None, rvol=0.9, cum_delta=None),   # zone present, nothing else
                       now_ts=T0 + timedelta(minutes=2))
    assert len(out) == 1
    assert out[0].grade == "B" and out[0].confluences == 1 and out[0].confluences_total == 5
    assert out[0].grade_reason.startswith("Grade B") and "thin" in out[0].grade_reason
    # the honest bear case must call out the missing HTF bias
    assert any("range reaction" in r for r in out[0].reasons_to_avoid)


def test_market_context_is_a_narrative_not_a_summary():
    s = build_setups("BTCUSDT", _htf(), _ltf(), now_ts=T0 + timedelta(minutes=2))[0]
    ctx = s.market_context.lower()
    assert "control" in ctx and ("swept" in ctx or "liquidity" in ctx)
    assert "drawn" in ctx or "draw" in ctx or "toward" in ctx        # the draw on liquidity
    assert "bias (" not in ctx                                        # not the old indicator dump


def test_reasons_to_avoid_always_present():
    """A professional always argues the bear case for their own idea."""
    for out in (build_setups("BTCUSDT", _htf(), _ltf(), now_ts=T0 + timedelta(minutes=2)),
                build_setups("BTCUSDT", _htf(bias="NEUTRAL", conf=0), _ltf(pd=None),
                             now_ts=T0 + timedelta(minutes=2))):
        assert out and out[0].reasons_to_avoid


# ------------------------------------------ necessary gates (all reject)

def test_counter_trend_to_convinced_htf_rejected():
    out = build_setups("BTCUSDT", _htf(bias="BEARISH", conf=70), _ltf(direction="LONG"),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_no_valid_location_rejected():
    """No discount AND no named zone -> no location -> no setup (the fix for the
    'high-probability in a bad spot' defect)."""
    out = build_setups("BTCUSDT", _htf(), _ltf(pd=None, zone=False),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_no_sweep_shift_rejected():
    out = build_setups("BTCUSDT", _htf(), _ltf(sweeps=False, shifts=False),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_insufficient_net_rr_rejected():
    out = build_setups("BTCUSDT", _htf(), _ltf(signals=[_sig(entry=100, sl=98, tp1=101)]),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_degraded_data_integrity_rejected():
    out = build_setups("BTCUSDT", _htf(), _ltf(integrity="DEGRADED"),
                       now_ts=T0 + timedelta(minutes=2))
    assert out == []


def test_stale_trigger_rejected():
    out = build_setups("BTCUSDT", _htf(), _ltf(), now_ts=T0 + timedelta(minutes=20))
    assert out == []


def test_no_signals_and_none_safe():
    assert build_setups("BTCUSDT", _htf(), _ltf(signals=[]), now_ts=T0) == []
    assert build_setups("BTCUSDT", None, None) == []

"""V3 L2 Market Map + L3 Market Memory — unit tests over hand-built reads."""

from __future__ import annotations

import json

from marketscalper.v3.config import V3Config
from marketscalper.v3.market_map import build_map, build_memory

CFG = V3Config()


def _read(tf, *, trend="RANGE", atr=10.0, close=100.0, zones=(), pools=(),
          pd=None, context=None):
    return {"symbol": "BTCUSDT", "tf": tf, "ready": True, "trend": trend,
            "atr": atr, "last_close": close, "zones": list(zones),
            "liquidity": list(pools), "premium_discount": pd,
            "context": context or {}}


def _zone(tf, kind, lo, hi, state="FRESH", zid=None, touches=0, history=()):
    return {"id": zid or f"{tf}:{kind}:{lo}", "kind": kind, "state": state,
            "lo": lo, "hi": hi, "touches": touches, "created_at": 0,
            "origin": "t", "flipped_from": None, "history": list(history)}


def _pool(kind, price, side, priority, state="UNSWEPT", session=None,
          post_sweep="PENDING"):
    return {"id": f"p:{kind}:{price}", "kind": kind, "price": price,
            "side": side, "priority": priority, "state": state,
            "swept_at": 1 if state == "SWEPT" else None,
            "post_sweep": post_sweep, "session": session, "history": []}


# ------------------------------------------------------------------- zones

def test_overlapping_zones_stack_across_tfs():
    reads = {
        "1h": _read("1h", zones=[_zone("1h", "ORDER_BLOCK", 95, 97)]),
        "4h": _read("4h", zones=[_zone("4h", "DEMAND", 94, 96.5)]),
        "1d": _read("1d"),
    }
    m = build_map("BTCUSDT", reads, CFG)
    assert m["ready"] and m["zones"]
    z = m["zones"][0]
    assert z["stack"] == 2 and set(z["tf_stack"]) == {"1h", "4h"}
    # weight = 2 tfs + 2 FRESH × 0.5 = 3.0, and the explain names both parts
    assert z["weight"] == 3.0
    assert "4h DEMAND(FRESH)" in z["explain"] and "1h ORDER_BLOCK(FRESH)" in z["explain"]
    assert z["side"] == "BELOW" and z["distance"] > 0


def test_far_zones_do_not_merge():
    reads = {"1h": _read("1h", zones=[_zone("1h", "SR", 90, 91),
                                      _zone("1h", "SR", 120, 121)])}
    m = build_map("BTCUSDT", reads, CFG)
    assert len(m["zones"]) == 2
    assert all(z["stack"] == 1 for z in m["zones"])


def test_decision_points_ordered_by_distance():
    reads = {"1h": _read("1h", close=100, zones=[
        _zone("1h", "SR", 130, 131), _zone("1h", "DEMAND", 97, 98),
        _zone("1h", "SUPPLY", 108, 109)])}
    m = build_map("BTCUSDT", reads, CFG)
    d = [p["distance"] for p in m["decision_points"]]
    assert d == sorted(d) and m["decision_points"][0]["side"] in ("ABOVE", "BELOW")


# -------------------------------------------------------------------- bias

def test_bias_ladder_weighted_vote():
    reads = {"1d": _read("1d", trend="BULLISH"), "4h": _read("4h", trend="BULLISH"),
             "1h": _read("1h", trend="BEARISH"), "15m": _read("15m", trend="BEARISH"),
             "5m": _read("5m", trend="RANGE")}
    b = build_map("BTCUSDT", reads, CFG)["bias"]
    assert b["bull_weight"] == 7.0 and b["bear_weight"] == 3.0
    assert b["overall"] == "BULLISH"
    assert b["per_tf"]["1h"] == "BEARISH"


def test_bias_neutral_when_split():
    reads = {"1d": _read("1d", trend="BULLISH"), "4h": _read("4h", trend="BEARISH"),
             "1h": _read("1h", trend="RANGE"), "15m": _read("15m", trend="RANGE")}
    b = build_map("BTCUSDT", reads, CFG)["bias"]
    assert b["overall"] == "NEUTRAL"


# --------------------------------------------------------------- liquidity

def test_liquidity_targets_nearest_unswept_dedup():
    pools_1h = [_pool("PDH", 110, "BUYSIDE", 5),
                _pool("EQH", 105, "BUYSIDE", 4),
                _pool("EQL", 92, "SELLSIDE", 4, state="SWEPT",
                      post_sweep="REVERSED"),
                _pool("PDL", 90, "SELLSIDE", 5)]
    pools_4h = [_pool("PDH", 110.02, "BUYSIDE", 5)]      # dup of 1h PDH (dedup)
    reads = {"1h": _read("1h", pools=pools_1h), "4h": _read("4h", pools=pools_4h)}
    liq = build_map("BTCUSDT", reads, CFG)["liquidity"]
    above = [p["kind"] for p in liq["above"]]
    assert above == ["EQH", "PDH"]                        # nearest first, dedup'd
    assert liq["draw_above"]["kind"] == "PDH"             # highest priority draw
    assert [p["kind"] for p in liq["below"]] == ["PDL"]   # swept EQL excluded
    assert liq["swept_recent"][0]["kind"] == "EQL"
    assert liq["swept_recent"][0]["post_sweep"] == "REVERSED"


# ------------------------------------------------------------------ memory

def test_memory_sessions_sweeps_weekly():
    ctx = {"prev_day": {"high": 111, "low": 96},
           "sessions_today": {"ASIA": {"h": 103, "l": 98}},
           "prev_week": {"h": 120, "l": 80}}
    pools = [_pool("SESSION_H", 103, "BUYSIDE", 3, state="SWEPT",
                   session="ASIA", post_sweep="CONTINUED"),
             _pool("PDH", 111, "BUYSIDE", 5, state="SWEPT",
                   post_sweep="REVERSED")]
    reads = {"5m": _read("5m", close=100, pools=pools, context=ctx),
             "1d": _read("1d", context={"prev_week": {"h": 120, "l": 80}})}
    mem = build_memory("BTCUSDT", reads, CFG)
    assert mem["day_profile"]["prev_day"]["high"] == 111
    assert "ASIA high swept" in mem["session_model"]["swept"][0]
    outcomes = {s["kind"]: s["outcome"] for s in mem["sweep_history"]}
    assert outcomes == {"SESSION_H": "CONTINUED", "PDH": "REVERSED"}
    assert mem["weekly"]["position_in_week_range"] == 0.5     # 100 in 80..120


def test_memory_zone_history_orders_by_touches():
    zones = [_zone("1h", "DEMAND", 95, 96, state="TESTED", touches=2,
                   history=[{"ts": 1, "event": "e", "reason": "first touch (HELD)"}]),
             _zone("1h", "SR", 90, 91, state="WEAK", touches=4)]
    reads = {"1h": _read("1h", zones=zones)}
    mem = build_memory("BTCUSDT", reads, CFG)
    assert mem["zone_history"][0]["touches"] == 4
    assert mem["zone_history"][1]["held"] == 1


# ------------------------------------------------------------- determinism

def test_map_and_memory_deterministic():
    reads = {"1h": _read("1h", zones=[_zone("1h", "DEMAND", 95, 97)],
                         pools=[_pool("PDH", 110, "BUYSIDE", 5)]),
             "4h": _read("4h", trend="BULLISH")}
    a = json.dumps({"m": build_map("BTCUSDT", reads, CFG),
                    "y": build_memory("BTCUSDT", reads, CFG)}, sort_keys=True)
    b = json.dumps({"m": build_map("BTCUSDT", reads, CFG),
                    "y": build_memory("BTCUSDT", reads, CFG)}, sort_keys=True)
    assert a == b


def test_map_not_ready_without_reads():
    assert build_map("BTCUSDT", {"1h": None}, CFG)["ready"] is False

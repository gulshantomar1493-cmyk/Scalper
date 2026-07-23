"""V3 L4 Virtual Trader + L5 Session Timing — unit tests.

Crafted maps + 5m price paths prove: state derivation (WATCHING/ARMED/
TRIGGERED), the three confirmations, session gates (BLOCK / downgrade /
STRONG_ONLY / boost-as-confluence), R:R floor, honesty (avoid reasons),
and determinism.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from marketscalper.v3.config import V3Config
from marketscalper.v3.session import window_at
from marketscalper.v3.virtual_trader import build_trades

CFG = V3Config()
_IST = timezone(timedelta(hours=5, minutes=30))


def ist_ts(hour, minute=0, weekday_offset=0):
    """Epoch for a Monday (or +offset days) at IST hour:minute."""
    base = datetime(2026, 7, 20, tzinfo=_IST)          # a Monday
    dt = base + timedelta(days=weekday_offset, hours=hour, minutes=minute)
    return int(dt.timestamp())


def bars_path(prices, t0=None, step=300):
    t0 = t0 if t0 is not None else ist_ts(20)          # 20:00 IST = best window
    out = []
    for i, p in enumerate(prices):
        o, c = (prices[i - 1] if i else p), p
        out.append({"ts": t0 + i * step, "o": o,
                    "h": max(o, c) + 0.3, "l": min(o, c) - 0.3, "c": c})
    return out


def mk_map(zones=(), bias="BULLISH", pd_1h="DISCOUNT", above=(), below=()):
    return {"ready": True, "price": 100.0,
            "bias": {"overall": bias, "per_tf": {}},
            "zones": list(zones),
            "liquidity": {"above": list(above), "below": list(below),
                          "draw_above": None, "draw_below": None,
                          "swept_recent": []},
            "premium_discount": {"1h": pd_1h}}


def zone(lo, hi, stack=2, weight=3.0, kinds=("DEMAND",), states=("FRESH",),
         zid="map:0", side="BELOW"):
    return {"id": zid, "lo": lo, "hi": hi, "stack": stack, "weight": weight,
            "kinds": list(kinds), "side": side, "distance": 1.0,
            "explain": " + ".join(f"1h {k}(FRESH)" for k in kinds),
            "components": [{"tf": "1h", "kind": k, "state": s, "id": f"z{i}"}
                           for i, (k, s) in enumerate(zip(kinds, states))]}


def r5(atr=1.0, trend="BULLISH", events=(), pools=()):
    return {"ready": True, "atr": atr, "trend": trend,
            "structure": {"events": list(events)},
            "liquidity": list(pools)}


POOL_UP = {"kind": "PDH", "price": 106.0, "priority": 5, "side": "BUYSIDE",
           "tf": "1h", "session": None}


def run(m, reads, bars, cfg=CFG):
    return build_trades("BTCUSDT", m, {}, reads, bars, cfg)


# --------------------------------------------------------------- lifecycle

def test_rejection_wick_triggers_long_setup():
    z = zone(95.0, 96.0)
    # approach from above, spear the zone, reject back out (long lower wick)
    bars = bars_path([100, 99, 98, 97])
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 97.0, "h": 97.2,
                 "l": 94.6, "c": 96.6})                 # rejection bar
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 96.6, "h": 97.4,
                 "l": 96.4, "c": 97.2})
    out = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, bars)
    assert out["setups"], out
    s = out["setups"][0]
    assert s["direction"] == "LONG" and s["state"] == "TRIGGERED"
    assert s["entry"] == 95.5                            # zone 50%
    assert s["sl"] < 94.6                                # beyond the wick + pad
    assert s["tp1"] == 106.0 and s["rr"] >= CFG.min_rr_net
    assert s["confluences"] >= 2 and "of 7" in s["grade_reason"]
    assert s["reasons_to_avoid"]                         # honesty always present
    assert "session" in s and s["session"]["rating"] == 6


def test_watching_and_armed_states():
    z = zone(95.0, 96.0)
    near = bars_path([98.0, 97.6, 97.3])                # near, never touched
    out = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, near)
    assert not out["setups"]
    assert out["watching"] and out["watching"][0]["state"] == "WATCHING"
    touched = bars_path([98, 97, 96.4, 95.8, 95.9, 96.0])   # inside, no confirm
    out2 = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, touched)
    assert not out2["setups"]
    assert out2["watching"][0]["state"] == "ARMED"
    assert "confirmation" in out2["watching"][0]["trigger_hint"]
    assert out2["message"] and "watching" in out2["message"]


def test_choch_confirmation_path():
    z = zone(95.0, 96.0)
    # stays INSIDE the zone (no rejection/engulfing possible: close never > hi)
    bars = bars_path([100, 98, 96.5, 95.5, 95.6, 95.55, 95.7])
    choch_ts = bars[6]["ts"]
    reads = {"5m": r5(events=[{"kind": "CHOCH", "direction": "UP",
                               "ts": choch_ts, "displaced": False}])}
    out = run(mk_map([z], above=[POOL_UP]), reads, bars)
    assert out["setups"] and out["setups"][0]["direction"] == "LONG"
    assert any("CHOCH" in f for f in out["setups"][0]["reasons"])


def test_short_mirror_at_supply():
    z = zone(104.0, 105.0, kinds=("SUPPLY",), side="ABOVE", zid="map:1")
    bars = bars_path([100, 102, 103.5])
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 103.5, "h": 105.4,
                 "l": 103.3, "c": 103.6})               # upper-wick rejection
    pool_dn = {"kind": "PDL", "price": 94.0, "priority": 5, "side": "SELLSIDE",
               "tf": "1h", "session": None}
    out = run(mk_map([z], bias="BEARISH", pd_1h="PREMIUM", below=[pool_dn]),
              {"5m": r5(trend="BEARISH")}, bars)
    assert out["setups"] and out["setups"][0]["direction"] == "SHORT"
    assert out["setups"][0]["sl"] > 105.4


# ------------------------------------------------------------ gates & honesty

def test_wrong_half_blocks_reversal():
    z = zone(95.0, 96.0)
    bars = bars_path([100, 98, 96.5])
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 96.5, "h": 96.7,
                 "l": 94.6, "c": 96.6})
    out = run(mk_map([z], pd_1h="PREMIUM", above=[POOL_UP]), {"5m": r5()}, bars)
    assert not out["setups"]
    assert any("premium" in w["trigger_hint"] for w in out["watching"])


def test_rr_floor_blocks_thin_geometry():
    z = zone(95.0, 96.0)
    near_pool = {"kind": "EQH", "price": 96.6, "priority": 4, "side": "BUYSIDE",
                 "tf": "5m", "session": None}
    bars = bars_path([100, 98, 96.5])
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 96.5, "h": 96.7,
                 "l": 94.6, "c": 96.6})
    out = run(mk_map([z], above=[near_pool]), {"5m": r5()}, bars)
    assert not out["setups"]
    assert any("R:R" in w["trigger_hint"] for w in out["watching"])


def test_block_window_suppresses_setups():
    z = zone(95.0, 96.0)
    t0 = ist_ts(4)                                     # 04:00 IST = dead zone
    bars = bars_path([100, 98, 96.5], t0=t0)
    bars.append({"ts": t0 + 3 * 300, "o": 96.5, "h": 96.7, "l": 94.6, "c": 96.6})
    out = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, bars)
    assert not out["setups"]
    assert "session blocked" in out["message"]


def test_lunch_window_downgrades_grade():
    z = zone(95.0, 96.0, stack=3, weight=4.0,
             kinds=("DEMAND", "ORDER_BLOCK", "TRENDLINE"),
             states=("FRESH", "FRESH", "FRESH"))
    t0 = ist_ts(12)                                    # 12:00 IST lunch chop
    bars = bars_path([100, 98, 96.5], t0=t0)
    bars.append({"ts": t0 + 3 * 300, "o": 96.5, "h": 96.7, "l": 94.6, "c": 96.6})
    out = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, bars)
    assert out["setups"]
    s = out["setups"][0]
    assert s["grade"] in ("A", "B")                     # downgraded from A+/A
    assert any("session" in a.lower() for a in s["reasons_to_avoid"])


def test_boost_window_counts_as_confluence():
    z = zone(95.0, 96.0, stack=1, weight=1.0, kinds=("SR",))
    bars = bars_path([100, 98, 96.5])                  # 20:00 IST overlap
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 96.5, "h": 96.7,
                 "l": 94.6, "c": 96.6})
    out = run(mk_map([z], above=[POOL_UP]), {"5m": r5()}, bars)
    assert out["setups"]
    assert any("session" in f for f in out["setups"][0]["reasons"])


# ------------------------------------------------------------------ session

def test_session_windows_verbatim():
    assert window_at(ist_ts(20))["rating"] == 6                 # overlap: best
    assert window_at(ist_ts(20))["effect"] == "BOOST"
    assert window_at(ist_ts(4))["effect"] == "BLOCK"            # 03:30-05:30
    assert window_at(ist_ts(2, 30))["effect"] == "BLOCK"        # 02:00-03:30
    assert window_at(ist_ts(12))["effect"] == "WARN_DOWNGRADE"  # lunch
    assert window_at(ist_ts(1))["effect"] == "STRONG_ONLY"      # 00:30-02:00
    assert window_at(ist_ts(15))["effect"] == "BOOST"           # London open
    assert window_at(ist_ts(6))["effect"] == "NORMAL"           # Tokyo
    sun = window_at(ist_ts(20, weekday_offset=6))               # Sunday
    assert sun["sunday"] and sun["effect"] == "WARN_DOWNGRADE"


# ------------------------------------------------------------- determinism

def test_trader_deterministic():
    z = zone(95.0, 96.0)
    bars = bars_path([100, 98, 96.5])
    bars.append({"ts": bars[-1]["ts"] + 300, "o": 96.5, "h": 96.7,
                 "l": 94.6, "c": 96.6})
    m = mk_map([z], above=[POOL_UP])
    a = json.dumps(run(m, {"5m": r5()}, bars), sort_keys=True)
    b = json.dumps(run(m, {"5m": r5()}, bars), sort_keys=True)
    assert a == b

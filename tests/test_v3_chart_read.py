"""V3 L1 Chart Read Engine — unit tests over hand-built candle vectors.

Engine contract: docs/V3/DOMAIN-MODEL.md. Pure fold — determinism is asserted
directly (same candles → identical payload).
"""

from __future__ import annotations

import json

from marketscalper.v3.chart_read import ChartReadEngine
from marketscalper.v3.config import V3Config

T0 = 1_700_000_000 - (1_700_000_000 % 86400)      # a UTC midnight


def mk(i, o, h, l, c, tf_s=3600):
    return {"ts": T0 + i * tf_s, "o": o, "h": h, "l": l, "c": c, "v": 100}


def flat(i, px, tf_s=3600, wig=0.0):
    return mk(i, px, px + wig + 0.4, px - wig - 0.4, px, tf_s)


def read(candles, **cfg_kw):
    cfg = V3Config(**cfg_kw) if cfg_kw else V3Config()
    return ChartReadEngine("BTCUSDT", "1h", cfg).read(candles)


def stairs_up(n, start=100.0):
    """Rising 5-bar waves (+2,+2,+2,−1,−1): clean HH/HL swings. Direction-change
    bars GAP 0.15 against the prior extreme so fractal strict-inequalities hold
    (no tied highs/lows)."""
    deltas = [2.0, 2.0, 2.0, -1.0, -1.0]
    closes, px = [], start
    for i in range(n):
        px += deltas[i % 5]
        closes.append(px)
    out = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else start
        o = prev + (0.15 if c > prev else -0.15)
        out.append(mk(i, o, max(o, c) + 0.3, min(o, c) - 0.3, c))
    return out


# ------------------------------------------------------------- swings/trend

def test_swings_and_bullish_trend():
    r = read(stairs_up(80))
    assert r["bars"] == 80 and r["atr"] is not None
    kinds = {s["kind"] for s in r["swings"]}
    assert kinds == {"HIGH", "LOW"}
    labels = [s["label"] for s in r["swings"] if s["label"]]
    assert "HH" in labels and "HL" in labels
    assert r["trend"] == "BULLISH"


def test_bearish_trend_mirror():
    ups = stairs_up(80)
    hi = max(c["h"] for c in ups)
    inv = [{"ts": c["ts"], "o": hi - c["o"] + 100, "h": hi - c["l"] + 100,
            "l": hi - c["h"] + 100, "c": hi - c["c"] + 100, "v": 100}
           for c in ups]
    r = read(inv)
    assert r["trend"] == "BEARISH"
    labels = [s["label"] for s in r["swings"] if s["label"]]
    assert "LL" in labels and "LH" in labels


# ---------------------------------------------------------------- structure

def test_bos_and_choch_events():
    # up structure, then a hard break down through the last swing low
    c = stairs_up(40)
    last = c[-1]["c"]
    for i in range(40, 46):
        c.append(mk(i, last, last + 0.2, last - 6.0, last - 5.5))
        last -= 5.5
    r = read(c)
    kinds = [e["kind"] for e in r["structure"]["events"]]
    assert "BOS" in kinds                        # with-trend breaks on the way up
    assert r["structure"]["last_choch"] is not None   # the down break = CHOCH
    assert r["structure"]["last_choch"]["direction"] == "DOWN"


# -------------------------------------------------------------------- zones

def test_demand_zone_from_base_and_impulse():
    c = [flat(i, 100.0) for i in range(30)]                 # quiet base
    c.append(mk(30, 100.0, 112.0, 99.8, 111.5))             # impulse up
    for i in range(31, 36):
        c.append(flat(i, 111.0))
    r = read(c)
    demand = [z for z in r["zones"] if z["kind"] == "DEMAND"]
    assert demand, r["zones"]
    z = demand[0]
    assert z["lo"] <= 100.0 <= z["hi"] and z["state"] in ("FRESH", "TESTED")
    assert "impulse" in z["origin"]


def test_zone_lifecycle_touch_then_break_and_roleflip():
    c = [flat(i, 100.0) for i in range(30)]
    c.append(mk(30, 100.0, 112.0, 99.8, 111.5))             # demand born
    for i in range(31, 40):
        c.append(flat(i, 111.0))
    c.append(mk(40, 100.6, 100.7, 100.1, 100.3))            # small-body touch (HELD)
    c.append(mk(41, 100.3, 108.0, 100.2, 107.5))
    for i in range(42, 48):
        c.append(flat(i, 107.0))
    c.append(mk(48, 107.0, 107.2, 88.0, 89.0))              # displaced close through
    c.append(flat(49, 89.0))
    r = read(c)
    states = {z["id"]: z for z in r["zones"]}
    broken = [z for z in r["zones"] if z["kind"] == "DEMAND" and z["state"] == "BROKEN"]
    flipped = [z for z in r["zones"] if z["flipped_from"]]
    assert broken, r["zones"]
    assert broken[0]["touches"] >= 1                        # the held touch counted
    assert flipped and flipped[0]["kind"] == "SUPPLY"       # role-flip born
    # self-explanation present
    assert any("created" == h["event"] for h in flipped[0]["history"])


def test_fvg_zone_detected():
    c = [flat(i, 100.0) for i in range(20)]
    c.append(mk(20, 100.0, 101.0, 99.5, 100.8))             # c1 h=101
    c.append(mk(21, 101.0, 106.0, 100.9, 105.8))            # big body
    c.append(mk(22, 105.8, 107.0, 103.5, 106.5))            # c3 l=103.5 > 101
    for i in range(23, 27):
        c.append(flat(i, 106.0))
    r = read(c)
    assert any(z["kind"] == "FVG" for z in r["zones"])


def test_order_block_on_displaced_break():
    c = stairs_up(30)
    # a red candle then a huge displacement up through the last swing high
    top = max(x["h"] for x in c)
    last = c[-1]["c"]
    c.append(mk(30, last, last + 0.2, last - 1.5, last - 1.2))       # down candle
    c.append(mk(31, last - 1.2, top + 8.0, last - 1.4, top + 7.5))   # displaced break
    for i in range(32, 36):
        c.append(flat(i, top + 7.0))
    r = read(c)
    obs = [z for z in r["zones"] if z["kind"] == "ORDER_BLOCK"]
    assert obs and "displaced" in obs[0]["origin"]


# ---------------------------------------------------------------- liquidity

def test_equal_highs_pool_and_sweep():
    c = []
    i = 0
    for _ in range(16):                    # ATR warmup (pools need a warm ATR)
        c.append(mk(i, 100, 100.5, 99.5, 100)); i += 1
    for _ in range(2):                     # two equal highs at 110
        for _ in range(3):
            c.append(mk(i, 100, 100.5, 99.5, 100)); i += 1
        c.append(mk(i, 100, 110.0, 99.8, 100.2)); i += 1
        for _ in range(3):
            c.append(mk(i, 100, 100.5, 99.5, 100)); i += 1
    for _ in range(6):
        c.append(mk(i, 100, 100.5, 99.5, 100)); i += 1
    r = read(c)
    eqh = [p for p in r["liquidity"] if p["kind"] == "EQH"]
    assert eqh and eqh[0]["state"] == "UNSWEPT" and eqh[0]["side"] == "BUYSIDE"
    assert eqh[0]["priority"] == 4
    # now sweep it
    c.append(mk(i, 100, 111.5, 99.9, 100.4)); i += 1
    c.append(mk(i, 100, 100.5, 99.5, 100)); i += 1
    r2 = read(c)
    eqh2 = [p for p in r2["liquidity"] if p["kind"] == "EQH"]
    assert eqh2 and eqh2[0]["state"] == "SWEPT" and eqh2[0]["swept_at"] is not None


def test_pdh_pdl_pools_next_day():
    day = 86400
    c = []
    for i in range(24):                                    # day 1 (1h bars)
        c.append(mk(i, 100, 105 if i == 10 else 101, 95 if i == 4 else 99, 100))
    for i in range(24, 30):                                # day 2
        c.append(mk(i, 100, 101, 99, 100))
    r = read(c)
    kinds = {p["kind"]: p for p in r["liquidity"]}
    assert "PDH" in kinds and "PDL" in kinds
    assert kinds["PDH"]["price"] == 105 and kinds["PDH"]["priority"] == 5
    assert kinds["PDL"]["price"] == 95


# ------------------------------------------------------------ trendlines/PD

def test_rising_support_trendline():
    c = stairs_up(90)
    r = read(c)
    sup = [t for t in r["trendlines"] if t["side"] == "SUPPORT"]
    assert sup, r["trendlines"]
    t = sup[0]
    assert t["touches"] >= 2 and t["state"] in ("NEW", "VALID", "STRONG")
    assert t["a"]["price"] < t["b"]["price"]               # rising
    # endpoints ready for direct chart drawing
    assert t["a"]["ts"] < t["b"]["ts"]


def test_premium_discount():
    c = stairs_up(60)                                       # price ends near top
    r = read(c)
    assert r["dealing_range"] is not None
    assert r["premium_discount"] in ("PREMIUM", "DISCOUNT")
    lows = [flat(i, 50.0) for i in range(60, 66)]
    r2 = read(c + [dict(x, ts=T0 + (60 + n) * 3600) for n, x in enumerate(lows)])
    assert r2["premium_discount"] == "DISCOUNT"


# ------------------------------------------------------------- determinism

def test_fold_is_deterministic():
    c = stairs_up(120)
    a = json.dumps(read(c), sort_keys=True)
    b = json.dumps(read(c), sort_keys=True)
    assert a == b


def test_history_reasons_everywhere():
    c = [flat(i, 100.0) for i in range(30)]
    c.append(mk(30, 100.0, 112.0, 99.8, 111.5))
    for i in range(31, 36):
        c.append(flat(i, 111.0))
    r = read(c)
    for z in r["zones"]:
        assert z["history"], z                              # never silent
        assert all(h["reason"] for h in z["history"])

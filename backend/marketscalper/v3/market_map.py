"""V3 L2 Market Map + L3 Market Memory — the merged battlefield.

Pure functions over the per-TF Chart Reads (L1 payloads). No I/O, no clock,
no randomness: build_map(reads) is deterministic, so the map is replay-safe.

L2 MarketMap: stacked multi-TF zones (overlapping zones merge with a tf_stack
and an explainable weight), the structure-only bias ladder, ranked unswept
liquidity targets above/below price, and the ordered decision points.

L3 MarketMemory: what a trader remembers — day profile, session model
(Asia→London→NY, which side's fuel is spent), the weekly frame, recent zone
reactions and how recent sweeps resolved. Memory only informs weights/context;
it NEVER invents a setup.
"""

from __future__ import annotations

from marketscalper.v3.config import V3Config, DEFAULT

_TF_RANK = {"5m": 0, "15m": 1, "1h": 2, "4h": 3, "1d": 4}
_LIVE_ZONE_STATES = ("FRESH", "TESTED", "WEAK")


# ------------------------------------------------------------------ L2 map

def _atr_of(reads: dict, tf: str) -> float:
    r = reads.get(tf) or {}
    return float(r.get("atr") or 0.0)


def _collect_zones(reads: dict) -> list[dict]:
    out = []
    for tf, r in reads.items():
        if not r or not r.get("ready", True):
            continue
        for z in (r.get("zones") or []):
            if z["state"] in _LIVE_ZONE_STATES:
                out.append({**z, "tf": tf, "rank": _TF_RANK.get(tf, 0)})
    return out


def _merge_zones(zones: list[dict], reads: dict, cfg: V3Config) -> list[dict]:
    """Greedy band-merge: overlapping (or near, ≤0.3×ATR of the higher TF)
    zones across TFs become ONE map-zone with a tf_stack + weight."""
    zs = sorted(zones, key=lambda z: (z["lo"], z["hi"]))
    merged: list[dict] = []
    for z in zs:
        if merged:
            cur = merged[-1]
            hi_tf = max([c["tf"] for c in cur["components"]] + [z["tf"]],
                        key=lambda t: _TF_RANK.get(t, 0))
            atr_hi = _atr_of(reads, hi_tf)
            tol = atr_hi * cfg.map_merge_atr
            new_lo = min(cur["lo"], z["lo"])
            new_hi = max(cur["hi"], z["hi"])
            # merge only when near AND the result stays a tradeable band —
            # never chain zones into a mega-zone wider than 1.5×ATR(higher tf)
            if z["lo"] <= cur["hi"] + tol and \
                    (new_hi - new_lo) <= atr_hi * cfg.map_max_width_atr:
                cur["components"].append(z)
                cur["lo"], cur["hi"] = new_lo, new_hi
                continue
        merged.append({"lo": z["lo"], "hi": z["hi"], "components": [z]})
    out = []
    for i, m in enumerate(merged):
        comps = m["components"]
        tfs = sorted({c["tf"] for c in comps}, key=lambda t: -_TF_RANK.get(t, 0))
        fresh = sum(1 for c in comps if c["state"] == "FRESH")
        weight = round(len(tfs) + fresh * cfg.map_fresh_bonus, 2)
        out.append({
            "id": f"map:{i}",
            "lo": round(m["lo"], 2), "hi": round(m["hi"], 2),
            "tf_stack": tfs, "stack": len(tfs), "weight": weight,
            "kinds": sorted({c["kind"] for c in comps}),
            "components": [{"tf": c["tf"], "kind": c["kind"],
                            "state": c["state"], "id": c["id"]} for c in comps],
            "explain": " + ".join(
                f"{c['tf']} {c['kind']}({c['state']})" for c in comps),
        })
    return out


def _bias_ladder(reads: dict, cfg: V3Config) -> dict:
    per_tf = {tf: (reads.get(tf) or {}).get("trend") or "RANGE"
              for tf in reads}
    bull = sum(w for tf, w in cfg.bias_weights.items()
               if per_tf.get(tf) == "BULLISH")
    bear = sum(w for tf, w in cfg.bias_weights.items()
               if per_tf.get(tf) == "BEARISH")
    total = sum(cfg.bias_weights.values())
    # decisive only: the winner needs a real share AND a real margin — a lone
    # timeframe against another is a conflict, not a bias.
    def _decisive(w, o):
        return w > o and w >= total * cfg.bias_min_share and \
               (w - o) >= total * cfg.bias_min_margin
    overall = "BULLISH" if _decisive(bull, bear) else \
              "BEARISH" if _decisive(bear, bull) else "NEUTRAL"
    return {"per_tf": per_tf, "overall": overall,
            "bull_weight": bull, "bear_weight": bear,
            "explain": f"structure votes — bull {bull} vs bear {bear} of {total}"}


def _liquidity_targets(reads: dict, price: float, cfg: V3Config) -> dict:
    seen, pools = set(), []
    for tf in sorted(reads, key=lambda t: -_TF_RANK.get(t, 0)):   # higher TF first
        for p in ((reads.get(tf) or {}).get("liquidity") or []):
            key = (p["kind"], round(p["price"], 1))
            if key in seen:
                continue                      # PDH etc. repeats on every TF read
            seen.add(key)
            pools.append({**p, "tf": tf})
    above = sorted([p for p in pools if p["state"] == "UNSWEPT"
                    and p["price"] > price],
                   key=lambda p: (p["price"]))[:cfg.map_max_targets]
    below = sorted([p for p in pools if p["state"] == "UNSWEPT"
                    and p["price"] < price],
                   key=lambda p: (-p["price"]))[:cfg.map_max_targets]
    def slim(p):
        return {"kind": p["kind"], "price": p["price"], "priority": p["priority"],
                "side": p["side"], "tf": p["tf"], "session": p.get("session")}
    draw_up = max(above, key=lambda p: p["priority"], default=None)
    draw_dn = max(below, key=lambda p: p["priority"], default=None)
    return {"above": [slim(p) for p in above], "below": [slim(p) for p in below],
            "draw_above": slim(draw_up) if draw_up else None,
            "draw_below": slim(draw_dn) if draw_dn else None,
            "swept_recent": [slim(p) | {"post_sweep": p.get("post_sweep")}
                             for p in pools if p["state"] == "SWEPT"][:8]}


def build_map(symbol: str, reads: dict, cfg: V3Config = DEFAULT) -> dict:
    """L2: the merged battlefield for one symbol. `reads` = {tf: L1 payload}."""
    ready = {tf: r for tf, r in reads.items() if r and r.get("ready", True)}
    price = None
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        if tf in ready and ready[tf].get("last_close") is not None:
            price = float(ready[tf]["last_close"])
            break
    if price is None:
        return {"symbol": symbol, "ready": False, "reason": "no reads"}

    zones = _merge_zones(_collect_zones(ready), ready, cfg)
    for z in zones:                            # side + distance vs price
        if z["lo"] <= price <= z["hi"]:
            z["side"], z["distance"] = "AT_PRICE", 0.0
        elif z["lo"] > price:
            z["side"], z["distance"] = "ABOVE", round(z["lo"] - price, 2)
        else:
            z["side"], z["distance"] = "BELOW", round(price - z["hi"], 2)
    zones.sort(key=lambda z: (-z["weight"], z["distance"]))
    decision = sorted([z for z in zones], key=lambda z: z["distance"])

    return {
        "symbol": symbol,
        "ready": True,
        "price": price,
        "bias": _bias_ladder(ready, cfg),
        "zones": zones[:cfg.map_max_zones],
        "decision_points": [
            {"id": z["id"], "side": z["side"], "distance": z["distance"],
             "lo": z["lo"], "hi": z["hi"], "weight": z["weight"],
             "explain": z["explain"]}
            for z in decision[:cfg.map_max_zones]],
        "liquidity": _liquidity_targets(ready, price, cfg),
        "premium_discount": {tf: ready[tf].get("premium_discount")
                             for tf in ready},
    }


# --------------------------------------------------------------- L3 memory

def build_memory(symbol: str, reads: dict, cfg: V3Config = DEFAULT) -> dict:
    """L3: rolling trader memory, derived deterministically from the reads.
    Informs weights/context only — never creates a setup."""
    low_tf = next((reads[t] for t in ("5m", "15m", "1h") if reads.get(t)
                   and reads[t].get("ready", True)), None) or {}
    day_tf = next((reads[t] for t in ("1d", "4h", "1h") if reads.get(t)
                   and reads[t].get("ready", True)), None) or {}
    ctx = low_tf.get("context") or {}
    dctx = day_tf.get("context") or {}

    # session model: which session's liquidity is already spent?
    swept_sessions = []
    for tf, r in reads.items():
        if not r or not r.get("ready", True):
            continue
        for p in (r.get("liquidity") or []):
            if p.get("session") and p["state"] == "SWEPT":
                swept_sessions.append(
                    f"{p['session']} {'high' if p['kind'] == 'SESSION_H' else 'low'} swept")
    swept_sessions = sorted(set(swept_sessions))

    # sweep outcomes (resolved only) — the raid statistics a trader remembers
    sweep_hist = []
    seen = set()
    for tf, r in (reads or {}).items():
        for p in ((r or {}).get("liquidity") or []):
            if p["state"] == "SWEPT" and p.get("post_sweep") in ("REVERSED", "CONTINUED"):
                key = (p["kind"], round(p["price"], 1))
                if key not in seen:
                    seen.add(key)
                    sweep_hist.append({"kind": p["kind"], "tf": tf,
                                       "outcome": p["post_sweep"]})

    # zone reactions: which zones recently HELD vs got PIERCED
    zone_hist = []
    for tf, r in (reads or {}).items():
        for z in ((r or {}).get("zones") or []):
            if z.get("touches"):
                held = sum(1 for h in z.get("history", [])
                           if "HELD" in str(h.get("reason", "")))
                zone_hist.append({"tf": tf, "kind": z["kind"],
                                  "state": z["state"], "touches": z["touches"],
                                  "held": held})
    zone_hist.sort(key=lambda x: -x["touches"])

    weekly = dctx.get("prev_week") or ctx.get("prev_week")
    pos = None
    price = low_tf.get("last_close") or day_tf.get("last_close")
    if weekly and price and weekly.get("h") is not None and \
            weekly["h"] != weekly["l"]:
        pos = round((price - weekly["l"]) / (weekly["h"] - weekly["l"]), 3)

    return {
        "symbol": symbol,
        "day_profile": {
            "prev_day": ctx.get("prev_day") or dctx.get("prev_day"),
            "sessions_today": ctx.get("sessions_today") or {},
        },
        "session_model": {
            "swept": swept_sessions,
            "note": ("liquidity spent on: " + "; ".join(swept_sessions))
                    if swept_sessions else "no session liquidity taken yet",
        },
        "weekly": {"prev_week": weekly, "position_in_week_range": pos},
        "sweep_history": sweep_hist[:12],
        "zone_history": zone_hist[:10],
    }

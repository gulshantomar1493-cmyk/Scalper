"""V3 L4 — the Virtual Trader: watch the map, wait at the zones, confirm, issue.

Pure deterministic function over (market map, memory, 5m read, 5m candles,
session window). The conceptual state machine WATCHING → ARMED → TRIGGERED is
derived from the recent price path against each map-zone — same inputs, same
states, replay-safe. It never predicts: a setup exists only after price REACHED
a mapped zone and price action CONFIRMED there.

Setups follow the frozen field names of the old setup card (direction, grade,
entry, sl, tp1, tp2, rr, reasons, ...) so the existing UI renders them as-is.
Grade = a confluence count out of 7 named factors — never a probability.
"""

from __future__ import annotations

from datetime import datetime, timezone

from marketscalper.v3.config import V3Config, DEFAULT
from marketscalper.v3.session import window_at

_LONG, _SHORT = "LONG", "SHORT"


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _net_rr(entry: float, sl: float, tp: float, cfg: V3Config) -> float | None:
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    fee = entry * cfg.taker_fee * 2.0
    net = (abs(tp - entry) - fee) / (risk + fee)
    return round(net, 2) if net > 0 else None


def _bar_body(b) -> float:
    return abs(b["c"] - b["o"])


def _lower_wick(b) -> float:
    return min(b["o"], b["c"]) - b["l"]


def _upper_wick(b) -> float:
    return b["h"] - max(b["o"], b["c"])


def _rng(b) -> float:
    return max(b["h"] - b["l"], 1e-9)


class _Confirm:
    def __init__(self, kind: str, bar: dict, displaced: bool):
        self.kind, self.bar, self.displaced = kind, bar, displaced


def _find_breakout(zone: dict, bars: list, atr: float, cfg: V3Config):
    """Breakout pattern over the recent path: a DISPLACED close through the
    zone, then a retest that HOLDS the broken level. Returns
    (direction, break_bar_i, retest_bar_i) or a watching hint string."""
    if atr <= 0:
        return None
    brk_i = brk_dir = None
    for i, b in enumerate(bars):
        if abs(b["c"] - b["o"]) < cfg.breakout_body_atr * atr:
            continue
        # the displaced candle must actually travel THROUGH the band
        if b["c"] > zone["hi"] and b["o"] < zone["hi"]:
            brk_i, brk_dir = i, _LONG        # up through resistance-side zone
        elif b["c"] < zone["lo"] and b["o"] > zone["lo"]:
            brk_i, brk_dir = i, _SHORT       # down through support-side zone
    if brk_i is None:
        return None
    if (len(bars) - 1 - brk_i) > cfg.breakout_max_age_bars:
        return None                          # stale break, momentum spent
    level = zone["hi"] if brk_dir == _LONG else zone["lo"]
    tol = cfg.breakout_retest_tol_atr * atr
    for j in range(brk_i + 1, len(bars)):
        b = bars[j]
        touched = (b["l"] <= level + tol) if brk_dir == _LONG else \
                  (b["h"] >= level - tol)
        held = (b["c"] > level) if brk_dir == _LONG else (b["c"] < level)
        failed = (b["c"] < zone["lo"]) if brk_dir == _LONG else \
                 (b["c"] > zone["hi"])
        if failed:
            return None                      # back inside — the break failed
        if touched and held:
            return (brk_dir, brk_i, j)
    return (brk_dir, brk_i, None)            # broke, no retest yet → watch


def _find_confirmation(direction: str, zone: dict, bars: list, start_i: int,
                       choch_ts: set, atr: float, cfg: V3Config) -> _Confirm | None:
    """First confirmation AFTER the zone touch — rejection wick, engulfing,
    or a 5m CHOCH toward the trade."""
    for i in range(start_i, len(bars)):
        b = bars[i]
        displaced = atr > 0 and _bar_body(b) > 1.2 * atr
        if direction == _LONG:
            rejected = (b["l"] <= zone["hi"] and b["c"] > zone["hi"]
                        and _lower_wick(b) >= cfg.rejection_wick_frac * _rng(b))
            engulfed = (i > 0 and b["c"] > bars[i - 1]["h"]
                        and _bar_body(b) > _bar_body(bars[i - 1])
                        and b["c"] > zone["hi"])
        else:
            rejected = (b["h"] >= zone["lo"] and b["c"] < zone["lo"]
                        and _upper_wick(b) >= cfg.rejection_wick_frac * _rng(b))
            engulfed = (i > 0 and b["c"] < bars[i - 1]["l"]
                        and _bar_body(b) > _bar_body(bars[i - 1])
                        and b["c"] < zone["lo"])
        if rejected:
            return _Confirm("rejection wick", b, displaced)
        if engulfed:
            return _Confirm("engulfing close", b, displaced)
        if b["ts"] in choch_ts:
            return _Confirm("5m CHOCH", b, displaced)
    return None


def _targets(direction: str, entry: float, mkt_map: dict) -> tuple:
    """TP1 = nearest unswept opposing pool beyond entry; TP2 = the next map-zone
    edge (or second pool) beyond TP1."""
    liq = mkt_map.get("liquidity") or {}
    pools = liq.get("above" if direction == _LONG else "below") or []
    ahead = [p for p in pools if (p["price"] > entry) == (direction == _LONG)]
    if not ahead:
        return None, None, None
    tp1p = ahead[0]
    tp1 = tp1p["price"]
    tp2 = None
    for z in mkt_map.get("zones") or []:
        edge = z["lo"] if direction == _LONG else z["hi"]
        if (direction == _LONG and edge > tp1) or \
           (direction == _SHORT and edge < tp1):
            tp2 = edge
            break
    if tp2 is None and len(ahead) > 1:
        tp2 = ahead[1]["price"]
    return tp1, tp2, tp1p


def _sweep_fuel(zone: dict, reads: dict, last_ts: int, atr: float,
                cfg: V3Config):
    """A high-priority pool swept recently, INTO the zone band → reversal fuel."""
    span = cfg.sweep_into_zone_bars * 300
    pad = 0.3 * atr
    for tf in ("5m", "15m"):
        for p in ((reads.get(tf) or {}).get("liquidity") or []):
            if p["state"] == "SWEPT" and p.get("swept_at") and \
                    (last_ts - p["swept_at"]) <= span and \
                    (zone["lo"] - pad) <= p["price"] <= (zone["hi"] + pad) and \
                    p["priority"] >= 4:
                return p
    return None


def build_trades(symbol: str, mkt_map: dict, memory: dict, reads: dict,
                 bars5: list[dict], cfg: V3Config = DEFAULT) -> dict:
    """The trader's pass: state per map-zone from the recent 5m path, then
    confirmed setups. Returns {session, setups, watching, message}."""
    if not bars5 or not mkt_map.get("ready"):
        return {"symbol": symbol, "session": None, "setups": [],
                "watching": [], "message": "market map not ready"}
    bars = bars5[-cfg.confirm_bars:]
    last = bars[-1]
    price = last["c"]
    session = window_at(last["ts"], cfg)
    r5 = reads.get("5m") or {}
    atr = float(r5.get("atr") or 0.0)
    choch_up = {e["ts"] for e in (r5.get("structure") or {}).get("events", [])
                if e["kind"] == "CHOCH" and e["direction"] == "UP"}
    choch_dn = {e["ts"] for e in (r5.get("structure") or {}).get("events", [])
                if e["kind"] == "CHOCH" and e["direction"] == "DOWN"}
    pd_1h = (mkt_map.get("premium_discount") or {}).get("1h") or \
            (mkt_map.get("premium_discount") or {}).get("15m")
    bias = (mkt_map.get("bias") or {}).get("overall", "NEUTRAL")

    setups, watching = [], []

    stalk = mkt_map.get("decision_points") or mkt_map.get("zones") or []
    for zone in stalk:
        if zone["side"] == "AT_PRICE" and (zone["hi"] - zone["lo"]) > 3 * atr > 0:
            continue                              # broad HTF area, not an entry band
        # --- how did price interact with this zone in the recent path? ---
        touch_i = None
        for i, b in enumerate(bars):
            if b["l"] <= zone["hi"] and b["h"] >= zone["lo"]:
                touch_i = i
                break
        if touch_i is None:
            dist = zone["lo"] - price if zone["lo"] > price else price - zone["hi"]
            if atr > 0 and 0 <= dist <= cfg.watch_dist_atr * atr:
                direction = _SHORT if zone["lo"] > price else _LONG
                watching.append({
                    "zone_id": zone["id"], "state": "WATCHING",
                    "direction": direction, "lo": zone["lo"], "hi": zone["hi"],
                    "distance": round(dist, 2), "weight": zone["weight"],
                    "explain": zone["explain"],
                    "trigger_hint": f"price reaches the zone, then a 5m "
                                    f"rejection / engulfing / CHOCH "
                                    f"{'down' if direction == _SHORT else 'up'}"})
            continue

        # ---- BREAKOUT / BREAKDOWN first: a displaced close THROUGH the zone
        # means the zone is no longer a reversal candidate — it is a level that
        # just changed hands. Trend sessions live on this pattern.
        bo = _find_breakout(zone, bars, atr, cfg)
        if bo is not None:
            bo_dir, brk_i, retest_i = bo
            level = zone["hi"] if bo_dir == _LONG else zone["lo"]
            if retest_i is None:
                watching.append({
                    "zone_id": zone["id"], "state": "ARMED", "direction": bo_dir,
                    "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                    "weight": zone["weight"], "explain": zone["explain"],
                    "trigger_hint": f"displaced break through {round(level, 2)} — "
                                    f"awaiting the retest-hold"})
                continue
            if session["effect"] == "BLOCK":
                continue
            brk_bar = bars[brk_i]
            strong = atr > 0 and abs(brk_bar["c"] - brk_bar["o"]) >= 1.5 * atr
            aligned = (bias == "BULLISH" and bo_dir == _LONG) or \
                      (bias == "BEARISH" and bo_dir == _SHORT)
            with_t5 = (r5.get("trend") == "BULLISH" and bo_dir == _LONG) or \
                      (r5.get("trend") == "BEARISH" and bo_dir == _SHORT)
            factors = []
            if aligned:
                factors.append(f"HTF bias {bias.lower()} — with the ladder")
            if zone["stack"] >= 2:
                factors.append(f"{zone['stack']}-TF level stack ({zone['explain']})")
            if any(c["state"] == "FRESH" for c in zone.get("components", [])):
                factors.append("first break of a FRESH level")
            if "TRENDLINE" in (zone.get("kinds") or []):
                factors.append("trendline break in the level")
            if strong:
                factors.append("strong displacement break (≥1.5×ATR body)")
            if with_t5:
                factors.append("5m trend already flowing with the break")
            if session["effect"] == "BOOST":
                factors.append(f"session: {session['label']} (trend window)")
            n = len(factors)
            if n < cfg.min_issue_confluences:
                watching.append({
                    "zone_id": zone["id"], "state": "ARMED", "direction": bo_dir,
                    "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                    "weight": zone["weight"], "explain": zone["explain"],
                    "trigger_hint": f"retest held but only {n} of 7 confluences — "
                                    f"below the {cfg.min_issue_confluences}-factor floor"})
                continue
            grade = "A+" if n >= cfg.grade_a_plus else "A"
            if session["effect"] == "WARN_DOWNGRADE":
                grade = {"A+": "A", "A": None}.get(grade)
                if grade is None:
                    continue
            entry = round(level, 2)
            pad = cfg.sl_pad_atr * atr
            sl = round((zone["lo"] - pad) if bo_dir == _LONG
                       else (zone["hi"] + pad), 2)
            tp1, tp2, tp1_pool = _targets(bo_dir, entry, mkt_map)
            if tp1 is None:
                continue
            rr = _net_rr(entry, sl, tp1, cfg)
            if rr is None or rr < cfg.min_rr_net:
                watching.append({
                    "zone_id": zone["id"], "state": "ARMED", "direction": bo_dir,
                    "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                    "weight": zone["weight"], "explain": zone["explain"],
                    "trigger_hint": f"break confirmed, but net R:R {rr} < "
                                    f"{cfg.min_rr_net} to {tp1}"})
                continue
            avoid = []
            if not aligned:
                avoid.append(f"HTF ladder is {bias} — the break has no higher-TF tailwind")
            if session["effect"] == "WARN_DOWNGRADE":
                avoid.append(f"chop window ({session['label']}) — fake-break risk, downgraded")
            if (len(bars) - 1 - brk_i) > 12:
                avoid.append("the break is aging — momentum may be spent")
            hist = memory.get("sweep_history") or []
            rev = sum(1 for s_ in hist if s_["outcome"] == "REVERSED")
            if hist and rev > len(hist) / 2:
                avoid.append("recent raids mostly REVERSED — breakout follow-through unproven")
            if rr < 2.0:
                avoid.append(f"net R:R only {rr} — limited room to the first pool")
            if not avoid:
                avoid.append("a close back inside the level voids the break — no chasing")
            kind_word = "Breakout" if bo_dir == _LONG else "Breakdown"
            ctx = (f"Price displaced through a {zone['stack']}-TF level "
                   f"({zone['explain']}) and the retest HELD at {entry}. "
                   f"Draw: {tp1_pool['kind']} at {tp1}. Session: {session['label']}.")
            setups.append({
                "id": f"{symbol}:v3:{zone['id']}:BO:{bars[retest_i]['ts']}",
                "symbol": symbol, "direction": bo_dir, "setup_type": kind_word,
                "grade": grade,
                "grade_reason": f"Grade {grade}: {n} of 7 confluences — "
                                + "; ".join(f[:60] for f in factors) + ".",
                "confluences": n, "confluences_total": 7,
                "risk_level": "LOW" if grade == "A+" and aligned else
                              "MEDIUM" if grade == "A+" or aligned else "HIGH",
                "entry": entry, "sl": sl, "tp1": tp1,
                "tp2": (round(tp2, 2) if tp2 else None), "rr": rr,
                "htf_bias": bias, "ltf_trend": r5.get("trend") or "RANGE",
                "market_context": ctx, "reasons": factors,
                "reasons_to_avoid": avoid,
                "invalidation": f"a decisive 5m close back "
                                f"{'below' if bo_dir == _LONG else 'above'} "
                                f"{entry} (inside the level) voids the break",
                "early_exit": ["the retest level is lost on a closing basis",
                               "an opposing 5m CHOCH prints",
                               "no continuation within a few bars of the retest"],
                "management_notes": [f"risk only to {sl}; size for a fixed small loss",
                                     "stop to break-even at +1R",
                                     f"partials at TP1 {tp1}"
                                     + (f", trail toward TP2 {tp2}" if tp2 else "")],
                "holding_time": "INTRADAY",
                "why": {
                    "why_exists": f"a mapped {zone['stack']}-TF level changed hands "
                                  f"on displacement",
                    "why_now": "the retest just held on 5m",
                    "why_entry": f"the broken level at {entry} (the retest price)",
                    "why_sl": f"back inside the level at {sl} — the break failed there",
                    "why_targets": f"next liquidity {tp1_pool['kind']} {tp1}"
                                   + (f", then the next map zone {tp2}" if tp2 else ""),
                    "why_edge": f"{n} independent confluences on a displaced "
                                f"break + retest-hold — continuation, not prediction",
                },
                "state": "TRIGGERED",
                "zone": {"lo": zone["lo"], "hi": zone["hi"],
                         "explain": zone["explain"], "stack": zone["stack"]},
                "session": session,
                "created_ts": _iso(bars[retest_i]["ts"]),
            })
            continue

        came_from = None
        if touch_i > 0:
            prev_close = bars[touch_i - 1]["c"]
            came_from = "ABOVE" if prev_close > zone["hi"] else \
                        "BELOW" if prev_close < zone["lo"] else None
        if came_from is None:
            came_from = "ABOVE" if price >= zone["hi"] else "BELOW"
        direction = _LONG if came_from == "ABOVE" else _SHORT

        # ---- REVERSAL confirmation ----
        conf = _find_confirmation(direction, zone, bars, touch_i,
                                  choch_up if direction == _LONG else choch_dn,
                                  atr, cfg)
        # location filter: longs from discount, shorts from premium (1h read)
        loc_ok = pd_1h is None or \
            (direction == _LONG and pd_1h == "DISCOUNT") or \
            (direction == _SHORT and pd_1h == "PREMIUM")

        if conf is None or not loc_ok:
            note = "waiting for a 5m confirmation" if loc_ok else \
                f"no reversal here — price is in {str(pd_1h).lower()} " \
                f"(wrong half for a {direction.lower()})"
            watching.append({
                "zone_id": zone["id"], "state": "ARMED",
                "direction": direction, "lo": zone["lo"], "hi": zone["hi"],
                "distance": 0.0, "weight": zone["weight"],
                "explain": zone["explain"], "trigger_hint": note})
            continue

        # ---- build the setup ----
        fuel = _sweep_fuel(zone, reads, last["ts"], atr, cfg)
        # counter-trend guard (replay-validated): candle patterns AGAINST the 5m
        # trend, with no sweep fuel and no CHOCH, are noise — a trader skips them
        t5 = r5.get("trend")
        opposing = (direction == _LONG and t5 == "BEARISH") or \
                   (direction == _SHORT and t5 == "BULLISH")
        structural = fuel is not None or conf.kind == "5m CHOCH"
        if cfg.counter_trend_needs_fuel and opposing and not structural:
            watching.append({
                "zone_id": zone["id"], "state": "ARMED", "direction": direction,
                "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                "weight": zone["weight"], "explain": zone["explain"],
                "trigger_hint": f"counter-trend {direction.lower()} without sweep "
                                f"fuel — needs a CHOCH or a liquidity raid first"})
            continue
        # trend-session guard (replay-validated: London open 8% / peak 0% win on
        # plain fades): in BOOST windows a reversal must be structural too
        if cfg.boost_needs_fuel and session["effect"] == "BOOST" and not structural:
            watching.append({
                "zone_id": zone["id"], "state": "ARMED", "direction": direction,
                "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                "weight": zone["weight"], "explain": zone["explain"],
                "trigger_hint": "trend session — a fade here needs a sweep or a "
                                "CHOCH, not just a candle pattern"})
            continue
        # entry at the zone EDGE (the retest a trader actually gets filled on)
        if cfg.entry_at_edge:
            entry = round(zone["hi"] if direction == _LONG else zone["lo"], 2)
        else:
            entry = round((zone["lo"] + zone["hi"]) / 2.0, 2)
        if direction == _LONG:
            wick_lo = min(b["l"] for b in bars[touch_i:bars.index(conf.bar) + 1])
            sl = round(min(zone["lo"], wick_lo) - cfg.sl_pad_atr * atr, 2)
        else:
            wick_hi = max(b["h"] for b in bars[touch_i:bars.index(conf.bar) + 1])
            sl = round(max(zone["hi"], wick_hi) + cfg.sl_pad_atr * atr, 2)
        tp1, tp2, tp1_pool = _targets(direction, entry, mkt_map)
        if tp1 is None:
            continue
        rr = _net_rr(entry, sl, tp1, cfg)
        if rr is None or rr < cfg.min_rr_net:
            watching.append({
                "zone_id": zone["id"], "state": "ARMED", "direction": direction,
                "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                "weight": zone["weight"], "explain": zone["explain"],
                "trigger_hint": f"confirmed, but net R:R {rr} < {cfg.min_rr_net} "
                                f"to {tp1} — geometry doesn't pay"})
            continue

        # ---- confluence graph (7 named factors) ----
        aligned = (bias == "BULLISH" and direction == _LONG) or \
                  (bias == "BEARISH" and direction == _SHORT)
        factors = []
        if aligned:
            factors.append(f"HTF bias {bias.lower()} — with the ladder")
        if zone["stack"] >= 2:
            factors.append(f"{zone['stack']}-TF zone stack ({zone['explain']})")
        if fuel:
            factors.append(f"{fuel['kind']} ★{fuel['priority']} swept into the zone "
                           f"(stop-hunt fuel)")
        if "TRENDLINE" in (zone.get("kinds") or []):
            factors.append("trendline confluence in the zone")
        if any(c["state"] == "FRESH" for c in zone.get("components", [])):
            factors.append("FRESH zone component (first return)")
        if conf.displaced or conf.kind == "5m CHOCH":
            factors.append(f"clean confirmation — {conf.kind}")
        if session["effect"] == "BOOST" and \
                (not cfg.boost_needs_fuel or fuel is not None
                 or conf.kind == "5m CHOCH"):
            # trend sessions reward SWEEP/CHOCH reversals; a plain fade into
            # London momentum is not a confluence (replay-validated)
            factors.append(f"session: {session['label']}")
        n = len(factors)

        # ---- session gates + issuance floor ----
        if session["effect"] == "BLOCK":
            continue
        if n < cfg.min_issue_confluences:      # replay-validated: <3 factors lost
            watching.append({
                "zone_id": zone["id"], "state": "ARMED", "direction": direction,
                "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                "weight": zone["weight"], "explain": zone["explain"],
                "trigger_hint": f"confirmed but only {n} of 7 confluences — "
                                f"below the {cfg.min_issue_confluences}-factor floor"})
            continue
        grade = "A+" if n >= cfg.grade_a_plus else \
                "A" if n >= cfg.grade_a else "B"
        if session["effect"] == "WARN_DOWNGRADE":
            # replay-validated: downgraded-to-B issuance lost 100% — A+ becomes
            # A here; a plain A in a chop window is simply not taken
            grade = {"A+": "A", "A": None}.get(grade)
            if grade is None:
                continue
        if session.get("min_grade") == "A+" and grade != "A+":
            watching.append({
                "zone_id": zone["id"], "state": "ARMED", "direction": direction,
                "lo": zone["lo"], "hi": zone["hi"], "distance": 0.0,
                "weight": zone["weight"], "explain": zone["explain"],
                "trigger_hint": f"{session['label']}: reversals need A+ here — "
                                f"this one graded {grade}"})
            continue

        # ---- honesty: the bear case ----
        avoid = []
        if not aligned:
            avoid.append(f"HTF ladder is {bias} — this trade has no higher-TF tailwind")
        if session["effect"] == "WARN_DOWNGRADE":
            avoid.append(f"weak session window ({session['label']}) — downgraded")
        weak = [c for c in zone.get("components", []) if c["state"] == "WEAK"]
        if weak:
            avoid.append(f"{len(weak)} zone component(s) already WEAK (multiple touches)")
        if fuel is None:
            avoid.append("no liquidity sweep into the zone — reversal fuel unproven")
        if rr < 2.0:
            avoid.append(f"net R:R only {rr} — limited room to the first pool")
        hist = memory.get("sweep_history") or []
        cont = sum(1 for s in hist if s["outcome"] == "CONTINUED")
        if hist and cont > len(hist) / 2:
            avoid.append("recent sweeps mostly CONTINUED — raids are running, not reversing")
        if not avoid:
            avoid.append("if the confirming bar's low is lost, the read is wrong — no averaging")

        risk_level = "LOW" if grade == "A+" and aligned else \
                     "MEDIUM" if grade in ("A+", "A") else "HIGH"
        d_word = "long" if direction == _LONG else "short"
        ctx = (f"Price {'dipped' if direction == _LONG else 'rallied'} into a "
               f"{zone['stack']}-TF map zone ({zone['explain']}) and confirmed with a "
               f"{conf.kind}"
               + (f" after a {fuel['kind']} sweep" if fuel else "")
               + f". Draw: {tp1_pool['kind']} at {tp1}. Session: {session['label']}.")
        setups.append({
            "id": f"{symbol}:v3:{zone['id']}:{conf.bar['ts']}",
            "symbol": symbol, "direction": direction,
            "setup_type": "Zone Reversal",
            "grade": grade,
            "grade_reason": f"Grade {grade}: {n} of 7 confluences — "
                            + "; ".join(f[:60] for f in factors) + ".",
            "confluences": n, "confluences_total": 7,
            "risk_level": risk_level,
            "entry": entry, "sl": sl, "tp1": tp1,
            "tp2": (round(tp2, 2) if tp2 else None), "rr": rr,
            "htf_bias": bias, "ltf_trend": r5.get("trend") or "RANGE",
            "market_context": ctx,
            "reasons": factors,
            "reasons_to_avoid": avoid,
            "invalidation": f"a decisive 5m close beyond {sl} voids the idea",
            "early_exit": ["no displacement away from the zone within a few bars",
                           "an opposing 5m CHOCH prints",
                           "the swept level is reclaimed and holds"],
            "management_notes": [f"risk only to {sl}; size for a fixed small loss",
                                 "stop to break-even at +1R",
                                 f"partials at TP1 {tp1}"
                                 + (f", trail toward TP2 {tp2}" if tp2 else "")],
            "holding_time": "INTRADAY",
            "why": {
                "why_exists": f"a mapped {zone['stack']}-TF zone was reached and held",
                "why_now": f"the {conf.kind} just printed on 5m",
                "why_entry": f"zone 50% at {entry}",
                "why_sl": f"beyond the zone{' and sweep wick' if fuel else ''} at {sl}",
                "why_targets": f"nearest opposing liquidity {tp1_pool['kind']} {tp1}"
                               + (f", then the next map zone {tp2}" if tp2 else ""),
                "why_edge": f"{n} independent confluences at a pre-mapped decision point",
            },
            "state": "TRIGGERED",
            "zone": {"lo": zone["lo"], "hi": zone["hi"],
                     "explain": zone["explain"], "stack": zone["stack"]},
            "session": session,
            "created_ts": _iso(conf.bar["ts"]),
        })

    rank = {"A+": 3, "A": 2, "B": 1}
    setups.sort(key=lambda s: (rank.get(s["grade"], 0), s["rr"]), reverse=True)
    watching.sort(key=lambda w: (-w["weight"], w["distance"]))
    msg = None
    if not setups:
        msg = ("session blocked: " + session["label"]
               if session["effect"] == "BLOCK"
               else "No setup right now — the engine is watching "
                    f"{len(watching)} mapped zone(s).")
    return {"symbol": symbol, "session": session,
            "setups": setups[:cfg.max_setups_out],
            "watching": watching[:cfg.max_watching_out],
            "message": msg}

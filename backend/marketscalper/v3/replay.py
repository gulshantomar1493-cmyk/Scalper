"""V3 P4 — Replay & Performance Validation Engine.

Feed any historical range through the FULL V3 stack (chart reads → market map →
memory → virtual trader, session-gated) exactly as live — no lookahead: at step
t every layer sees only candles closed at t; outcomes are then simulated on the
candles AFTER the confirming bar. Deterministic: same range, same report.

Report: win rate · avg R · expectancy · profit factor · max drawdown · average
hold · per-grade / per-session / per-direction splits · EXPIRED count ·
FALSE trades (stopped before +1R, with context) · MISSED trades (ARMED zones
that were never issued but then ran ≥2R). This is the objective scoreboard the
owner reviews — numbers, not vibes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from marketscalper.v3.chart_read import ChartReadEngine, _parse_ts
from marketscalper.v3.config import V3Config, DEFAULT
from marketscalper.v3.market_map import build_map, build_memory
from marketscalper.v3.virtual_trader import build_trades

log = logging.getLogger(__name__)

_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


# ------------------------------------------------------- outcome simulation

def simulate_outcome(setup: dict, bars5: list[dict], confirm_i: int,
                     cfg: V3Config) -> dict:
    """Walk the candles AFTER the confirming bar: limit fill at entry, then
    SL vs TP1 (same-bar ambiguity = SL first — conservative), MAE/MFE in R,
    TP2 flag, horizon mark-to-market. Pure."""
    entry, sl, tp1, tp2 = setup["entry"], setup["sl"], setup["tp1"], setup["tp2"]
    long = setup["direction"] == "LONG"
    risk = abs(entry - sl)
    if risk <= 0:
        return {"outcome": "INVALID", "r": 0.0}
    fill_i = None
    for i in range(confirm_i + 1,
                   min(confirm_i + 1 + cfg.replay_entry_window_bars, len(bars5))):
        b = bars5[i]
        if b["l"] <= entry <= b["h"]:
            fill_i = i
            break
        # gap through the entry in the trade direction → fill at the open
        if (long and b["h"] < entry) or (not long and b["l"] > entry):
            fill_i = i
            entry = b["o"]
            risk = abs(entry - sl)
            if risk <= 0:
                return {"outcome": "INVALID", "r": 0.0}
            break
    if fill_i is None:
        return {"outcome": "EXPIRED", "r": 0.0}

    mae = mfe = 0.0
    tp2_hit = False
    end_i = min(fill_i + cfg.replay_horizon_bars, len(bars5) - 1)
    for i in range(fill_i, end_i + 1):
        b = bars5[i]
        up_r = ((b["h"] - entry) if long else (entry - b["l"])) / risk
        dn_r = ((entry - b["l"]) if long else (b["h"] - entry)) / risk
        mfe = max(mfe, up_r)
        mae = max(mae, dn_r)
        sl_hit = (b["l"] <= sl) if long else (b["h"] >= sl)
        tp_hit = (b["h"] >= tp1) if long else (b["l"] <= tp1)
        if sl_hit:                                   # ambiguity → SL first
            return {"outcome": "STOPPED", "r": -1.0, "hold": i - fill_i,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "tp2_hit": False}
        if tp_hit:
            r1 = abs(tp1 - entry) / risk
            if tp2 is not None:                      # did the runner reach TP2?
                for j in range(i, end_i + 1):
                    bj = bars5[j]
                    if (long and bj["l"] <= sl) or (not long and bj["h"] >= sl):
                        break
                    if (long and bj["h"] >= tp2) or (not long and bj["l"] <= tp2):
                        tp2_hit = True
                        break
            return {"outcome": "TP1", "r": round(r1, 2), "hold": i - fill_i,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "tp2_hit": tp2_hit}
    close = bars5[end_i]["c"]
    r = ((close - entry) if long else (entry - close)) / risk
    return {"outcome": "TIMEOUT", "r": round(r, 2), "hold": end_i - fill_i,
            "mae": round(mae, 2), "mfe": round(mfe, 2), "tp2_hit": False}


# ------------------------------------------------------------- aggregation

def aggregate(trades: list[dict]) -> dict:
    """Pure scoreboard math over simulated trades (EXPIRED excluded from
    performance, counted separately)."""
    done = [t for t in trades if t["outcome"] in ("TP1", "STOPPED", "TIMEOUT")]
    expired = sum(1 for t in trades if t["outcome"] == "EXPIRED")
    if not done:
        return {"n": 0, "expired": expired, "win_rate": None, "avg_r": None,
                "expectancy": None, "profit_factor": None, "max_drawdown": None,
                "avg_hold_bars": None, "tp2_rate": None}
    rs = [t["r"] for t in done]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {
        "n": len(done), "expired": expired,
        "win_rate": round(len(wins) / len(done), 3),
        "avg_r": round(sum(rs) / len(done), 2),
        "expectancy": round(sum(rs) / len(done), 2),
        "profit_factor": (round(sum(wins) / sum(losses), 2)
                          if losses and sum(losses) > 0 else None),
        "max_drawdown": round(dd, 2),
        "avg_hold_bars": round(sum(t.get("hold", 0) for t in done) / len(done), 1),
        "tp2_rate": round(sum(1 for t in done if t.get("tp2_hit")) / len(done), 3),
        "total_r": round(sum(rs), 2),
    }


def _split(trades: list[dict], key) -> dict:
    groups: dict = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: aggregate(v) for k, v in sorted(groups.items(), key=lambda x: str(x[0]))}


# ------------------------------------------------------------------ engine

class ReplayEngine:
    """Walks a historical range through the full V3 stack, no lookahead."""

    def __init__(self, chart_service, cfg: V3Config = DEFAULT):
        self._charts = chart_service
        self._cfg = cfg

    async def _candles(self, symbol: str, tf: str, start: datetime,
                       end: datetime) -> list[dict]:
        pad = timedelta(seconds=_TF_SECONDS[tf] * self._cfg.history_bars)
        chart = await self._charts.get_chart(symbol, tf, start - pad, end)
        return [{"ts": _parse_ts(c["ts"]), "o": float(c["o"]), "h": float(c["h"]),
                 "l": float(c["l"]), "c": float(c["c"]),
                 "v": float(c.get("v") or 0)}
                for c in chart["candles"] if c.get("complete", True)]

    async def run(self, symbol: str, start: datetime, end: datetime,
                  progress=None) -> dict:
        cfg = self._cfg
        data = {tf: await self._candles(symbol, tf, start, end)
                for tf in cfg.read_tfs}
        bars5 = data["5m"]
        start_ts = int(start.timestamp())
        first_i = next((i for i, b in enumerate(bars5) if b["ts"] >= start_ts), None)
        if first_i is None or not bars5:
            return {"error": "no candles in range"}

        read_cache: dict = {}          # tf -> (last_ts_used, read)
        idx: dict = {tf: 0 for tf in cfg.read_tfs}
        issued: dict = {}              # setup id -> {setup, confirm_i}
        armed_seen: dict = {}          # zone key -> armed snapshot (missed scan)

        for i in range(max(first_i, cfg.confirm_bars), len(bars5),
                       cfg.replay_step_bars):
            now_ts = bars5[i]["ts"]
            # per-TF reads: refresh only when that TF printed a new closed candle
            reads = {}
            for tf, series in data.items():
                k = idx[tf]
                while k + 1 < len(series) and series[k + 1]["ts"] + _TF_SECONDS[tf] <= now_ts + 300:
                    k += 1
                idx[tf] = k
                last_ts = series[k]["ts"]
                hit = read_cache.get(tf)
                if hit is None or hit[0] != last_ts:
                    window = series[max(0, k + 1 - cfg.history_bars): k + 1]
                    read = ChartReadEngine(symbol, tf, cfg).read(window) \
                        if len(window) >= 30 else None
                    if read is not None:
                        read["ready"] = True
                    read_cache[tf] = (last_ts, read)
                reads[tf] = read_cache[tf][1]

            if reads.get("5m") is None:
                continue
            mkt_map = build_map(symbol, reads, cfg)
            if not mkt_map.get("ready"):
                continue
            memory = build_memory(symbol, reads, cfg)
            window5 = bars5[max(0, i + 1 - cfg.confirm_bars): i + 1]
            out = build_trades(symbol, mkt_map, memory, reads, window5, cfg)
            for s in out["setups"]:
                if s["id"] not in issued:
                    issued[s["id"]] = {"setup": s, "confirm_i": i}
            for w in out["watching"]:
                if w["state"] == "ARMED":
                    key = (round(w["lo"], 1), round(w["hi"], 1), w["direction"])
                    if key not in armed_seen:
                        armed_seen[key] = {**w, "armed_i": i,
                                           "atr": (reads.get("5m") or {}).get("atr") or 0.0}
            if progress and (i - first_i) % (cfg.replay_step_bars * 40) == 0:
                progress(i - first_i, len(bars5) - first_i)

        # ---- outcomes ---------------------------------------------------
        trades = []
        for rec in issued.values():
            s = rec["setup"]
            sim = simulate_outcome(s, bars5, rec["confirm_i"], cfg)
            trades.append({**sim, "id": s["id"], "direction": s["direction"],
                           "grade": s["grade"], "rr_planned": s["rr"],
                           "setup_type": s.get("setup_type"),
                           "session": (s.get("session") or {}).get("label"),
                           "entry": s["entry"], "sl": s["sl"], "tp1": s["tp1"],
                           "zone": (s.get("zone") or {}).get("explain"),
                           "created_ts": s["created_ts"]})

        false_trades = [t for t in trades
                        if t["outcome"] == "STOPPED" and t.get("mfe", 0) < 1.0]

        # ---- missed scan: ARMED but never issued, then ran ≥2R ----------
        issued_zone_keys = {(round((x["setup"].get("zone") or {}).get("lo", 0), 1),
                             round((x["setup"].get("zone") or {}).get("hi", 0), 1),
                             x["setup"]["direction"]) for x in issued.values()}
        missed = []
        for key, w in armed_seen.items():
            if key in issued_zone_keys:
                continue
            entry = (w["lo"] + w["hi"]) / 2.0
            pad = cfg.sl_pad_atr * (w.get("atr") or 0.0)
            long = w["direction"] == "LONG"
            sl = (w["lo"] - pad) if long else (w["hi"] + pad)
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            hyp = {"direction": w["direction"], "entry": entry, "sl": sl,
                   "tp1": entry + (risk * cfg.replay_missed_rr if long
                                   else -risk * cfg.replay_missed_rr),
                   "tp2": None}
            sim = simulate_outcome(hyp, bars5, w["armed_i"], cfg)
            if sim["outcome"] == "TP1":
                missed.append({"zone": w["explain"], "direction": w["direction"],
                               "lo": w["lo"], "hi": w["hi"],
                               "would_have_made_r": sim["r"],
                               "reason_not_issued": w["trigger_hint"]})

        report = {
            "symbol": symbol,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "bars_5m": len(bars5) - first_i,
            "issued": len(trades),
            "overall": aggregate(trades),
            "by_grade": _split(trades, lambda t: t["grade"]),
            "by_session": _split(trades, lambda t: t["session"]),
            "by_direction": _split(trades, lambda t: t["direction"]),
            "by_type": _split(trades, lambda t: t.get("setup_type")),
            "trades": sorted(trades, key=lambda t: t["created_ts"]),
            "false_trades": false_trades,
            "missed_trades": missed[:20],
            "missed_count": len(missed),
        }
        return report

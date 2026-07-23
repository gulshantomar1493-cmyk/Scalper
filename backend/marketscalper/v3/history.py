"""V3 — Trade Recommendation History (auto-recorded, permanently searchable).

Completely independent of paper trading. Every setup the engine issues is
INSERTed once (setup_id dedupe); a live tracker then folds new 5m closes into
each ACTIVE row: limit fill → SL/TP1/TP2 (SL-first same-bar ambiguity, like the
replay engine) → final status + points + MAE/MFE + holding time. The `analysis`
JSONB keeps the engine's full reasoning at issue time (reasons, avoid-reasons,
invalidation, management, why, zone, session, bias) so success/failure can be
studied later. Read-only query helpers power the History screen + CSV export.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from marketscalper.v3.config import V3Config, DEFAULT

log = logging.getLogger(__name__)

STATUSES = ("ACTIVE", "TP1_HIT", "TP2_HIT", "STOP_LOSS",
            "CANCELLED", "EXPIRED", "TIMEOUT")

_COLS = ("id, setup_id, ts, symbol, timeframe, session_label, session_rating,"
         " direction, setup_type, grade, entry, sl, tp1, tp2, rr, status,"
         " result_r, points_captured, points_lost, mae_r, mfe_r,"
         " holding_minutes, filled_ts, closed_ts, analysis, created_at")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def row_dict(r) -> dict:
    d = {k: r[k] for k in
         ("id", "setup_id", "symbol", "timeframe", "session_label",
          "session_rating", "direction", "setup_type", "grade", "status")}
    for k in ("entry", "sl", "tp1", "tp2", "rr", "result_r",
              "points_captured", "points_lost", "mae_r", "mfe_r"):
        d[k] = float(r[k]) if r[k] is not None else None
    d["holding_minutes"] = r["holding_minutes"]
    d["ts"] = _iso(r["ts"])
    d["filled_ts"] = _iso(r["filled_ts"])
    d["closed_ts"] = _iso(r["closed_ts"])
    d["created_at"] = _iso(r["created_at"])
    a = r["analysis"]
    d["analysis"] = json.loads(a) if isinstance(a, str) else a
    return d


# ------------------------------------------------------------------ record

async def record_setups(pool, setups: list[dict]) -> int:
    """Insert newly issued setups (setup_id dedupe). Error-tolerant: a DB
    failure never breaks the engine path."""
    n = 0
    for s in setups:
        try:
            analysis = {k: s.get(k) for k in
                        ("grade_reason", "confluences", "confluences_total",
                         "risk_level", "market_context", "reasons",
                         "reasons_to_avoid", "invalidation", "early_exit",
                         "management_notes", "why", "zone", "htf_bias",
                         "ltf_trend", "holding_time")}
            async with pool.acquire() as conn:
                res = await conn.execute(
                    "INSERT INTO v3_recommendations"
                    " (setup_id, ts, symbol, timeframe, session_label,"
                    "  session_rating, direction, setup_type, grade,"
                    "  entry, sl, tp1, tp2, rr, analysis)"
                    " VALUES ($1,$2,$3,'5m',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)"
                    " ON CONFLICT (setup_id) DO NOTHING",
                    s["id"], datetime.fromisoformat(s["created_ts"]),
                    s["symbol"], (s.get("session") or {}).get("label"),
                    (s.get("session") or {}).get("rating"),
                    s["direction"], s["setup_type"], s["grade"],
                    s["entry"], s["sl"], s["tp1"], s.get("tp2"), s.get("rr"),
                    json.dumps(analysis))
            if res.endswith("1"):
                n += 1
        except Exception as exc:                      # never break the engine
            log.warning("v3 history record failed for %s: %s", s.get("id"), exc)
    return n


# ------------------------------------------------------------- live tracker

def advance_recommendation(rec: dict, bars5: list[dict],
                           cfg: V3Config = DEFAULT) -> dict | None:
    """Pure fold: given an ACTIVE recommendation and the 5m closed candles
    SINCE its issue bar, decide the next status. Mirrors the replay simulator
    (limit fill window, SL-first ambiguity, TP1→TP2, horizon timeout).
    Returns an update dict or None (still ACTIVE, nothing to write)."""
    entry, sl, tp1, tp2 = rec["entry"], rec["sl"], rec["tp1"], rec["tp2"]
    long = rec["direction"] == "LONG"
    issue_ts = int(datetime.fromisoformat(rec["ts"]).timestamp()) \
        if isinstance(rec["ts"], str) else int(rec["ts"].timestamp())
    path = [b for b in bars5 if b["ts"] > issue_ts]
    if not path:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return {"status": "CANCELLED", "closed_ts": path[-1]["ts"]}

    # ---- fill phase ----
    fill_i = None
    for i, b in enumerate(path[:cfg.replay_entry_window_bars]):
        # invalidated before fill: price runs through the STOP side first
        if (long and b["c"] < sl) or (not long and b["c"] > sl):
            return {"status": "CANCELLED", "closed_ts": b["ts"],
                    "note": "invalidated before entry filled"}
        if b["l"] <= entry <= b["h"]:
            fill_i = i
            break
    if fill_i is None:
        if len(path) >= cfg.replay_entry_window_bars:
            return {"status": "EXPIRED", "closed_ts": path[-1]["ts"]}
        return None                                    # still waiting for fill

    filled_ts = path[fill_i]["ts"]
    mae = mfe = 0.0
    tp1_i = None
    end_i = min(fill_i + cfg.replay_horizon_bars, len(path) - 1)
    for i in range(fill_i, end_i + 1):
        b = path[i]
        up_r = ((b["h"] - entry) if long else (entry - b["l"])) / risk
        dn_r = ((entry - b["l"]) if long else (b["h"] - entry)) / risk
        mfe = max(mfe, up_r)
        mae = max(mae, dn_r)
        sl_hit = (b["l"] <= sl) if long else (b["h"] >= sl)
        tp_hit = (b["h"] >= tp1) if long else (b["l"] <= tp1)
        if sl_hit and tp1_i is None:                   # SL first (conservative)
            return {"status": "STOP_LOSS", "result_r": -1.0,
                    "points_captured": 0.0, "points_lost": round(risk, 2),
                    "mae_r": round(mae, 2), "mfe_r": round(mfe, 2),
                    "filled_ts": filled_ts, "closed_ts": b["ts"],
                    "holding_minutes": (b["ts"] - filled_ts) // 60}
        if tp_hit and tp1_i is None:
            tp1_i = i
        if tp1_i is not None:
            # after TP1: watch for TP2 (or SL on the runner → book TP1 only)
            if tp2 is not None and ((b["h"] >= tp2) if long else (b["l"] <= tp2)):
                r2 = abs(tp2 - entry) / risk
                return {"status": "TP2_HIT", "result_r": round(r2, 2),
                        "points_captured": round(abs(tp2 - entry), 2),
                        "points_lost": 0.0,
                        "mae_r": round(mae, 2), "mfe_r": round(mfe, 2),
                        "filled_ts": filled_ts, "closed_ts": b["ts"],
                        "holding_minutes": (b["ts"] - filled_ts) // 60}
            if sl_hit or tp2 is None or i == end_i:
                r1 = abs(tp1 - entry) / risk
                return {"status": "TP1_HIT", "result_r": round(r1, 2),
                        "points_captured": round(abs(tp1 - entry), 2),
                        "points_lost": 0.0,
                        "mae_r": round(mae, 2), "mfe_r": round(mfe, 2),
                        "filled_ts": filled_ts, "closed_ts": b["ts"],
                        "holding_minutes": (b["ts"] - filled_ts) // 60}
    if (len(path) - 1 - fill_i) >= cfg.replay_horizon_bars:
        b = path[end_i]
        r = ((b["c"] - entry) if long else (entry - b["c"])) / risk
        pts = (b["c"] - entry) if long else (entry - b["c"])
        return {"status": "TIMEOUT", "result_r": round(r, 2),
                "points_captured": round(max(pts, 0.0), 2),
                "points_lost": round(max(-pts, 0.0), 2),
                "mae_r": round(mae, 2), "mfe_r": round(mfe, 2),
                "filled_ts": filled_ts, "closed_ts": b["ts"],
                "holding_minutes": (b["ts"] - filled_ts) // 60}
    return None                                        # in trade, still open


async def update_active(pool, chart_service, cfg: V3Config = DEFAULT) -> int:
    """Advance every ACTIVE recommendation with the latest 5m candles."""
    from datetime import timedelta
    from marketscalper.v3.chart_read import _parse_ts
    updated = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM v3_recommendations WHERE status='ACTIVE'"
            f" ORDER BY id")
    by_symbol: dict = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)
    for symbol, recs in by_symbol.items():
        oldest = min(r["ts"] for r in recs)
        now = datetime.now(timezone.utc)
        try:
            chart = await chart_service.get_chart(symbol, "5m",
                                                  oldest, now)
        except Exception as exc:
            log.warning("v3 history: candle fetch failed for %s: %s", symbol, exc)
            continue
        bars5 = [{"ts": _parse_ts(c["ts"]), "o": float(c["o"]),
                  "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])}
                 for c in chart["candles"] if c.get("complete", True)]
        for r in recs:
            upd = advance_recommendation(row_dict(r), bars5, cfg)
            if not upd:
                continue
            sets, vals = [], []
            for k in ("status", "result_r", "points_captured", "points_lost",
                      "mae_r", "mfe_r", "holding_minutes"):
                if k in upd:
                    vals.append(upd[k])
                    sets.append(f"{k}=${len(vals) + 1}")
            for k in ("filled_ts", "closed_ts"):
                if k in upd and upd[k] is not None:
                    vals.append(datetime.fromtimestamp(upd[k], tz=timezone.utc))
                    sets.append(f"{k}=${len(vals) + 1}")
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"UPDATE v3_recommendations SET {', '.join(sets)}"
                        f" WHERE id=$1", r["id"], *vals)
                updated += 1
            except Exception as exc:
                log.warning("v3 history update failed id=%s: %s", r["id"], exc)
    return updated


# ------------------------------------------------------------------ queries

async def list_recommendations(pool, *, symbol=None, grade=None, status=None,
                               setup_type=None, direction=None, session=None,
                               date_from=None, date_to=None, q=None,
                               sort="ts", order="desc",
                               limit=50, offset=0) -> dict:
    where, vals = [], []

    def add(clause, v):
        vals.append(v)
        where.append(clause.replace("?", f"${len(vals)}"))

    if symbol:
        add("symbol = ?", symbol)
    if grade:
        add("grade = ?", grade)
    if status:
        add("status = ?", status)
    if setup_type:
        add("setup_type = ?", setup_type)
    if direction:
        add("direction = ?", direction)
    if session:
        add("session_label ILIKE ?", f"%{session}%")
    if date_from:
        add("ts >= ?", date_from)
    if date_to:
        add("ts < ?", date_to)
    if q:
        add("analysis::text ILIKE ?", f"%{q}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    sort_col = sort if sort in ("ts", "grade", "status", "result_r", "rr",
                                "symbol", "setup_type") else "ts"
    dr = "ASC" if str(order).lower() == "asc" else "DESC"
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM v3_recommendations{w}", *vals)
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM v3_recommendations{w}"
            f" ORDER BY {sort_col} {dr} NULLS LAST, id DESC"
            f" LIMIT {int(limit)} OFFSET {int(offset)}", *vals)
    return {"total": total, "items": [row_dict(r) for r in rows],
            "limit": limit, "offset": offset}


async def get_recommendation(pool, rec_id: int) -> dict | None:
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            f"SELECT {_COLS} FROM v3_recommendations WHERE id=$1", rec_id)
    return row_dict(r) if r else None


CSV_FIELDS = ("id", "ts", "symbol", "timeframe", "session_label", "direction",
              "setup_type", "grade", "entry", "sl", "tp1", "tp2", "rr",
              "status", "result_r", "points_captured", "points_lost",
              "mae_r", "mfe_r", "holding_minutes", "filled_ts", "closed_ts")


def to_csv(items: list[dict]) -> str:
    import io
    import csv as _csv
    buf = io.StringIO()
    wr = _csv.writer(buf, lineterminator="\n")
    wr.writerow(CSV_FIELDS)
    for it in items:
        wr.writerow([it.get(f, "") if it.get(f) is not None else ""
                     for f in CSV_FIELDS])
    return buf.getvalue()

"""Honest outcome accounting. This is where the old engine lied; V4 does not.

Old V3 defects this module exists to prevent:
  * outcomes booked GROSS of fees (fees only gated issuance)      -> fees always charged
  * losses floored at exactly -1.0R                               -> gaps charged in full
  * 24h mark-to-market counted as a WIN in the win rate           -> TIME exits separated
  * funding never charged on multi-day holds                      -> charged per day
"""
from __future__ import annotations
from dataclasses import dataclass
from . import config as C

OPEN, FILLED, TP, SL, TIME, CANCELLED = "OPEN", "FILLED", "TP", "SL", "TIME", "CANCELLED"
TERMINAL = (TP, SL, TIME, CANCELLED)


@dataclass
class Outcome:
    status: str
    fill_price: float | None = None
    exit_price: float | None = None
    gross_r: float | None = None
    fee_r: float | None = None
    funding_r: float | None = None
    net_r: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    hold_minutes: int | None = None
    filled_ts: int | None = None
    closed_ts: int | None = None


def advance(setup, bars_1m: dict, *, taker: float = C.TAKER_FEE,
            funding_per_day: float = C.FUNDING_PER_DAY,
            max_hold_days: int = C.MAX_HOLD_DAYS) -> Outcome:
    """Walk 1m bars strictly AFTER the setup's decision time.

    A STOP order: fills when price trades THROUGH the level. A gap beyond the
    trigger fills WORSE (at the open) — never better. Same-bar SL/TP ambiguity
    resolves to SL. Horizon exit is a real market close that pays fees.
    """
    import numpy as np
    ts, o, h, l, c = bars_1m["ts"], bars_1m["o"], bars_1m["h"], bars_1m["l"], bars_1m["c"]
    d = setup.direction
    start = int(np.searchsorted(ts, setup.decision_ts, side="left"))
    if start >= len(ts):
        return Outcome(OPEN)

    # ---- fill (resting stop order) ----
    stop_i = min(start + C.ENTRY_VALID_BARS_MIN, len(ts))
    if stop_i <= start:
        return Outcome(OPEN)
    hs, ls_, os_ = h[start:stop_i], l[start:stop_i], o[start:stop_i]
    trig = (hs >= setup.entry) if d > 0 else (ls_ <= setup.entry)
    w = np.flatnonzero(trig)
    if len(w) == 0:
        if stop_i >= len(ts):
            return Outcome(OPEN)                    # window not elapsed yet
        return Outcome(CANCELLED, closed_ts=int(ts[stop_i - 1]))
    k = int(w[0]); fill_i = start + k
    gapped = (os_[k] > setup.entry) if d > 0 else (os_[k] < setup.entry)
    fill = float(os_[k]) if gapped else float(setup.entry)

    risk = abs(fill - setup.stop)
    if risk <= 0:
        return Outcome(CANCELLED, closed_ts=int(ts[fill_i]))

    end_i = min(fill_i + max_hold_days * 1440, len(ts) - 1)
    if end_i <= fill_i:
        return Outcome(FILLED, fill_price=fill, filled_ts=int(ts[fill_i]))

    seg_h, seg_l, seg_o = h[fill_i:end_i + 1], l[fill_i:end_i + 1], o[fill_i:end_i + 1]
    sl_hit = (seg_l <= setup.stop) if d > 0 else (seg_h >= setup.stop)
    tp_hit = (seg_h >= setup.target) if d > 0 else (seg_l <= setup.target)
    i_sl = int(np.argmax(sl_hit)) if sl_hit.any() else 10 ** 9
    i_tp = int(np.argmax(tp_hit)) if tp_hit.any() else 10 ** 9

    if i_sl <= i_tp and i_sl < 10 ** 9:             # SL wins ties (conservative)
        j = i_sl
        beyond = (seg_o[j] < setup.stop) if d > 0 else (seg_o[j] > setup.stop)
        exit_px = float(seg_o[j]) if beyond else float(setup.stop)   # gap charged in full
        status = SL
    elif i_tp < 10 ** 9:
        j = i_tp
        exit_px = float(setup.target)               # favourable gap NOT credited
        status = TP
    else:
        j = len(seg_h) - 1
        exit_px = float(c[fill_i + j])              # real market close
        status = TIME
        if fill_i + j >= len(ts) - 1:
            return Outcome(FILLED, fill_price=fill, filled_ts=int(ts[fill_i]))

    gross = ((exit_px - fill) if d > 0 else (fill - exit_px)) / risk
    hold = int(j)
    fee_r = (fill * taker + exit_px * taker) / risk
    fund_r = (hold / 1440.0) * funding_per_day / (risk / fill)
    mfe = float(((seg_h[:j + 1].max() - fill) if d > 0 else (fill - seg_l[:j + 1].min())) / risk)
    mae = float(((fill - seg_l[:j + 1].min()) if d > 0 else (seg_h[:j + 1].max() - fill)) / risk)
    return Outcome(status, fill_price=fill, exit_price=exit_px,
                   gross_r=round(gross, 4), fee_r=round(fee_r, 4),
                   funding_r=round(fund_r, 4),
                   net_r=round(gross - fee_r - fund_r, 4),
                   mae_r=round(max(mae, 0.0), 3), mfe_r=round(max(mfe, 0.0), 3),
                   hold_minutes=hold, filled_ts=int(ts[fill_i]),
                   closed_ts=int(ts[fill_i + j]))


def summarise(rows: list[dict]) -> dict:
    """Portfolio stats. TIME exits are reported separately from real exits."""
    import numpy as np
    done = [r for r in rows if r.get("net_r") is not None]
    if not done:
        return dict(n=0, n_open=sum(1 for r in rows if r.get("status") in (OPEN, FILLED)))
    r = np.array([x["net_r"] for x in done], dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    return dict(
        n=len(r), n_open=sum(1 for x in rows if x.get("status") in (OPEN, FILLED)),
        win_rate=round(float((r > 0).mean()), 4),
        avg_net_r=round(float(r.mean()), 4), total_r=round(float(r.sum()), 2),
        profit_factor=round(float(wins.sum() / abs(losses.sum())), 3) if len(losses) and losses.sum() else None,
        max_drawdown_r=round(dd, 2),
        tp=sum(1 for x in done if x["status"] == TP),
        sl=sum(1 for x in done if x["status"] == SL),
        time_exit=sum(1 for x in done if x["status"] == TIME),
        avg_fee_r=round(float(np.mean([x.get("fee_r") or 0 for x in done])), 4),
        avg_hold_minutes=int(np.mean([x.get("hold_minutes") or 0 for x in done])),
    )


def performance_report(rows: list[dict]) -> dict:
    """Overall + per-strategy summary. One shared shape for GET /performance
    and the daily ops log line, so the two can never disagree."""
    by: dict[str, list] = {}
    for x in rows:
        by.setdefault(x.get("strategy_id", "?"), []).append(x)
    return {"overall": summarise(rows),
            "by_strategy": {k: summarise(v) for k, v in by.items()}}

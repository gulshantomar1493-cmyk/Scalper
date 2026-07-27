"""Setup construction: LEVEL + TREND FILTER -> a resting stop order.

The geometry is fixed by the research and must not be tuned per-symbol:
    entry  = the level itself (a resting STOP order, so latency is irrelevant)
    stop   = entry -/+ 5 x ATR(5m)     (keeps fee/R ~= 0.09; tighter kills it)
    target = 10R                        (the edge lives in the tail)
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import numpy as np
from . import config as C
from . import levels as L
from .filters import TrendContext, align_causal


@dataclass
class Setup:
    strategy_id: str
    symbol: str
    direction: int              # +1 long, -1 short
    entry: float
    stop: float
    target: float
    risk_pct: float             # |entry-stop| / entry
    rr: float                   # target R multiple (net of the fee estimate)
    level_source: str
    level_tf: str
    filters_passed: int
    filters_detail: dict
    decision_ts: int            # when the level became known (order can rest from here)
    valid_until_ts: int
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["direction_label"] = "LONG" if self.direction > 0 else "SHORT"
        return d


def _net_rr(entry: float, stop: float, target: float) -> float:
    """R multiple after a round-trip taker fee, so the number shown is honest."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    fee = entry * C.TAKER_FEE * 2.0
    return round((abs(target - entry) - fee) / (risk + fee), 2)


def build_setups(strategy: C.Strategy, level_bars: dict, daily_bars: dict,
                 m5_bars: dict, eval_bars: dict | None = None, *,
                 only_last: bool = False) -> list[Setup]:
    """Generate setups for one strategy.

    level_bars : bars the LEVEL is derived from (e.g. 1d swings)
    eval_bars  : grid the level is CHECKED on (e.g. 4h). Defaults to level_bars.
                 Separating these matters: a resting order can be re-armed more
                 often than the level itself updates.
    daily_bars : 1d bars, for the daily trend filter
    m5_bars    : 5m bars, ONLY used to size the stop via ATR(5m)
    only_last  : if True, return setups for the most recent closed bar only
    """
    if eval_bars is None:
        eval_bars = level_bars
    ctx = TrendContext(eval_bars, daily_bars)
    a5 = L.atr(m5_bars, C.ATR_PERIOD)
    eval_close = eval_bars["ts"] + eval_bars["tf_s"]
    j5 = align_causal(m5_bars, eval_close)
    jlvl = align_causal(level_bars, eval_close)      # causal level lookup
    cache: dict = {}
    n = len(eval_bars["c"])
    start = max(n - 2, 0) if only_last else 210
    out: list[Setup] = []
    tf_s = int(eval_bars["tf_s"])

    for i in range(start, n):
        k5 = int(j5[i]); kl = int(jlvl[i])
        if k5 < C.ATR_PERIOD + 5 or np.isnan(a5[k5]) or a5[k5] <= 0:
            continue
        if kl < 30:
            continue
        d_ts = int(eval_bars["ts"][i] + tf_s)
        for lvl in L.levels_for(level_bars, kl, strategy.level_source,
                                strategy.lookback, strategy.symbol, cache):
            if lvl is None or not np.isfinite(lvl) or lvl <= 0:
                continue
            # BREAK only. Fading a level loses on every level type (research).
            direction = 1 if lvl >= eval_bars["c"][i] else -1
            passed, detail = ctx.score(i, direction)
            if passed < strategy.min_filters:
                continue
            stop = lvl - direction * C.STOP_ATR5M_MULT * float(a5[k5])
            risk = abs(lvl - stop)
            if risk <= 0:
                continue
            target = lvl + direction * C.TARGET_R * risk
            names = [k for k, v in detail.items() if v]
            out.append(Setup(
                strategy_id=strategy.id, symbol=strategy.symbol, direction=direction,
                entry=round(float(lvl), 2), stop=round(float(stop), 2),
                target=round(float(target), 2),
                risk_pct=round(risk / lvl * 100.0, 3),
                rr=_net_rr(lvl, stop, target),
                level_source=strategy.level_source, level_tf=strategy.level_tf,
                filters_passed=passed, filters_detail=detail,
                decision_ts=d_ts,
                valid_until_ts=d_ts + C.ENTRY_VALID_BARS_MIN * 60,
                reason=(f"{'Break above' if direction > 0 else 'Break below'} "
                        f"{strategy.level_source} {strategy.level_tf} level "
                        f"{lvl:,.2f}; filters: {', '.join(names) if names else 'none'}"),
            ))
    return out

"""The proof harness.

Nothing about V5's expectancy is shown to the owner until this has run. V4
shipped on a 9-year headline whose own journal admitted significance was never
established after 2021 (t = 0.7-1.7) — and it then lost eleven live trades out
of eleven. So this reports PER ERA, never a single blended number.

The engine is driven bar by bar through `engine.read`, the same function the
live path calls, over candles cut with `fold.slice_upto` so it never sees a bar
that had not closed. Outcomes go through v4/outcome.py, which already charges
fees, resolves same-bar stop/target ambiguity to the stop, and charges gaps in
full.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..v4.outcome import advance
from . import engine, fold

#: How much history each timeframe is analysed over. Structure older than this
#: is not what a trader is reading, and an unbounded window makes the whole
#: backtest O(n^2).
WINDOW = 300

ERAS = [("2017-2020", 1483228800, 1609459200),
        ("2021-2023", 1609459200, 1704067200),
        ("2024-2026", 1704067200, 4102444800)]


@dataclass
class Trade:
    strategy_id: str
    symbol: str
    direction: int
    decision_ts: int
    entry: float
    stop: float
    target: float
    planned_rr: float
    status: str = ""
    net_r: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    hold_minutes: int | None = None
    closed_ts: int | None = None


class _AsSetup:
    """Adapts a V5 plan to the shape v4.outcome.advance expects."""
    __slots__ = ("direction", "entry", "stop", "target", "decision_ts")

    def __init__(self, t: Trade):
        self.direction, self.entry = t.direction, t.entry
        self.stop, self.target = t.stop, t.target
        self.decision_ts = t.decision_ts


def run(symbol: str, m1: dict, *, step_bars: int = 4,
        enable_range_fade: bool = False, progress=None,
        enabled: tuple = engine.ENABLED) -> list:
    """Replay `m1` and return every trade the engine would have issued.

    `step_bars` is how many 15m bars to advance between reads. 4 = hourly
    decisions, which is enough for playbooks whose fastest input is a 15m close
    and keeps a 9-year run tractable.
    """
    tfs = {tf: fold.fold(m1, tf) for tf in ("15m", "1h", "4h", "1d")}
    m15 = tfs["15m"]
    trades: list = []
    open_until = 0            # no new setup while one is live (engine rule 8)

    n = len(m15["ts"])
    for k in range(0, n, step_bars):
        close_ts = int(m15["ts"][k] + m15["tf_s"])
        if progress is not None and k % (step_bars * 500) == 0:
            progress(k, n)

        cut = {tf: _tail(fold.slice_upto(b, close_ts), WINDOW) for tf, b in tfs.items()}
        if len(cut["1d"]["c"]) < engine.MIN_BARS:
            continue

        r = engine.read(symbol, cut["1d"], cut["4h"], cut["1h"], cut["15m"],
                        enable_range_fade=enable_range_fade,
                        in_trade=close_ts < open_until, enabled=enabled)
        if not r.has_setup:
            continue
        s = r.setup
        t = Trade(s.strategy_id, symbol, s.direction, close_ts,
                  s.plan.entry, s.plan.stop, s.plan.target, s.plan.rr)
        out = advance(_AsSetup(t), m1)
        t.status = out.status
        t.net_r, t.mfe_r, t.mae_r = out.net_r, out.mfe_r, out.mae_r
        t.hold_minutes, t.closed_ts = out.hold_minutes, out.closed_ts
        trades.append(t)
        # block re-entry until this one resolves, so the backtest respects the
        # same one-trade-per-symbol rule the live engine enforces
        open_until = (out.closed_ts or close_ts) + 1
    return trades


def _tail(bars: dict, n: int) -> dict:
    return {k: (v if k == "tf_s" else v[-n:]) for k, v in bars.items()}


def summarise(trades: list) -> dict:
    """Honest stats. TIME exits are counted as their real R, not as wins."""
    done = [t for t in trades if t.net_r is not None]
    if not done:
        return dict(n=0, n_issued=len(trades))
    r = np.array([t.net_r for t in done], float)
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    return dict(
        n=len(r), n_issued=len(trades),
        win_rate=round(float((r > 0).mean()), 3),
        avg_net_r=round(float(r.mean()), 3),
        total_r=round(float(r.sum()), 1),
        profit_factor=(round(float(wins.sum() / abs(losses.sum())), 2)
                       if len(losses) and losses.sum() else None),
        max_dd_r=round(dd, 1),
        tp=sum(1 for t in done if t.status == "TP"),
        sl=sum(1 for t in done if t.status == "SL"),
        time_exit=sum(1 for t in done if t.status == "TIME"),
        avg_hold_h=round(float(np.mean([t.hold_minutes or 0 for t in done])) / 60, 1),
        avg_planned_rr=round(float(np.mean([t.planned_rr for t in done])), 2),
    )


def report(trades: list) -> dict:
    """Overall, per strategy, and PER ERA — the breakdown V4 never showed."""
    by_strategy: dict = {}
    for t in trades:
        by_strategy.setdefault(t.strategy_id, []).append(t)
    by_era: dict = {}
    for name, lo, hi in ERAS:
        sel = [t for t in trades if lo <= t.decision_ts < hi]
        if sel:
            by_era[name] = summarise(sel)
    return {"overall": summarise(trades),
            "by_strategy": {k: summarise(v) for k, v in by_strategy.items()},
            "by_era": by_era}

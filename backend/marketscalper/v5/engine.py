"""Per-symbol orchestration: candles in, one Read out.

A Read is always produced — with a setup or without one — because the owner's
question is not only "is there a trade" but "what are you looking at". V4 could
only answer the first, and when it answered "no setup" that was
indistinguishable from the engine being broken (which, on the day it silently
returned zero setups for a full history, it was).

Pure: numpy arrays in, dataclasses out. No I/O, no clock, no randomness. The
backtest and the live path call this same function, which is the only reason a
backtest number means anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import bias as B
from . import regime as R
from . import strategies as ST
from . import structure as S
from . import zones as Z

MIN_BARS = 60
#: How long a setup stays actionable, in TRIGGER bars (15m).
VALID_BARS = 8


@dataclass
class Read:
    """The engine's complete view of one symbol at one moment."""
    symbol: str
    ts: int
    price: float
    bias: B.Bias
    regime: str
    efficiency: float
    daily: S.Structure
    h4: S.Structure
    h1: S.Structure
    m15: S.Structure
    levels: list
    setup: ST.Setup | None
    attempts: list = field(default_factory=list)   # every playbook's verdict
    blocked: str = ""                              # why no setup, at engine level

    @property
    def has_setup(self) -> bool:
        return self.setup is not None


def _targets(direction: int, h1: S.Structure, h4: S.Structure,
             levels: list, price: float) -> list:
    """Structural prices to aim at, NEAREST FIRST.

    Nearest-first matters: geometry.build takes the first candidate that clears
    2R, so ordering by distance means the engine aims at the closest thing that
    is worth aiming at, rather than the furthest thing it can justify.
    """
    out: list = []
    for st, tf in ((h1, "1H"), (h4, "4H")):
        swings = [s for s in st.swings if s.kind == ("high" if direction > 0 else "low")]
        if swings:
            out.append((swings[-1].price, f"{tf} swing {'high' if direction > 0 else 'low'}"))
    for lv in levels:
        out.append((lv.price, lv.label))
    ahead = [(p, lab) for p, lab in out
             if (p > price if direction > 0 else p < price)]
    ahead.sort(key=lambda t: abs(t[0] - price))
    return ahead


def _warm(*bar_sets) -> bool:
    return all(b is not None and len(b.get("c", ())) >= MIN_BARS for b in bar_sets)


#: Which playbooks are live, in priority order. A playbook is only in this
#: tuple once the backtest says it earns its place — the first version of this
#: engine ran all four and the harness showed trend_pullback losing 0.31R a
#: trade while blocking the two that were not losing.
ENABLED = ("trend_pullback", "bos_retest", "level_reclaim", "range_fade")


def read(symbol: str, d1: dict, h4: dict, h1: dict, m15: dict,
         *, enable_range_fade: bool = False,
         in_trade: bool = False,
         enabled: tuple = ENABLED) -> Read:
    """The whole decision procedure, in the order docs/V5/ARCHITECTURE.md §3
    specifies. Every early exit carries a named reason."""
    price = float(h1["c"][-1]) if len(h1.get("c", ())) else 0.0
    ts = int(m15["ts"][-1] + m15["tf_s"]) if len(m15.get("ts", ())) else 0
    warm = _warm(d1, h4, h1, m15)

    st_d1 = S.analyse(d1) if len(d1.get("c", ())) else S.analyse({"h": np.array([]), "l": np.array([]), "c": np.array([]), "ts": np.array([])})
    st_h4 = S.analyse(h4) if len(h4.get("c", ())) else st_d1
    st_h1 = S.analyse(h1) if len(h1.get("c", ())) else st_d1
    st_m15 = S.analyse(m15) if len(m15.get("c", ())) else st_d1

    bias = B.decide(st_d1, st_h4, st_h1, warm=warm)
    reg, eff = R.classify(h4["c"]) if warm else (R.RANGING, float("nan"))
    levels = Z.htf_levels(d1, len(d1["c"]) - 1, price) if warm else []

    out = Read(symbol, ts, price, bias, reg, eff, st_d1, st_h4, st_h1, st_m15,
               levels, None)

    if not warm:
        out.blocked = B.NOT_ENOUGH_DATA
        return out
    if not bias.tradeable:
        out.blocked = bias.reason
        return out
    if in_trade:
        # One live setup per symbol. Stacking correlated entries on one idea is
        # how a 0.5% risk becomes 2% without anyone deciding to take 2%.
        out.blocked = "ALREADY_IN_A_TRADE"
        return out

    d = bias.direction
    targets = _targets(d, st_h1, st_h4, levels, price)
    h1_atr = R.atr(h1)
    h4_atr = R.atr(h4)

    # Priority order. The first playbook that produces a setup wins and the
    # rest are not evaluated — two setups on one symbol is the bug being fixed.
    book = {
        "trend_pullback":
            lambda: ST.trend_pullback(symbol, d, st_h1, st_m15, h1, m15, h1_atr, targets),
        "bos_retest":
            lambda: ST.bos_retest(symbol, d, st_h4, st_m15, h4, m15, h4_atr, targets),
        "level_reclaim":
            lambda: ST.level_reclaim(symbol, d, levels, st_m15, h1, m15, h1_atr, targets),
        "range_fade":
            lambda: ST.range_fade(symbol, d, reg, h4, st_m15, h4_atr, targets,
                                  enabled=enable_range_fade),
    }
    order = [k for k in enabled if k in book]
    if reg == R.RANGING:
        # In a measured range the continuation playbooks are exactly what bled
        # V4 dry. Only the reclaim (a failed break) and the fade are eligible.
        order = [k for k in order if k in ("level_reclaim", "range_fade")]
    plan = [book[k] for k in order]

    for step in plan:
        att = step()
        out.attempts.append(att)
        if att.setup is not None:
            valid_for = VALID_BARS * int(m15["tf_s"])
            out.setup = ST.Setup(
                att.setup.strategy_id, symbol, att.setup.direction, att.setup.plan,
                att.setup.zone, att.setup.reason, att.setup.facts,
                decision_ts=ts, valid_until_ts=ts + valid_for)
            return out

    out.blocked = out.attempts[0].reason if out.attempts else "NO_SETUP"
    return out

"""The single directional view.

This module exists because of one line in V4:

    direction = 1 if lvl >= eval_bars["c"][i] else -1

Direction was derived from where a level happened to sit relative to price, so
every bar produced a long at the donchian high and a short at the donchian low —
on the same symbol, from the same strategy, at the same moment. Ten of the
eleven live losses were shorts issued while the daily was bullish.

Here the direction is decided ONCE, top-down, before any level is looked at. A
setup in the other direction is not arbitrated away later; it can never be
constructed, because every playbook is handed the bias and only builds in it.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import structure as S

LONG, SHORT, NONE = 1, -1, 0

# Named reasons. These are what the virtual trader says when there is no trade —
# "no setup" without a reason is indistinguishable from a broken engine.
NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"
NO_DIRECTIONAL_EDGE = "NO_DIRECTIONAL_EDGE"
TIMEFRAME_CONFLICT = "TIMEFRAME_CONFLICT"
OK = "OK"


@dataclass(frozen=True)
class Bias:
    direction: int          # LONG / SHORT / NONE
    reason: str
    daily: str              # the 1D structure state
    h4: str                 # the 4H structure state
    h1: str                 # the 1H structure state
    source: str             # which timeframe decided it

    @property
    def tradeable(self) -> bool:
        return self.direction != NONE and self.reason == OK

    @property
    def label(self) -> str:
        return {LONG: "LONG", SHORT: "SHORT"}.get(self.direction, "FLAT")


_FROM_STATE = {S.BULLISH: LONG, S.BEARISH: SHORT}


def decide(daily: S.Structure, h4: S.Structure, h1: S.Structure,
           *, warm: bool = True) -> Bias:
    """Top-down: the daily decides, the 4H may veto, the 1H is context only.

    The 1H deliberately gets NO vote. It flips constantly inside a healthy
    trend — that is what a pullback is — and giving it a vote is precisely how
    V4's `structure_trend` authorised shorts during a rally.
    """
    d, f, o = daily.state, h4.state, h1.state
    if not warm:
        return Bias(NONE, NOT_ENOUGH_DATA, d, f, o, "none")

    direction = _FROM_STATE.get(d, NONE)
    source = "1D"
    if direction == NONE:
        # A ranging daily is not a veto — it is an absence of opinion, so the
        # 4H may speak. A ranging 4H under it means nobody has a view.
        direction = _FROM_STATE.get(f, NONE)
        source = "4H"
        if direction == NONE:
            return Bias(NONE, NO_DIRECTIONAL_EDGE, d, f, o, "none")
        return Bias(direction, OK, d, f, o, source)

    # The daily has a view. The 4H may only contradict it, never refine it.
    opposed = _FROM_STATE.get(f, NONE)
    if opposed != NONE and opposed != direction:
        return Bias(NONE, TIMEFRAME_CONFLICT, d, f, o, "1D vs 4H")
    return Bias(direction, OK, d, f, o, source)

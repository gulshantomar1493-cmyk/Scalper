"""The playbooks.

Every one of them receives the bias and may only build in that direction — the
contrary setup is not rejected later, it is never constructed. That is the
structural answer to "a long and a short on the same symbol at the same time".

Each returns either a Setup or a NAMED REASON for standing aside. "No setup" on
its own is indistinguishable from a broken engine; the reason is what the
virtual trader reports, and it is the difference between a tool you can follow
and a tool you have to trust blindly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import geometry as G
from . import regime as R
from . import structure as S
from . import zones as Z

# ---- reasons -------------------------------------------------------------
NO_IMPULSE = "NO_IMPULSE"
NOT_IN_ZONE = "NOT_IN_ZONE"
SHALLOW_PULLBACK = "SHALLOW_PULLBACK"
DEEP_PULLBACK = "DEEP_PULLBACK"
NO_CONFIRMATION = "NO_CONFIRMATION"
NO_ZONE = "NO_ZONE"
RR_TOO_LOW = "RR_TOO_LOW"
WRONG_REGIME = "WRONG_REGIME"
NO_RECENT_BOS = "NO_RECENT_BOS"
NOT_AT_LEVEL = "NOT_AT_LEVEL"
NO_RECLAIM = "NO_RECLAIM"
DISABLED = "DISABLED"

#: How far into the leg a pullback must come. Above 0.786 the leg is close to
#: being invalidated and the "trend" reading is about to change anyway.
RETRACE_MIN = 0.382
RETRACE_MAX = 0.786


@dataclass(frozen=True)
class Setup:
    strategy_id: str
    symbol: str
    direction: int
    plan: G.Plan
    zone: Z.Zone | None
    reason: str                       # human sentence, not a code
    facts: list = field(default_factory=list)
    decision_ts: int = 0
    valid_until_ts: int = 0


@dataclass(frozen=True)
class Attempt:
    """What a playbook did on this bar — a setup, or why not."""
    strategy_id: str
    setup: Setup | None
    reason: str                       # a REASON constant when setup is None
    detail: str = ""


def _confirmed(m15: S.Structure, direction: int, since_ts: int) -> S.Break | None:
    """A break on the trigger timeframe, in our direction, after `since_ts`.

    This is the piece V4 had no equivalent of. V4 rested a stop order at a level
    and was filled by the move that broke it — including every false break. Here
    price must first come to us, and then the lower timeframe must turn.
    """
    for b in reversed(m15.breaks):
        if b.ts <= since_ts:
            break
        if b.direction == direction:
            return b
    return None


# ------------------------------------------------------------ 1. pullback --

def trend_pullback(symbol, direction, h1: S.Structure, m15: S.Structure,
                   h1_bars, m15_bars, h1_atr, targets) -> Attempt:
    """The core playbook: buy the retracement of an up-leg, in an uptrend.

    The stop sits under the zone that produced the leg, which is close — and
    that closeness is the entire reason a 1:3 is reachable without needing the
    market to trend for three days.
    """
    sid = "trend_pullback"
    imp = S.last_impulse(h1, direction)
    if imp is None:
        return Attempt(sid, None, NO_IMPULSE, "1H par koi saaf leg nahi bani")
    origin, extreme = imp

    price = float(h1_bars["c"][-1])
    depth = S.retracement(origin.price, extreme.price, price) if direction > 0 \
        else S.retracement(extreme.price, origin.price, price)
    if depth < RETRACE_MIN:
        return Attempt(sid, None, SHALLOW_PULLBACK,
                       f"pullback abhi sirf {depth:.0%} — {RETRACE_MIN:.0%} chahiye")
    if depth > RETRACE_MAX:
        return Attempt(sid, None, DEEP_PULLBACK,
                       f"pullback {depth:.0%} — leg tootne wali hai, entry nahi")

    zone = Z.zone_before(h1_bars, extreme.i, direction)
    if zone is None:
        return Attempt(sid, None, NO_ZONE, "impulse se pehle koi opposite candle nahi mili")
    zone = Z.mark_tested(zone, h1_bars, len(h1_bars["c"]) - 1)
    if not zone.contains(price):
        return Attempt(sid, None, NOT_IN_ZONE,
                       f"zone {zone.lo:,.2f}–{zone.hi:,.2f} tak aana baaki hai")

    conf = _confirmed(m15, direction, zone.ts)
    if conf is None:
        return Attempt(sid, None, NO_CONFIRMATION,
                       "zone mein hain par 15m par turn confirm nahi hua")

    atr = float(h1_atr[-1]) if len(h1_atr) and h1_atr[-1] == h1_atr[-1] else 0.0
    extreme_px = zone.lo if direction > 0 else zone.hi
    stop = G.structural_stop(extreme_px, direction, atr)
    plan = G.build(price, stop, direction, targets)
    if plan is None:
        return Attempt(sid, None, RR_TOO_LOW,
                       "agla structural target 2R se kam hai — chhod diya")
    return Attempt(sid, Setup(
        sid, symbol, direction, plan, zone,
        "Trend ke saath pullback: zone mein price aayi aur 15m ne turn confirm kiya.",
        [f"1H structure {h1.state}",
         f"pullback {depth:.0%} of the leg",
         f"zone {zone.lo:,.2f}–{zone.hi:,.2f}{' (pehle test ho chuka)' if zone.tested else ''}",
         f"15m {conf.kind} {'up' if conf.direction > 0 else 'down'} @ {conf.level:,.2f}",
         f"target: {plan.target_label}"]), "")


# ----------------------------------------------------------- 2. bos retest --

def bos_retest(symbol, direction, h4: S.Structure, m15: S.Structure,
               h4_bars, m15_bars, h4_atr, targets, *, max_age_bars=10) -> Attempt:
    """Price broke a 4H swing and came back to it. Trade the retest, not the
    break — the break is where V4 was filled and where the false ones live."""
    sid = "bos_retest"
    bos = h4.last_bos
    if bos is None or bos.direction != direction:
        return Attempt(sid, None, NO_RECENT_BOS, "4H par is direction mein koi taaza BOS nahi")
    last_i = len(h4_bars["c"]) - 1
    if last_i - bos.i > max_age_bars:
        return Attempt(sid, None, NO_RECENT_BOS,
                       f"BOS {last_i - bos.i} bars purana ho gaya")

    atr = float(h4_atr[-1]) if len(h4_atr) and h4_atr[-1] == h4_atr[-1] else 0.0
    tol = 0.25 * atr
    price = float(h4_bars["c"][-1])
    if abs(price - bos.level) > tol:
        return Attempt(sid, None, NOT_AT_LEVEL,
                       f"toota hua level {bos.level:,.2f} abhi door hai")

    conf = _confirmed(m15, direction, bos.ts)
    if conf is None:
        return Attempt(sid, None, NO_CONFIRMATION, "retest par 15m confirmation nahi")

    stop = G.structural_stop(bos.level - direction * tol, direction, atr)
    plan = G.build(price, stop, direction, targets)
    if plan is None:
        return Attempt(sid, None, RR_TOO_LOW, "retest ka target 2R tak nahi pahunchta")
    return Attempt(sid, Setup(
        sid, symbol, direction, plan, None,
        "Toote hue level ka retest, trend ke saath.",
        [f"4H BOS @ {bos.level:,.2f}",
         f"{last_i - bos.i} bars pehle toota",
         f"15m {conf.kind} confirm",
         f"target: {plan.target_label}"]), "")


# -------------------------------------------------------- 3. level reclaim --

def level_reclaim(symbol, direction, levels, m15: S.Structure,
                  h1_bars, m15_bars, h1_atr, targets) -> Attempt:
    """Price traded through a higher-timeframe level and closed back inside it.

    The journal found that FADING a level loses. The claim here is narrower: not
    that the level holds, but that a failed break of it — a close back through
    after trading beyond — is information. Unproven until the backtest says so.
    """
    sid = "level_reclaim"
    if not levels:
        return Attempt(sid, None, NOT_AT_LEVEL, "koi HTF level paas mein nahi")
    o, h, l, c = h1_bars["o"], h1_bars["h"], h1_bars["l"], h1_bars["c"]
    i = len(c) - 1
    if i < 2:
        return Attempt(sid, None, NO_RECLAIM, "data kam hai")
    atr = float(h1_atr[-1]) if len(h1_atr) and h1_atr[-1] == h1_atr[-1] else 0.0

    for lv in levels:
        if direction > 0:
            swept = l[i] < lv.price and c[i] > lv.price
            extreme_px = float(l[i])
        else:
            swept = h[i] > lv.price and c[i] < lv.price
            extreme_px = float(h[i])
        if not swept:
            continue
        conf = _confirmed(m15, direction, int(m15_bars["ts"][max(0, len(m15_bars["ts"]) - 8)]))
        if conf is None:
            return Attempt(sid, None, NO_CONFIRMATION,
                           f"{lv.label} reclaim hua par 15m confirm nahi")
        stop = G.structural_stop(extreme_px, direction, atr)
        plan = G.build(float(c[i]), stop, direction, targets)
        if plan is None:
            return Attempt(sid, None, RR_TOO_LOW, "reclaim ka target 2R tak nahi")
        return Attempt(sid, Setup(
            sid, symbol, direction, plan, None,
            f"{lv.label} ko tod ke wapas andar close — failed break.",
            [f"{lv.label} @ {lv.price:,.2f}",
             f"wick {extreme_px:,.2f} tak gayi, close {c[i]:,.2f}",
             f"15m {conf.kind} confirm",
             f"target: {plan.target_label}"]), "")
    return Attempt(sid, None, NO_RECLAIM, "kisi level par failed break nahi bana")


# ------------------------------------------------------------ 4. range fade --

def range_fade(symbol, direction, regime, h4_bars, m15: S.Structure,
               h4_atr, targets, *, enabled=False) -> Attempt:
    """Boundary fade inside a measured range.

    Ships OFF. The V4 journal found mean reversion gross-negative in every
    regime — but it never tested reversion conditioned on a MEASURED range plus
    lower-timeframe confirmation. That is a hypothesis, and it stays disabled
    until the backtest rules on it.
    """
    sid = "range_fade"
    if not enabled:
        return Attempt(sid, None, DISABLED,
                       "range fade band hai — research ne mean reversion ko reject kiya tha")
    if regime != R.RANGING:
        return Attempt(sid, None, WRONG_REGIME, "market range mein nahi hai")
    return Attempt(sid, None, NO_CONFIRMATION, "range fade abhi implement nahi hua")

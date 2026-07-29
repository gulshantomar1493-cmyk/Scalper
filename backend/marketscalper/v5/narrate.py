"""The virtual trader.

It speaks on every closed candle whether or not there is a trade, because "no
setup" alone is worthless: it reads the same whether the engine is patient or
broken. Every sentence is built from state the engine actually computed — if it
cannot be named in §2 of the architecture, it cannot be said here.

Pure formatting. No decisions are made in this file.
"""
from __future__ import annotations

from . import bias as B
from . import regime as R
from . import strategies as ST

_STATE_HI = {"BULLISH": "BULLISH", "BEARISH": "BEARISH", "RANGE": "RANGE"}
_REGIME_HI = {R.TRENDING: "trend chal raha hai", R.MIXED: "mila-jula",
              R.RANGING: "range mein hai"}

#: Engine-level blocks, in the owner's language. Each says what is missing, not
#: merely that something is.
_BLOCKED = {
    B.NOT_ENOUGH_DATA: "Abhi itna data nahi hai ki structure padha ja sake.",
    B.NO_DIRECTIONAL_EDGE:
        "Daily aur 4H dono range mein hain — koi direction hi nahi ban rahi, "
        "isliye koi trade nahi.",
    B.TIMEFRAME_CONFLICT:
        "Daily aur 4H ulta bol rahe hain. Aise waqt par trade lena hi V4 ki "
        "sabse badi galti thi — hum ruk rahe hain.",
    "ALREADY_IN_A_TRADE":
        "Is symbol par ek trade already chal raha hai. Ek idea par do baar "
        "paisa nahi lagate.",
}

_ATTEMPT = {
    ST.NO_IMPULSE: "1H par abhi koi saaf leg nahi bani",
    ST.SHALLOW_PULLBACK: "pullback abhi bahut chhota hai",
    ST.DEEP_PULLBACK: "pullback itna gehra ho gaya ki leg hi khatre mein hai",
    ST.NOT_IN_ZONE: "price abhi zone tak aayi nahi",
    ST.NO_ZONE: "impulse ke pehle koi saaf zone nahi mila",
    ST.NO_CONFIRMATION: "zone mein hain, par 15m par turn confirm nahi hua",
    ST.RR_TOO_LOW: "agla structural target 2R se kam de raha hai",
    ST.NO_RECENT_BOS: "4H par koi taaza break nahi",
    ST.NOT_AT_LEVEL: "toota hua level abhi door hai",
    ST.NO_RECLAIM: "kisi level par failed break nahi bana",
    ST.WRONG_REGIME: "is regime mein ye playbook nahi chalti",
    ST.DISABLED: "ye playbook band hai",
}


def _dec(symbol: str) -> int:
    return 0 if symbol.startswith("BTC") else 2


def narrate(read) -> str:
    """The full reading, as the trader would say it out loud."""
    d = _dec(read.symbol)
    lines = [f"{read.symbol} — {read.bias.label} ka mood"]
    lines.append(
        f"Daily: {_STATE_HI.get(read.daily.state, read.daily.state)}.  "
        f"4H: {_STATE_HI.get(read.h4.state, read.h4.state)}, "
        f"{_REGIME_HI.get(read.regime, read.regime)}"
        + (f" (efficiency {read.efficiency:.2f})" if read.efficiency == read.efficiency else "")
        + f".  1H: {_STATE_HI.get(read.h1.state, read.h1.state)}.")
    lines.append(f"Price {read.price:,.{d}f}.")

    if read.setup is not None:
        s = read.setup
        p = s.plan
        lines.append(
            f"Setup mila: {_pretty(s.strategy_id)} — entry {p.entry:,.{d}f}, "
            f"stop {p.stop:,.{d}f}, target {p.target:,.{d}f} ({p.rr:.1f}R net). "
            f"Target {p.target_label} par rakha hai.")
        lines.append(s.reason)
        return "\n".join(lines)

    if read.blocked in _BLOCKED:
        lines.append(_BLOCKED[read.blocked])
        return "\n".join(lines)

    waiting = [a for a in read.attempts if a.setup is None]
    if waiting:
        first = waiting[0]
        detail = first.detail or _ATTEMPT.get(first.reason, first.reason)
        lines.append(f"Wait kar rahe hain — {_pretty(first.strategy_id)}: {detail}.")
        others = [f"{_pretty(a.strategy_id)}: {_ATTEMPT.get(a.reason, a.reason)}"
                  for a in waiting[1:]]
        if others:
            lines.append("Baaki playbooks — " + "; ".join(others) + ".")
    else:
        lines.append("Koi playbook abhi eligible nahi hai.")
    return "\n".join(lines)


def _pretty(strategy_id: str) -> str:
    return {
        "trend_pullback": "Trend Pullback",
        "bos_retest": "BOS Retest",
        "level_reclaim": "Level Reclaim",
        "range_fade": "Range Fade",
    }.get(strategy_id, strategy_id)


def headline(read) -> str:
    """One line, for a list row or an alert."""
    if read.setup is not None:
        s = read.setup
        return (f"{read.symbol} {'LONG' if s.direction > 0 else 'SHORT'} — "
                f"{_pretty(s.strategy_id)} {s.plan.rr:.1f}R")
    if read.blocked in _BLOCKED:
        return f"{read.symbol} — {read.bias.label}, koi trade nahi"
    return f"{read.symbol} — {read.bias.label}, wait"

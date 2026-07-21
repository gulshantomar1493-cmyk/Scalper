"""Trade Engine V2 — professional discretionary setup engine (decision-support).

Rewritten after the forensic trading audit (docs/V2/ENGINE-AUDIT.md). It does NOT
re-detect anything — it orchestrates what the frozen engines already produce
(HtfService bias + the live 1m `structure` payload: trend, liquidity {pools,
premium_discount, sweeps, shifts}, order blocks, FVGs, confluence, the frozen
StrategyEngine `signals`, qualification) into HTF-gated, fully-explained setups —
or a confident "No high-probability setup available." It NEVER executes.

Confidence is NOT a fabricated percentage (the §0.3 rule + the audit). A setup is
first passed through **necessary gates** — the things that must be true or there
is simply no trade: a fresh trigger, never fighting a convinced higher timeframe,
a liquidity sweep THEN a structure shift, a valid LOCATION (the correct half of
the range or a named order block / FVG at the entry), and net-of-fees R:R. Only
then does a **grade emerge from how many independent confluences agree** — A+ /
A / B, shown as "N of M confluences", never a number to a decimal.

Every setup reads like a trader's card: a market narrative (who controls, what
liquidity was taken, where the draw is, where price goes next & why), the setup
type, primary/secondary confluence, the six "why"s, the honest **reasons to
avoid**, early-exit conditions, and management notes.

Pure + deterministic. Engine-isolated — no bus, no structure write, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MIN_RR = 1.5                 # net-of-fees reward:risk to the next pool — a gate
TAKER_FEE = 0.0005           # Delta taker/side (matches engines/risk.py); round trip ×2
CONVINCED_HTF = 0.5          # HTF agreement fraction above which we never fight it
STRONG_HTF = 0.6             # HTF agreement fraction that counts as a real confluence

_LONG, _SHORT = "LONG", "SHORT"
_BULLISH, _BEARISH = "BULLISH", "BEARISH"
_SETUP_TYPE = {"S1": "Liquidity Sweep Reversal", "S2": "Trend Pullback",
               "S3": "Fake-Break Trap"}


@dataclass(frozen=True)
class TradeSetupV2:
    # FROZEN contract v1.0 (docs/V2/API-CONTRACT.md). Field names / enums stable.
    symbol: str                    # e.g. "BTCUSDT"
    direction: str                 # enum: LONG | SHORT
    setup_type: str                # enum: Liquidity Sweep Reversal | Trend Pullback | Fake-Break Trap
    grade: str                     # enum: A+ | A | B (emergent, NOT a probability)
    confluences: int               # how many independent confluences aligned (1..5)
    confluences_total: int         # the maximum (always 5) — render "{confluences}/{total}"
    risk_level: str                # enum: LOW | MEDIUM | HIGH
    entry: float                   # suggested entry price
    sl: float                      # stop loss price
    tp1: float                     # first target (the nearer pool)
    tp2: float | None              # second target, or null if none beyond TP1
    rr: float                      # NET-of-fees reward:risk to TP1 (>= 1.5)
    htf_bias: str                  # enum: BULLISH | BEARISH | NEUTRAL (the direction filter)
    ltf_trend: str                 # enum: BULLISH | BEARISH | RANGE | UNKNOWN (the 1m structure)
    market_context: str            # the narrative — who controls / what was taken / the draw / next
    reasons: tuple                 # the aligned confluences (clean strings; UI adds any ✓)
    reasons_to_avoid: tuple        # the honest bear case for this idea
    invalidation: str              # the price/condition that voids the idea
    early_exit: tuple              # conditions to bail before the stop
    management_notes: tuple        # display-only management guidance
    holding_time: str              # enum: INTRADAY (minutes to a few hours)
    why: dict                      # 6 keys: why_exists/why_now/why_entry/why_sl/why_targets/why_edge
    created_ts: str | None         # ISO-8601 UTC of the trigger, or null


# ------------------------------------------------------------------ helpers

def _bias_of(direction: str) -> str:
    return _BULLISH if direction == _LONG else _BEARISH


def _aligned(direction: str, htf_bias: str) -> bool:
    return _bias_of(direction) == htf_bias


def _net_rr(entry: float, sl: float, tp: float) -> float | None:
    """Net-of-fees reward:risk (round-trip taker fee) — the frozen risk.py convention."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    fee = abs(entry) * TAKER_FEE * 2.0
    net_reward = abs(tp - entry) - fee
    net_risk = risk + fee
    return net_reward / net_risk if net_risk > 0 else None


def _prem_disc_ok(direction: str, pd_state) -> bool:
    if not pd_state:
        return False
    s = str(pd_state).upper()
    return (direction == _LONG and "DISCOUNT" in s) or \
           (direction == _SHORT and "PREMIUM" in s)


def _has_sweep_then_shift(ltf: dict) -> bool:
    liq = ltf.get("liquidity") or {}
    return bool(liq.get("shifts")) and bool(liq.get("sweeps"))


def _named_zone(direction: str, entry: float, ltf: dict) -> str | None:
    """A specific, direction-matched, still-live order block / FVG whose zone
    holds the entry — the precise location a trader would name."""
    want = "BULL" if direction == _LONG else "BEAR"
    for b in ((ltf.get("orderblocks") or {}).get("blocks") or []):
        if b.get("direction") == want and b.get("status") != "broken":
            lo, hi = b.get("lo"), b.get("hi")
            if lo is not None and hi is not None and lo <= entry <= hi:
                st = b.get("status")
                tag = "unmitigated " if st == "active" else ""
                return f"entry at an {tag}{'bullish' if direction == _LONG else 'bearish'} order block"
    for g in (ltf.get("fvgs") or []):
        if g.get("direction") == want and g.get("status") not in ("filled",):
            lo, hi = g.get("lo"), g.get("hi")
            if lo is not None and hi is not None and lo <= entry <= hi:
                return f"entry into a {'bullish' if direction == _LONG else 'bearish'} fair-value gap"
    return None


def _volume_confirms(direction: str, ltf: dict) -> bool:
    vol = ltf.get("volume") or {}
    rvol, cd = vol.get("rvol"), vol.get("cum_delta")
    if rvol is None or rvol < 1.2 or cd is None:
        return False
    return (direction == _LONG and cd > 0) or (direction == _SHORT and cd < 0)


def _draw_on_liquidity(direction: str, entry: float, ltf: dict) -> float | None:
    """The nearest UNswept pool price in the trade direction — where price is
    drawn (buy-side above for longs, sell-side below for shorts)."""
    pools = (ltf.get("liquidity") or {}).get("pools") or []
    best = None
    for p in pools:
        price, kind = p.get("price"), str(p.get("kind", "")).upper()
        if price is None:
            continue
        high_side = kind.startswith("EQH") or "HIGH" in kind
        low_side = kind.startswith("EQL") or "LOW" in kind
        if direction == _LONG and high_side and price > entry:
            best = price if best is None else min(best, price)
        elif direction == _SHORT and low_side and price < entry:
            best = price if best is None else max(best, price)
    return best


def _last_sweep(ltf: dict):
    sweeps = (ltf.get("liquidity") or {}).get("sweeps") or []
    return sweeps[-1] if sweeps else None


def _htf_choch_against(direction: str, htf: dict | None) -> str | None:
    """A higher timeframe that just printed a CHOCH against the trade = a
    transition warning (label of the offending tf), else None."""
    want_up = direction == _LONG
    for tf, a in ((htf or {}).get("timeframes") or {}).items():
        ch = (a or {}).get("choch")
        if ch and ((ch.get("direction") == "DOWN") == want_up):
            return tf
    return None


def _fresh(sig: dict, now_ts: datetime | None) -> bool:
    if now_ts is None:
        return True
    try:
        created = datetime.fromisoformat(sig["created_ts"])
    except (KeyError, TypeError, ValueError):
        return True
    bars = sig.get("invalid_after_bars") or 5
    age_min = (now_ts - created).total_seconds() / 60.0
    return 0 <= age_min <= bars


def _narrative(direction: str, htf_bias: str, htf_conf: float, ltf: dict,
               entry: float, draw: float | None) -> str:
    control = ({_BULLISH: "Buyers control the higher timeframe",
               _BEARISH: "Sellers control the higher timeframe"}
              .get(htf_bias, "The higher timeframe is balanced — no clear control"))
    sw = _last_sweep(ltf)
    if sw:
        side = str(sw.get("side", "")).upper()
        px = sw.get("price")
        taken = ("sell-side liquidity was just swept" if side == "LOW"
                 else "buy-side liquidity was just swept" if side == "HIGH"
                 else "resting liquidity was just swept")
        if px is not None:
            taken += f" at {px}"
        trapped = ("the late shorts who sold that low are now trapped" if side == "LOW"
                   else "the breakout longs are now trapped" if side == "HIGH"
                   else "traders on the wrong side are trapped")
    else:
        taken, trapped = "liquidity was raided", "the trapped side must cover"
    trend = ltf.get("trend") or "UNKNOWN"
    choch = (ltf.get("choch") or [])
    phase = ("transitioning (a recent change of character)" if choch
             else "trending" if trend in (_BULLISH, _BEARISH) else "ranging")
    where = (f"Price is likely drawn to the {'buy' if direction == _LONG else 'sell'}-side "
             f"liquidity at {draw}" if draw is not None else
             f"Price is likely to continue {'higher' if direction == _LONG else 'lower'} toward the next pool")
    return (f"{control}. {taken.capitalize()} and structure shifted — {trapped}. "
            f"The 1m is {phase}. {where}, and the {'long' if direction == _LONG else 'short'} "
            f"is the execution of that story.")


def _risk_level(grade: str, aligned: bool) -> str:
    if grade == "A+" and aligned:
        return "LOW"
    if grade in ("A+", "A"):
        return "MEDIUM"
    return "HIGH"


# ------------------------------------------------------------------ the engine

def build_setups(symbol: str, htf: dict | None, ltf: dict | None,
                 now_ts: datetime | None = None) -> list[TradeSetupV2]:
    """HIGH-probability, HTF-gated, fully-explained setups for one symbol (empty =
    "No high-probability setup available"). Necessary gates first; grade emerges
    from confluence agreement."""
    if not ltf:
        return []
    overall = (htf or {}).get("overall") or {}
    htf_bias = overall.get("bias") or "NEUTRAL"
    raw_conf = float(overall.get("confidence") or 0.0)
    htf_conf = raw_conf / 100.0 if raw_conf > 1.5 else raw_conf   # HtfService gives 0..100
    ltf_trend = ltf.get("trend") or "UNKNOWN"
    data_ok = (ltf.get("qualification") or {}).get("data_integrity") == "PASS"
    pd_state = (ltf.get("liquidity") or {}).get("premium_discount")

    out: list[TradeSetupV2] = []
    for sig in (ltf.get("signals") or []):
        direction = sig.get("direction")
        entry, sl, tp1, tp2 = sig.get("entry"), sig.get("sl"), sig.get("tp1"), sig.get("tp2")
        if direction not in (_LONG, _SHORT) or entry is None or sl is None or tp1 is None:
            continue

        # ---- NECESSARY GATES (all must hold, or there is no trade) ----
        if not _fresh(sig, now_ts):
            continue                                    # not "now"
        if not data_ok:
            continue                                    # never trust a bad feed
        aligned = _aligned(direction, htf_bias)
        if htf_bias in (_BULLISH, _BEARISH) and htf_conf >= CONVINCED_HTF and not aligned:
            continue                                    # never fight a convinced HTF
        if not _has_sweep_then_shift(ltf):
            continue                                    # no sweep->shift trigger
        pd_ok = _prem_disc_ok(direction, pd_state)
        zone = _named_zone(direction, entry, ltf)
        if not (pd_ok or zone):
            continue                                    # no valid LOCATION -> no setup
        rr = _net_rr(entry, sl, tp1)
        if rr is None or rr < MIN_RR:
            continue                                    # structure doesn't pay enough

        # ---- SUPPORTING CONFLUENCES (grade emerges from agreement) ----
        vol_ok = _volume_confirms(direction, ltf)
        confl: list[str] = []
        if aligned:
            confl.append(f"aligned with the {htf_bias.lower()} higher-timeframe bias")
        if aligned and htf_conf >= STRONG_HTF:
            confl.append(f"strong HTF conviction ({round(htf_conf * 100)}% timeframe agreement)")
        if zone:
            confl.append(zone)
        if pd_ok:
            confl.append(f"entry in {'discount' if direction == _LONG else 'premium'} — the correct half of the range")
        if vol_ok:
            confl.append("volume confirms (elevated rvol, delta leaning with the move)")
        n = len(confl)
        grade = "A+" if n >= 4 else ("A" if n >= 2 else "B")
        risk_level = _risk_level(grade, aligned)

        draw = _draw_on_liquidity(direction, entry, ltf)
        context = _narrative(direction, htf_bias, htf_conf, ltf, entry, draw)

        # honest bear case — a professional always argues against their own idea
        avoid: list[str] = []
        if not aligned:
            avoid.append("the higher timeframe has no clear bias in this direction — this is a range reaction, not a trend trade")
        elif htf_conf < STRONG_HTF:
            avoid.append(f"HTF agreement is only {round(htf_conf * 100)}% — the timeframes are mixed")
        bad_tf = _htf_choch_against(direction, htf)
        if bad_tf:
            avoid.append(f"the {bad_tf} recently printed a CHOCH against this direction — momentum may be turning")
        if not vol_ok:
            avoid.append("volume / delta do not yet confirm the move")
        if rr < 2.0:
            avoid.append(f"net R:R is only {rr:.2f} — limited room to the next pool")
        avoid.append("if the swept level is reclaimed, the reversal thesis is void — do not average down")

        side = "long" if direction == _LONG else "short"
        why = {
            "why_exists": (f"a with-bias {side}: liquidity was swept and structure shifted into a "
                           f"{'discount' if direction == _LONG else 'premium'} location"),
            "why_now": "the sweep + shift just printed and the trigger is still inside its validity window",
            "why_entry": (zone or f"the {'discount' if direction == _LONG else 'premium'} zone at {entry}")
                         + f" left by the shift",
            "why_sl": f"beyond {sl}, the swept extreme — the price that proves the read wrong",
            "why_targets": (f"the draw at {draw}" if draw is not None else f"the next pool at {tp1}")
                           + f" (TP1 {tp1}" + (f", TP2 {tp2}" if tp2 else "") + f"; net R:R {rr:.2f})",
            "why_edge": (f"{'an' if grade in ('A+', 'A') else 'a'} {grade} setup: {n} "
                         f"independent confluences agree on a with-context "
                         f"{side} after a confirmed liquidity raid + structure shift into a valid "
                         f"location — the sweep→shift→zone pattern, not an indicator signal"),
        }
        early = (
            "price fails to displace away from the zone within a few bars (no follow-through)",
            "an opposing change-of-character prints on the entry timeframe",
            "the swept level is reclaimed and holds",
        )
        manage = (
            f"risk to {sl} only; size so the loss is a fixed, small % of the account",
            "move the stop to break-even once price reaches +1R",
            f"take partials at TP1 ({tp1}); trail the remainder toward "
            + (f"TP2 ({tp2})" if tp2 else "the next pool"),
        )
        out.append(TradeSetupV2(
            symbol=symbol, direction=direction,
            setup_type=_SETUP_TYPE.get(sig.get("strategy"), "Discretionary"),
            grade=grade, confluences=n, confluences_total=5, risk_level=risk_level,
            entry=float(entry), sl=float(sl), tp1=float(tp1),
            tp2=(float(tp2) if tp2 is not None else None), rr=round(rr, 2),
            htf_bias=htf_bias, ltf_trend=ltf_trend, market_context=context,
            reasons=tuple(confl), reasons_to_avoid=tuple(avoid),
            invalidation=f"a decisive close beyond {sl} voids the idea",
            early_exit=early, management_notes=manage, holding_time="INTRADAY",
            why=why, created_ts=sig.get("created_ts")))

    # best idea first: grade, then more confluences, then better R:R
    _rank = {"A+": 3, "A": 2, "B": 1}
    out.sort(key=lambda s: (_rank.get(s.grade, 0), s.rr), reverse=True)
    return out

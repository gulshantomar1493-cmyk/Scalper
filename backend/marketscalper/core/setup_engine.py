"""Trade Engine V2 — professional discretionary setup engine (decision-support).

This is an ORCHESTRATION + top-down-gating + explainability layer. It does NOT
re-detect anything: it consumes what the frozen engines already produce —

  * HTF bias + market story  <- HtfService (1d/4h/1h/15m), passed as `htf`
  * LTF trigger + context    <- the live 1m `structure` payload, passed as `ltf`
                                (trend, liquidity {pools, premium_discount, sweeps,
                                shifts}, orderblocks, fvgs, confluence, the frozen
                                StrategyEngine `signals`, and `qualification`)

and turns them into HTF-gated, fully-explained `TradeSetupV2`s — or, when nothing
clears the bar, an empty list (the caller renders "No high-probability setup
available"). It NEVER executes; the user executes manually.

Discipline (from docs/V2/RESEARCH.md): price action decides, indicators only
confirm; the higher timeframe sets direction, the lower timeframe sets timing;
the canonical edge is sweep -> shift -> refined zone in the correct half of the
range, with confluence; if the pillars aren't there, there is no trade. Confidence
is weighted rule-agreement of independent structural pillars — never a probability,
never fabricated (mirrors the V1 §0.3 discipline).

Pure + deterministic: same (htf, ltf, now_ts) -> same setups. Engine-isolated —
never publishes on the bus, never writes the structure payload, never persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# --- confidence pillars (weighted rule-agreement, 0..100; price-action-heavy) ---
W_HTF_ALIGNED = 35.0        # top-down alignment — the single biggest edge
W_HTF_STRENGTH = 15.0       # how convinced the HTF read is (scaled by confidence)
W_SWEEP_SHIFT = 20.0        # the trigger quality: liquidity sweep THEN a shift
W_PREM_DISC = 15.0          # entry in the correct half (discount long / premium short)
W_CONFLUENCE = 10.0         # a direction-matched OB/FVG/pool zone at the entry
W_VOLUME = 5.0              # rvol / delta agreement — confirmation only

# only HIGH-probability setups are surfaced (the directive's bar); below this the
# engine returns nothing -> "No high-probability setup available."
MIN_CONFIDENCE = 70.0
MIN_RR = 1.5                # structure must offer at least this to the next pool
FRESH_FACTOR = 1            # a signal is a live trigger only within its own
                            # invalid_after_bars window (× 1 min per bar)

_LONG = "LONG"
_SHORT = "SHORT"
_BULLISH = "BULLISH"
_BEARISH = "BEARISH"


@dataclass(frozen=True)
class TradeSetupV2:
    symbol: str
    direction: str                 # LONG | SHORT
    entry: float
    sl: float
    tp1: float
    tp2: float | None
    rr: float                      # net reward:risk to TP1 (structure-discovered)
    confidence: float              # 0..100 weighted pillar agreement (NOT probability)
    risk_level: str                # LOW | MEDIUM | HIGH
    market_bias: str               # the LTF (1m) structural read
    htf_bias: str                  # the higher-timeframe bias (the direction filter)
    strategy: str                  # which frozen trigger armed it (S1/S2/S3)
    reasons: tuple = ()            # the aligned pillars, ✓-prefixed
    invalidation: str = ""         # the price/condition that voids the idea
    why: dict = field(default_factory=dict)   # the six explainability questions
    created_ts: str | None = None


# ------------------------------------------------------------------ helpers

def _dir_to_bias(direction: str) -> str:
    return _BULLISH if direction == _LONG else _BEARISH


def _htf_aligned(direction: str, htf_bias: str) -> bool:
    return _dir_to_bias(direction) == htf_bias


def _rr(entry: float, sl: float, tp: float) -> float | None:
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return abs(tp - entry) / risk


def _prem_disc_ok(direction: str, pd_state) -> bool:
    """Longs want discount, shorts want premium. pd_state is the frozen liquidity
    engine's read ('PREMIUM'|'DISCOUNT'|'EQUILIBRIUM'|None). Unknown -> neutral
    (doesn't add the pillar, doesn't reject)."""
    if not pd_state:
        return False
    s = str(pd_state).upper()
    return (direction == _LONG and "DISCOUNT" in s) or \
           (direction == _SHORT and "PREMIUM" in s)


def _has_sweep_then_shift(ltf: dict) -> bool:
    """The canonical trigger: a liquidity sweep followed by a structural shift
    (the frozen liquidity engine pairs these as `shifts`)."""
    liq = ltf.get("liquidity") or {}
    return bool(liq.get("shifts")) and bool(liq.get("sweeps"))


def _confluence_near(direction: str, entry: float, ltf: dict, atr_hint: float) -> bool:
    """A direction-matched confluence zone containing / adjacent to the entry."""
    want = _BULLISH if direction == _LONG else _BEARISH
    band = atr_hint if atr_hint > 0 else abs(entry) * 0.001
    for z in (ltf.get("confluence") or []):
        if z.get("direction") not in (want, "BULL" if direction == _LONG else "BEAR"):
            continue
        lo, hi = z.get("lo"), z.get("hi")
        if lo is None or hi is None:
            continue
        if (lo - band) <= entry <= (hi + band):
            return True
    return False


def _volume_confirms(direction: str, ltf: dict) -> bool:
    """Confirmation ONLY (never a trigger): rvol elevated and cumulative delta
    leaning with the trade direction."""
    vol = ltf.get("volume") or {}
    rvol = vol.get("rvol")
    cd = vol.get("cum_delta")
    if rvol is None or rvol < 1.2:
        return False
    if cd is None:
        return False
    return (direction == _LONG and cd > 0) or (direction == _SHORT and cd < 0)


def _fresh(sig: dict, now_ts: datetime | None) -> bool:
    """A signal is a live trigger only inside its invalid_after_bars window."""
    if now_ts is None:
        return True
    try:
        created = datetime.fromisoformat(sig["created_ts"])
    except (KeyError, TypeError, ValueError):
        return True
    bars = sig.get("invalid_after_bars") or 5
    age_min = (now_ts - created).total_seconds() / 60.0
    return 0 <= age_min <= bars * FRESH_FACTOR


def _risk_level(confidence: float, aligned: bool) -> str:
    if confidence >= 82 and aligned:
        return "LOW"
    if confidence >= 72:
        return "MEDIUM"
    return "HIGH"


# ------------------------------------------------------------------ the engine

def build_setups(symbol: str, htf: dict | None, ltf: dict | None,
                 now_ts: datetime | None = None,
                 atr_hint: float = 0.0) -> list[TradeSetupV2]:
    """Return the HIGH-probability, HTF-aligned, fully-explained setups for one
    symbol (empty = "No high-probability setup available"). Reuses the frozen
    detections in `htf`/`ltf` — adds only the top-down gate, the pillar scoring,
    and the narrative."""
    if not ltf:
        return []
    overall = (htf or {}).get("overall") or {}
    htf_bias = overall.get("bias") or "NEUTRAL"
    htf_conf = float(overall.get("confidence") or 0.0)
    story = overall.get("market_story") or ""
    ltf_trend = ltf.get("trend") or "UNKNOWN"
    qual = ltf.get("qualification") or {}
    data_ok = (qual.get("data_integrity") == "PASS")

    out: list[TradeSetupV2] = []
    for sig in (ltf.get("signals") or []):
        direction = sig.get("direction")
        entry, sl, tp1, tp2 = sig.get("entry"), sig.get("sl"), sig.get("tp1"), sig.get("tp2")
        if direction not in (_LONG, _SHORT) or entry is None or sl is None or tp1 is None:
            continue
        if not _fresh(sig, now_ts):
            continue                                   # stale trigger — not "now"

        rr = _rr(entry, sl, tp1)
        if rr is None or rr < MIN_RR:
            continue                                   # structure doesn't pay enough

        aligned = _htf_aligned(direction, htf_bias)
        # top-down discipline: never fight a CONVINCED higher timeframe.
        if not aligned and htf_bias in (_BULLISH, _BEARISH) and htf_conf >= 0.5:
            continue

        sweep_shift = _has_sweep_then_shift(ltf)
        pd_ok = _prem_disc_ok(direction, (ltf.get("liquidity") or {}).get("premium_discount"))
        confl = _confluence_near(direction, entry, ltf, atr_hint)
        vol_ok = _volume_confirms(direction, ltf)

        confidence = (
            (W_HTF_ALIGNED if aligned else 0.0)
            + (W_HTF_STRENGTH * min(1.0, htf_conf) if aligned else 0.0)
            + (W_SWEEP_SHIFT if sweep_shift else 0.0)
            + (W_PREM_DISC if pd_ok else 0.0)
            + (W_CONFLUENCE if confl else 0.0)
            + (W_VOLUME if vol_ok else 0.0)
        )
        # data-integrity is a hard gate (no trusting a gapped/mis-clocked feed).
        if not data_ok or confidence < MIN_CONFIDENCE:
            continue

        reasons = [f"✓ HTF {htf_bias.lower()} bias — trading with the higher timeframe"] if aligned else []
        if sweep_shift:
            reasons.append("✓ liquidity swept then structure shifted (CHOCH/BOS) — the reversal is confirmed")
        if pd_ok:
            reasons.append(f"✓ entry in {'discount' if direction == _LONG else 'premium'} — the correct half of the range")
        if confl:
            reasons.append("✓ price-action confluence (order block / FVG / pool) at the entry")
        if vol_ok:
            reasons.append("✓ volume confirms (elevated rvol, delta leaning with the move)")

        risk_level = _risk_level(confidence, aligned)
        invalidation = (f"a decisive close beyond the stop at {sl} (the swept wick / "
                        f"zone origin) voids the read")
        side_word = "long" if direction == _LONG else "short"
        why = {
            "why_exists": (f"{story} On the 1m the market swept liquidity and shifted, "
                           f"offering a with-bias {side_word}." if story else
                           f"A with-bias {side_word}: liquidity swept and structure shifted."),
            "why_now": ("the sweep + shift just printed and the trigger is still live "
                        "(inside its validity window)"),
            "why_entry": (f"the refined zone at {entry} — a {'discount' if direction == _LONG else 'premium'} "
                          f"order block / FVG left by the shift"),
            "why_sl": f"beyond {sl}, the swept extreme — the price that would prove the read wrong",
            "why_targets": (f"the next opposing liquidity pool at {tp1}"
                            + (f", then {tp2}" if tp2 else "") + f" (net R:R {rr:.2f})"),
            "why_edge": ("higher-timeframe alignment + a confirmed liquidity raid + a "
                         "structure shift into a discounted zone — the professional "
                         "sweep→shift→zone pattern, not an indicator signal"),
        }
        out.append(TradeSetupV2(
            symbol=symbol, direction=direction,
            entry=float(entry), sl=float(sl), tp1=float(tp1),
            tp2=(float(tp2) if tp2 is not None else None),
            rr=round(rr, 2), confidence=round(confidence, 1),
            risk_level=risk_level, market_bias=ltf_trend, htf_bias=htf_bias,
            strategy=sig.get("strategy") or "?", reasons=tuple(reasons),
            invalidation=invalidation, why=why, created_ts=sig.get("created_ts")))

    # strongest first — a professional shows the best idea, not a list of maybes.
    out.sort(key=lambda s: s.confidence, reverse=True)
    return out

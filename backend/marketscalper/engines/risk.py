"""Risk / Trade Management Engine — COMPLETE and FROZEN (engine-wise
freeze after the D17 conformance audit; Architecture §7 PLANNING +
SUGGESTED MANAGEMENT; Decision D17 incl. its freeze-audit record;
roadmap P3.17). Modify only on a genuine production defect.

Pure capability module, no composition wiring (D17.1 — the P1.1/R1
precedent): its inputs exist only once S1–S3 strategies produce plans.
`plan_trade` applies the §7 math verbatim — risk_amt = equity × 0.5%,
suggested qty (DISPLAY ONLY, §7), fee-adjusted net RR with the pinned
SHORT mirror, strict reject below net RR(TP1) 1.0 — after the D17.3
geometry validations (refuse-until-valid: rejected plans carry reasons
and no numbers). `management_guidance` renders the §7 display-only text
(v1.2: guidance is text, never automation). The platform never executes
trades; nothing here places, sizes, or manages a live order.

Pure functions of their arguments — no clock, no randomness, no state;
replay and live are trivially bit-identical (§0 rule 2).
"""

from __future__ import annotations

from dataclasses import dataclass

# Frozen §7/§6 literals — module constants, not config (D17.2).
RISK_PCT = 0.005                    # §7: equity × 0.5% per trade
NET_RR_TP1_FLOOR = 1.0              # §7: reject if net RR(TP1) < 1.0 (strict)
RR_TP2_FLOOR = 1.5                  # §6 G6: RR to TP2 ≥ 1.5 (inclusive)
DEFAULT_TAKER_FEE = 0.0005          # §7: Delta ~0.05% taker/side


@dataclass(frozen=True)
class TradePlan:
    """One §7 planning result. status 'suggested' | 'rejected'.

    Geometry-rejected plans carry only the inputs and reasons (D17.3 —
    refuse-until-valid); a net-RR rejection keeps its computed numbers
    (they ARE the evidence). qty is SUGGESTED display text, never an
    order."""

    direction: str                  # 'LONG' | 'SHORT'
    entry: float
    sl: float
    tp1: float
    tp2: float | None
    status: str
    reasons: tuple                  # rejection reasons, () when suggested
    risk_amt: float | None
    r_per_unit: float | None
    qty: float | None               # display only (§7)
    fee_per_unit: float | None
    net_rr_tp1: float | None
    net_rr_tp2: float | None        # None when tp2 absent
    rr_floor_ok: bool | None        # G6-alignment flag (D17.1 deferral)


def _net_rr(direction: str, entry: float, sl: float, tp: float,
            fee_per_unit: float) -> float:
    """§7 net RR — LONG verbatim, SHORT the pinned mirror (D17.2)."""
    if direction == "LONG":
        return (tp - entry - fee_per_unit) / (entry - sl + fee_per_unit)
    return (entry - tp - fee_per_unit) / (sl - entry + fee_per_unit)


def plan_trade(*, direction: str, entry: float, sl: float, tp1: float,
               tp2: float | None = None, equity: float,
               taker_fee: float = DEFAULT_TAKER_FEE) -> TradePlan:
    """§7 planning math over one strategy-supplied setup (D17.2/D17.3)."""
    reasons: list[str] = []
    long = direction == "LONG"
    if direction not in ("LONG", "SHORT"):
        reasons.append("unknown direction")
    if not equity > 0:
        reasons.append("equity must be positive")
    if not taker_fee >= 0:
        reasons.append("taker fee must be non-negative")
    prices = [entry, sl, tp1] + ([tp2] if tp2 is not None else [])
    if not all(p > 0 for p in prices):
        reasons.append("prices must be positive")
    if direction in ("LONG", "SHORT") and all(p > 0 for p in prices):
        if not (sl < entry if long else sl > entry):
            reasons.append("stop loss not on the loss side")
        if not (tp1 > entry if long else tp1 < entry):
            reasons.append("tp1 not on the profit side")
        if tp2 is not None and not (tp2 > tp1 if long else tp2 < tp1):
            reasons.append("tp2 not beyond tp1")
    if reasons:
        return TradePlan(direction, entry, sl, tp1, tp2, "rejected",
                         tuple(reasons), None, None, None, None, None,
                         None, None)

    risk_amt = equity * RISK_PCT
    r_per_unit = abs(entry - sl)
    qty = risk_amt / r_per_unit
    fee_per_unit = entry * taker_fee * 2
    net_rr_tp1 = _net_rr(direction, entry, sl, tp1, fee_per_unit)
    net_rr_tp2 = (None if tp2 is None
                  else _net_rr(direction, entry, sl, tp2, fee_per_unit))
    rr_floor_ok = (net_rr_tp1 >= NET_RR_TP1_FLOOR
                   and (net_rr_tp2 is None or net_rr_tp2 >= RR_TP2_FLOOR))
    status = "suggested"
    if net_rr_tp1 < NET_RR_TP1_FLOOR:                  # §7: strict reject
        status = "rejected"
        reasons.append("net RR to TP1 below 1.0 after fees")
    return TradePlan(direction, entry, sl, tp1, tp2, status,
                     tuple(reasons), risk_amt, r_per_unit, qty,
                     fee_per_unit, net_rr_tp1, net_rr_tp2, rr_floor_ok)


def management_guidance(plan: TradePlan) -> tuple:
    """§7 SUGGESTED MANAGEMENT — display-only text lines (D17.4).

    The platform never acts on these (v1.2); empty for rejected plans."""
    if plan.status != "suggested":
        return ()
    one_r = (plan.entry + plan.r_per_unit if plan.direction == "LONG"
             else plan.entry - plan.r_per_unit)
    return (
        f"At +1R ({one_r:.10g}): move SL to break-even ({plan.entry:.10g})",
        f"At TP1 ({plan.tp1:.10g}): take 50% off, trail the rest at the "
        "last confirmed 1m swing",
        "Time stop: exit if the thesis has not played out in 15 minutes",
        "Exit on an opposite displacement candle (> 1.5×ATR)",
    )

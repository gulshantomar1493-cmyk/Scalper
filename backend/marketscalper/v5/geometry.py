"""Where the stop goes, where the target goes, and when to walk away.

Two rules carry the whole difference from V4:

  * The stop is STRUCTURAL — just beyond the thing that would prove the idea
    wrong — not a volatility multiple. V4 used 5 x ATR(5m), which is a long way
    from anything the chart cares about, and then needed 10R to justify it.

  * The target is a PRICE SOMEONE IS WATCHING — a prior swing, a higher-timeframe
    level — and the setup is REJECTED when the nearest such price is closer than
    2R. It is never moved further out to make the ratio look acceptable. That is
    the difference between a plan and a wish.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The owner's real Delta schedule. Entry is always taker (the trigger is a
#: market-or-better fill); the target rests as a limit, so it is maker.
TAKER = 0.0005
MAKER = 0.0002
GST = 0.18

MIN_RR = 2.0
#: Beyond this the target is in open space. The V4 tail argument (10R) has
#: already been shown not to pay in this era, so V5 does not reach for it.
MAX_RR = 4.0

#: Padding beyond the structural price, in ATR of the trigger timeframe. Exists
#: so a single wick through the exact low does not stop the trade out; it is not
#: a volatility stop.
STOP_PAD_ATR = 0.25


@dataclass(frozen=True)
class Plan:
    entry: float
    stop: float
    target: float
    rr: float               # net of fees — what the trader actually collects
    gross_rr: float
    risk: float             # entry - stop, absolute
    target_label: str       # the structural thing being aimed at
    fee_r: float            # round-trip cost as a fraction of 1R


def round_trip_fee(entry: float, exit_px: float, risk: float) -> float:
    """Cost of the round trip as a fraction of one R, GST included.

    This is the number the whole strategy set lives or dies on: the research
    threshold was fee/R < 0.12, and it was computed WITHOUT the 18% GST the
    owner actually pays.
    """
    if risk <= 0:
        return float("inf")
    gst = 1.0 + GST
    return ((entry * TAKER) + (exit_px * MAKER)) * gst / risk


def structural_stop(extreme: float, direction: int, atr: float) -> float:
    """Just beyond the price that would prove the setup wrong."""
    pad = STOP_PAD_ATR * (atr if atr and atr == atr else 0.0)
    return extreme - pad if direction > 0 else extreme + pad


def build(entry: float, stop: float, direction: int,
          candidates: list) -> Plan | None:
    """Pick the first structural target that clears MIN_RR net of fees.

    `candidates` is [(price, label), ...] in the order the engine wants them
    considered — nearest structure first. Returns None when none of them clears
    the floor, which is a rejection, not a fallback.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        return None                      # stop on the wrong side of entry

    best: Plan | None = None
    for price, label in candidates:
        if price is None or price != price:          # None or NaN
            continue
        reward = (price - entry) if direction > 0 else (entry - price)
        if reward <= 0:
            continue                     # target behind us
        gross = reward / risk
        fee_r = round_trip_fee(entry, price, risk)
        net = gross - fee_r
        if net < MIN_RR:
            continue
        if net > MAX_RR:
            # Structure is further than we are willing to reach. Take the
            # capped distance at MAX_RR rather than the structural price — but
            # only when the structure genuinely supports going that far.
            capped = entry + direction * (MAX_RR + fee_r) * risk
            return Plan(entry, stop, float(capped), MAX_RR,
                        float((abs(capped - entry)) / risk), risk,
                        f"{label} (capped at {MAX_RR:g}R)", fee_r)
        best = Plan(entry, stop, float(price), round(net, 2), round(gross, 2),
                    risk, label, round(fee_r, 4))
        break                            # nearest qualifying target wins
    return best

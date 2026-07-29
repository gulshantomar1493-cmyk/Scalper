"""Market structure: swings, their labels, the trend they describe, and the
breaks that change it.

Pure numpy in, dataclasses out. No I/O, no clock, no randomness — the backtest
and the live path run this identical code, which is the only reason a backtest
number means anything.

NO REPAINT. A swing at bar j is only confirmed at bar j+k, and every value at
bar i is computed from bars <= i. `state_at(i)` is what the engine knew at the
close of bar i, not what we know now.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: fractal half-width. A swing high is the strict maximum of the 2 bars either
#: side, so it is confirmed 2 bars late.
K = 2

BULLISH, BEARISH, RANGE = "BULLISH", "BEARISH", "RANGE"
HH, HL, LH, LL = "HH", "HL", "LH", "LL"


@dataclass(frozen=True)
class Swing:
    i: int              # bar index of the pivot itself
    confirmed_i: int    # bar index at which it became known (i + K)
    ts: int
    price: float
    kind: str           # "high" | "low"
    label: str | None   # HH / HL / LH / LL, None for the first of its kind


@dataclass(frozen=True)
class Break:
    i: int              # the bar whose CLOSE broke the level
    ts: int
    kind: str           # "BOS" | "CHOCH"
    direction: int      # +1 up, -1 down
    level: float        # the swing price that was broken


@dataclass(frozen=True)
class Structure:
    """Everything the engine knows about one timeframe at one bar."""
    state: str                      # BULLISH / BEARISH / RANGE
    swings: list                    # confirmed Swings, oldest first
    last_high: Swing | None
    last_low: Swing | None
    breaks: list                    # confirmed Breaks, oldest first
    last_bos: Break | None
    last_choch: Break | None


def find_swings(bars: dict, k: int = K) -> list:
    """Strict fractal pivots, confirmed k bars late.

    Strict on both sides: a bar that ties with a neighbour is not a pivot. Ties
    are common on low-volatility bars and an inclusive test would produce a
    cluster of pivots at the same price, which then labels as a fake HH/HL pair.
    """
    h, l = bars["h"], bars["l"]
    ts = bars["ts"]
    n = len(h)
    out: list = []
    for j in range(k, n - k):
        wh = h[j - k:j + k + 1]
        wl = l[j - k:j + k + 1]
        if h[j] == wh.max() and (wh == h[j]).sum() == 1:
            out.append(Swing(j, j + k, int(ts[j]), float(h[j]), "high", None))
        elif l[j] == wl.min() and (wl == l[j]).sum() == 1:
            # elif, not if: an outside bar that is both is treated as a high,
            # matching the order the market printed it in. Allowing both would
            # emit two swings at one bar and corrupt the alternation.
            out.append(Swing(j, j + k, int(ts[j]), float(l[j]), "low", None))
    return out


def label_swings(swings: list) -> list:
    """HH/HL/LH/LL, each against the previous swing OF THE SAME KIND.

    The first swing of each kind has no predecessor to compare against and is
    labelled None — it still seeds the chain. Equality labels as the weaker of
    the pair (LH / LL): a level that was matched, not exceeded, is not strength.
    """
    out = []
    prev_h = prev_l = None
    for s in swings:
        label = None
        if s.kind == "high":
            if prev_h is not None:
                label = HH if s.price > prev_h else LH
            prev_h = s.price
        else:
            if prev_l is not None:
                label = HL if s.price > prev_l else LL
            prev_l = s.price
        out.append(Swing(s.i, s.confirmed_i, s.ts, s.price, s.kind, label))
    return out


def _state_from(last_high: Swing | None, last_low: Swing | None) -> str:
    """BULLISH needs BOTH a higher high and a higher low. One alone is noise:
    a higher high with a lower low is an expanding range, not an uptrend."""
    if last_high is None or last_low is None:
        return RANGE
    if last_high.label == HH and last_low.label == HL:
        return BULLISH
    if last_high.label == LH and last_low.label == LL:
        return BEARISH
    return RANGE


def analyse(bars: dict, upto: int | None = None, k: int = K) -> Structure:
    """The structure as known at the close of bar `upto` (default: last bar).

    Only swings whose confirmation bar has already passed are visible, so this
    is safe to call inside a backtest loop.
    """
    n = len(bars["c"])
    i = (n - 1) if upto is None else int(upto)
    if i < 0:
        return Structure(RANGE, [], None, None, [], None, None)

    visible = [s for s in label_swings(find_swings(bars, k)) if s.confirmed_i <= i]
    highs = [s for s in visible if s.kind == "high"]
    lows = [s for s in visible if s.kind == "low"]
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    state = _state_from(last_high, last_low)

    breaks = _find_breaks(bars, visible, i)
    bos = next((b for b in reversed(breaks) if b.kind == "BOS"), None)
    choch = next((b for b in reversed(breaks) if b.kind == "CHOCH"), None)
    return Structure(state, visible, last_high, last_low, breaks, bos, choch)


def _find_breaks(bars: dict, swings: list, upto: int) -> list:
    """Walk forward, re-deriving the structure as it was at each bar, and record
    every close that broke the then-current swing.

    A break is classified by the structure IN FORCE WHEN IT HAPPENED — with the
    trend it is a BOS (continuation), against it a CHOCH (the first warning of a
    change). Classifying with today's structure would relabel history every time
    the trend flipped, which is exactly the repaint this module exists to avoid.

    Each swing can only be broken once. Without that latch a single strong leg
    away from a level emits a break on every bar it stays beyond it.
    """
    c, ts = bars["c"], bars["ts"]
    out: list = []
    hi = lo = None            # the last confirmed swing of each kind
    hi_used = lo_used = True  # nothing to break yet
    state = RANGE
    last_h_label = last_l_label = None

    # confirmation bar -> swings that become visible at it
    by_conf: dict = {}
    for s in swings:
        by_conf.setdefault(s.confirmed_i, []).append(s)

    for i in range(upto + 1):
        for s in by_conf.get(i, ()):
            if s.kind == "high":
                hi, hi_used, last_h_label = s, False, s.label
            else:
                lo, lo_used, last_l_label = s, False, s.label
            if last_h_label == HH and last_l_label == HL:
                state = BULLISH
            elif last_h_label == LH and last_l_label == LL:
                state = BEARISH
            else:
                state = RANGE

        px = c[i]
        if hi is not None and not hi_used and px > hi.price:
            hi_used = True
            kind = "CHOCH" if state == BEARISH else "BOS"
            out.append(Break(i, int(ts[i]), kind, 1, hi.price))
        elif lo is not None and not lo_used and px < lo.price:
            lo_used = True
            kind = "CHOCH" if state == BULLISH else "BOS"
            out.append(Break(i, int(ts[i]), kind, -1, lo.price))
    return out


def last_impulse(st: Structure, direction: int) -> tuple[Swing, Swing] | None:
    """The most recent completed leg in `direction`: (origin, extreme).

    For a long: the latest swing high, and the last swing low BEFORE it. Not
    "the last two swings" — by the time a pullback is deep enough to trade, it
    has usually printed a new swing low of its own, and requiring the high to be
    the most recent swing would return None at exactly the moment the engine
    needs the leg. The leg being retraced is the one that ended at that high,
    whatever has printed since.
    """
    highs = [s for s in st.swings if s.kind == "high"]
    lows = [s for s in st.swings if s.kind == "low"]
    if direction > 0:
        if not highs:
            return None
        extreme = highs[-1]
        prior = [s for s in lows if s.i < extreme.i]
        return (prior[-1], extreme) if prior else None
    if not lows:
        return None
    extreme = lows[-1]
    prior = [s for s in highs if s.i < extreme.i]
    return (prior[-1], extreme) if prior else None


def retracement(origin: float, extreme: float, price: float) -> float:
    """How far price has come back into a leg. 0.0 = at the extreme,
    1.0 = all the way back to where the leg started."""
    span = extreme - origin
    if span == 0:
        return 0.0
    return float((extreme - price) / span)

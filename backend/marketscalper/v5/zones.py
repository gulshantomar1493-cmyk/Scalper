"""Demand and supply zones, and the higher-timeframe levels price reacts to.

A zone is where an impulse began: the last opposite-colour candle before the
move that broke structure. It is the only part of the chart where the engine is
willing to enter, because it is the only place a structural stop is close enough
for a 1:3 to exist. V4 entered at the level itself with a resting stop order,
which is the far side of the same move — the stop then has to sit behind the
whole leg, and 10R is the only way to justify it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Zone:
    lo: float
    hi: float
    kind: str        # "demand" | "supply"
    i: int           # the candle that formed it
    ts: int
    tested: bool     # price has been back inside since it formed

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


def zone_before(bars: dict, impulse_start: int, direction: int,
                *, lookback: int = 12) -> Zone | None:
    """The last opposite-colour candle strictly before the impulse.

    For a long: the last DOWN candle before price left. Its low-to-open is the
    demand — the last price at which sellers were in control before they were
    overwhelmed. Dojis are skipped: a candle with no body expressed no control.
    """
    o, h, l, c, ts = bars["o"], bars["h"], bars["l"], bars["c"], bars["ts"]
    start = max(0, impulse_start - lookback)
    for i in range(impulse_start, start - 1, -1):
        body = c[i] - o[i]
        if direction > 0 and body < 0:
            return Zone(float(l[i]), float(o[i]), "demand", i, int(ts[i]), False)
        if direction < 0 and body > 0:
            return Zone(float(o[i]), float(h[i]), "supply", i, int(ts[i]), False)
    return None


def mark_tested(zone: Zone, bars: dict, upto: int) -> Zone:
    """A zone price has already traded back into is weaker: the orders that
    were resting there have been filled. Recorded, not discarded — the engine
    prefers untested zones but a tested one is still structure."""
    if zone is None:
        return zone
    h, l = bars["h"], bars["l"]
    for i in range(zone.i + 1, min(upto + 1, len(h))):
        if l[i] <= zone.hi and h[i] >= zone.lo:
            return Zone(zone.lo, zone.hi, zone.kind, zone.i, zone.ts, True)
    return zone


# ------------------------------------------------------------------ levels --

@dataclass(frozen=True)
class Level:
    price: float
    label: str
    kind: str        # "pdh" | "pdl" | "pwh" | "pwl" | "round"


def _period_extremes(bars: dict, upto: int, seconds: int) -> tuple[float, float] | None:
    """High and low of the last COMPLETE period ending before bar `upto`.

    Complete is the point: a partial day's high is not the prior-day high, and
    using it would let a level move under the engine's feet.
    """
    ts, h, l = bars["ts"], bars["h"], bars["l"]
    if upto < 1:
        return None
    now_period = int(ts[upto]) // seconds
    prev = now_period - 1
    lo_i = hi_i = None
    for i in range(upto, -1, -1):
        p = int(ts[i]) // seconds
        if p == prev:
            if hi_i is None:
                hi_i = i
            lo_i = i
        elif p < prev:
            break
    if lo_i is None:
        return None
    seg_h = float(np.max(h[lo_i:hi_i + 1]))
    seg_l = float(np.min(l[lo_i:hi_i + 1]))
    return seg_h, seg_l


def htf_levels(bars_1d: dict, upto: int, price: float) -> list:
    """Prior-day and prior-week high/low, plus the round numbers around price.

    `bars_1d` must be daily candles; weeks are derived from them so the two
    cannot disagree about where a day ends.
    """
    out: list = []
    day = _period_extremes(bars_1d, upto, 86400)
    if day:
        out.append(Level(day[0], "Prior Day High", "pdh"))
        out.append(Level(day[1], "Prior Day Low", "pdl"))
    week = _period_extremes(bars_1d, upto, 7 * 86400)
    if week:
        out.append(Level(week[0], "Prior Week High", "pwh"))
        out.append(Level(week[1], "Prior Week Low", "pwl"))
    for r in _round_levels(price):
        out.append(Level(r, f"Round {r:,.0f}", "round"))
    return out


def _round_levels(price: float) -> list:
    """The psychological numbers either side of price. The step scales with the
    price so it is one rule for a $1,900 ETH and a $63,000 BTC."""
    if price <= 0:
        return []
    step = 10.0 ** (np.floor(np.log10(price)) - 1)
    if step <= 0:
        return []
    base = np.floor(price / step) * step
    return [float(base), float(base + step)]

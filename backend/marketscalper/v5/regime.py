"""Is this market trending or ranging?

This is the first question V5 asks, and the one V4 never asked. V4 ran a
breakout playbook unconditionally; the eleven live losses were a breakout system
bleeding inside a range, taking the false break at each boundary and giving back
the 0.6-1.5R it was briefly shown.

The measure is Kaufman's efficiency ratio: how much of the distance travelled
was actually progress. A market that closes 100 points away having moved 120 in
total is going somewhere; one that closes 10 away having moved 400 is not.
Deterministic, causal, no parameters beyond the window.
"""
from __future__ import annotations

import numpy as np

TRENDING, MIXED, RANGING = "TRENDING", "MIXED", "RANGING"

WINDOW = 20
#: Thresholds. Deliberately leaves a MIXED band rather than forcing a binary
#: call: near the boundary the measure is not meaningful, and pretending it is
#: would flip the whole playbook on noise.
TREND_AT = 0.35
RANGE_AT = 0.20


def efficiency(closes: np.ndarray, window: int = WINDOW) -> float:
    """Net progress / total travel over `window` bars. 1.0 = a straight line,
    0.0 = ended where it started. Returns NaN until there is enough data."""
    if len(closes) < window + 1:
        return float("nan")
    seg = closes[-(window + 1):]
    travel = float(np.abs(np.diff(seg)).sum())
    if travel <= 0:
        return 0.0
    return float(abs(seg[-1] - seg[0]) / travel)


def classify(closes: np.ndarray, window: int = WINDOW) -> tuple[str, float]:
    """(regime, efficiency). Unwarm data is RANGING, not TRENDING — the
    conservative side: it disables the continuation playbooks rather than
    authorising a trade on evidence we do not have."""
    e = efficiency(closes, window)
    if np.isnan(e):
        return RANGING, float("nan")
    if e >= TREND_AT:
        return TRENDING, e
    if e <= RANGE_AT:
        return RANGING, e
    return MIXED, e


def atr(bars: dict, period: int = 14) -> np.ndarray:
    """Wilder ATR. Used for stop padding and level tolerances — never for the
    stop distance itself, which V5 takes from structure."""
    h, l, c = bars["h"], bars["l"], bars["c"]
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    out[period] = tr[:period].mean()
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out

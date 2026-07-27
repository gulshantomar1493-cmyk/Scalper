"""Level detection — the price points a resting order is parked at.

Every function is CAUSAL: a level returned for bar i is computed only from bars
with index <= i. The research proved that trading the BREAK of these levels works
and that fading them (bounce) loses on every level type, so V4 only ever places
break (stop) orders.
"""
from __future__ import annotations
import numpy as np


def _roll_max(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        from numpy.lib.stride_tricks import sliding_window_view
        out[w - 1:] = sliding_window_view(x, w).max(axis=1)
    return out


def _roll_min(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        from numpy.lib.stride_tricks import sliding_window_view
        out[w - 1:] = sliding_window_view(x, w).min(axis=1)
    return out


def atr(bars: dict, period: int = 14) -> np.ndarray:
    """Wilder ATR. atr[i] uses only bars 0..i."""
    h, l, c = bars["h"], bars["l"], bars["c"]
    pc = np.empty_like(c); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full(len(tr), np.nan)
    if len(tr) <= period:
        return out
    prev = tr[1:period + 1].mean()
    out[period] = prev
    for i in range(period + 1, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def donchian(bars: dict, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """Highest high / lowest low of the last `lookback` CLOSED bars."""
    return _roll_max(bars["h"], lookback), _roll_min(bars["l"], lookback)


def swings(bars: dict, k: int = 2):
    """Fractal swing highs/lows, confirmed k bars later.
    Returns (last_high, last_low) arrays: the most recent CONFIRMED swing known at i."""
    h, l = bars["h"], bars["l"]
    n = len(h)
    last_h = np.full(n, np.nan)
    last_l = np.full(n, np.nan)
    hi = lo = np.nan
    pend = []
    for i in range(n):
        # a swing at index i-k becomes KNOWN at i
        j = i - k
        if j >= k:
            wh, wl = h[j - k:j + k + 1], l[j - k:j + k + 1]
            if h[j] == wh.max() and (wh == h[j]).sum() == 1:
                hi = h[j]
            elif l[j] == wl.min() and (wl == l[j]).sum() == 1:
                lo = l[j]
        last_h[i], last_l[i] = hi, lo
    return last_h, last_l


def prior_day(bars: dict) -> tuple[np.ndarray, np.ndarray]:
    """Prior UTC day's high/low, known from the first bar of the new day."""
    ts, h, l = bars["ts"], bars["h"], bars["l"]
    n = len(ts)
    pdh = np.full(n, np.nan); pdl = np.full(n, np.nan)
    day = ts // 86400
    cur = -1
    d_h = d_l = prev_h = prev_l = np.nan
    for i in range(n):
        d = int(day[i])
        if d != cur:
            prev_h, prev_l = d_h, d_l
            d_h = d_l = np.nan
            cur = d
        pdh[i], pdl[i] = prev_h, prev_l
        d_h = h[i] if np.isnan(d_h) else max(d_h, h[i])
        d_l = l[i] if np.isnan(d_l) else min(d_l, l[i])
    return pdh, pdl


def round_levels(price: float, symbol: str) -> list[float]:
    """Psychological round numbers bracketing price."""
    step = 1000.0 if symbol.upper().startswith("BTC") else 100.0
    base = np.floor(price / step) * step
    return [float(base + step), float(base)]


def levels_for(bars: dict, i: int, source: str, lookback: int, symbol: str,
               cache: dict | None = None) -> list[float]:
    """The level candidates for bar i from the requested source."""
    if cache is None:
        cache = {}
    key = (source, lookback, id(bars))
    if source == "donchian":
        if key not in cache:
            cache[key] = donchian(bars, lookback)
        hi, lo = cache[key]
        return [hi[i], lo[i]]
    if source == "swing":
        if key not in cache:
            cache[key] = swings(bars)
        hi, lo = cache[key]
        return [hi[i], lo[i]]
    if source == "pdh_pdl":
        if key not in cache:
            cache[key] = prior_day(bars)
        hi, lo = cache[key]
        return [hi[i], lo[i]]
    if source == "round":
        return round_levels(float(bars["c"][i]), symbol)
    return []

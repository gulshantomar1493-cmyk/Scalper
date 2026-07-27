"""Trend filters — the ONLY confluence family the research found to add value.

Measured lift (net R with the factor ON minus OFF), both symbols:
    1D trend alignment      +0.189 (BTC) / +0.455 (ETH)
    structure/BOS alignment +0.273 / +0.412
    4H trend alignment      +0.015 / +0.683

Filters that were tested and HURT are deliberately absent (premium/discount
-0.229/-0.454, liquidity sweep -0.227/-0.258). See config.REJECTED_IDEAS.
"""
from __future__ import annotations
import numpy as np


def ema(x: np.ndarray, period: int) -> np.ndarray:
    a = 2.0 / (period + 1.0)
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    out[period - 1] = x[:period].mean()
    for i in range(period, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def structure_trend(bars: dict, k: int = 2) -> np.ndarray:
    """+1 after price closes above the last confirmed swing high, -1 after it
    closes below the last confirmed swing low, 0 before either. Causal."""
    h, l, c = bars["h"], bars["l"], bars["c"]
    n = len(c)
    out = np.zeros(n, np.int8)
    hi = lo = np.nan
    cur = 0
    for i in range(n):
        j = i - k
        if j >= k:
            wh, wl = h[j - k:j + k + 1], l[j - k:j + k + 1]
            if h[j] == wh.max() and (wh == h[j]).sum() == 1:
                hi = h[j]
            elif l[j] == wl.min() and (wl == l[j]).sum() == 1:
                lo = l[j]
        if not np.isnan(hi) and c[i] > hi:
            cur = 1
        elif not np.isnan(lo) and c[i] < lo:
            cur = -1
        out[i] = cur
    return out


def align_causal(src: dict, target_close_ts: np.ndarray) -> np.ndarray:
    """Index of the last `src` bar that had already CLOSED at each target time.
    This is the anti-lookahead join for multi-timeframe context."""
    return np.searchsorted(src["ts"] + src["tf_s"], target_close_ts, side="right") - 1


class TrendContext:
    """Pre-computes the three filters once, then answers per-bar cheaply."""

    def __init__(self, anchor: dict, daily: dict, ema_anchor: int = 50, ema_daily: int = 20):
        self.anchor = anchor
        self.daily = daily
        self._ea = ema(anchor["c"], ema_anchor)
        self._ed = ema(daily["c"], ema_daily)
        self._st = structure_trend(anchor)
        self._jd = align_causal(daily, anchor["ts"] + anchor["tf_s"])

    def score(self, i: int, direction: int) -> tuple[int, dict]:
        """How many of the three filters agree with `direction` at anchor bar i."""
        det = {}
        c = self.anchor["c"][i]
        det["trend_anchor"] = bool((not np.isnan(self._ea[i])) and np.sign(c - self._ea[i]) == direction)
        kd = int(self._jd[i])
        det["trend_daily"] = bool(kd >= 0 and (not np.isnan(self._ed[kd]))
                                  and np.sign(self.daily["c"][kd] - self._ed[kd]) == direction)
        det["structure"] = bool(self._st[i] == direction)
        return sum(det.values()), det

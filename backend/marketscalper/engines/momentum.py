"""Shared momentum utilities (roadmap P1.1; Architecture §4.7).

NOT an engine: plain incremental metrics that the Structure, Trendline,
Volume and Qualification engines consume. This module starts with the
ATR(14) utility (task P1.1); velocity/acceleration/body-dominance follow
at P1.2 and the regime classifier at P1.4.

No repaint: updates happen only on closed candles (the bus carries truth
candles only). No wall clock, no randomness — a pure fold over the candle
stream, so replay and live produce identical sequences (§0 rule 2).
"""

from __future__ import annotations

from marketscalper.providers.base import Candle


class IncrementalATR:
    """Wilder ATR over closed candles of one (symbol, timeframe) stream.

    Convention (P1.1 task plan): the first candle yields no TR (there is no
    previous close); TRs exist from candle 2 onward; the first ATR value is
    the SMA of the first `period` TRs — available at candle period+1 —
    and thereafter Wilder's RMA:

        tr  = max(h - l, |h - prev_close|, |l - prev_close|)
        atr = (atr_prev * (period - 1) + tr) / period

    update() and value return None until warm. O(1) time and memory; the
    caller owns one instance per (symbol, timeframe).
    """

    __slots__ = ("_period", "_prev_close", "_tr_sum", "_tr_count", "_atr")

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        self._period = period
        self._prev_close: float | None = None
        self._tr_sum = 0.0
        self._tr_count = 0
        self._atr: float | None = None

    @property
    def value(self) -> float | None:
        """Last computed ATR, or None while warming up."""
        return self._atr

    def update(self, candle: Candle) -> float | None:
        """Fold one closed candle in; return the post-update ATR (or None)."""
        prev_close = self._prev_close
        self._prev_close = candle.c
        if prev_close is None:
            return None                       # first candle: TR undefined
        tr = max(
            candle.h - candle.l,
            abs(candle.h - prev_close),
            abs(candle.l - prev_close),
        )
        if self._atr is None:
            self._tr_sum += tr
            self._tr_count += 1
            if self._tr_count < self._period:
                return None
            self._atr = self._tr_sum / self._period      # Wilder seed (SMA)
        else:
            self._atr = (self._atr * (self._period - 1) + tr) / self._period
        return self._atr

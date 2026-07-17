"""Shared momentum utilities (roadmap P1.1; Architecture §4.7).

NOT an engine: plain incremental metrics that the Structure, Trendline,
Volume and Qualification engines consume. ATR(14) landed at P1.1;
velocity / acceleration / momentum-shift / body-dominance at P1.2
(thresholds per Decision D9); the regime classifier follows at P1.4.

No repaint: updates happen only on closed candles (the bus carries truth
candles only). No wall clock, no randomness — a pure fold over the candle
stream, so replay and live produce identical sequences (§0 rule 2).
"""

from __future__ import annotations

from collections import deque

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


class MomentumState:
    """Velocity / acceleration / momentum-shift / body-dominance (§4.7, P1.2)
    for one (symbol, timeframe) stream.

    Conventions (P1.2 task plan + Decision D9):
      velocity      EMA-5 of close-to-close change; SMA-seeded like ATR
                    (first value = mean of the first 5 deltas, at candle 6),
                    then ema = a*delta + (1-a)*ema_prev with a = 1/3.
      acceleration  velocity_t - velocity_{t-1} (first value at candle 7).
      momentum_shift  strict velocity sign crossing AND
                    |acceleration| > shift_accel_atr_ratio * atr.value.
                    Per-bar flag (True only on the flip bar); False while
                    acceleration or the ATR is unwarm; zero velocity never
                    flips. The caller updates the ATR BEFORE this instance
                    each candle (pinned update-order contract).
      body_dominance  mean of |c-o|/(h-l) over the last 5 candles; a
                    zero-range candle contributes 0.0; None until 5 seen.
    """

    __slots__ = ("_atr_ref", "_ratio", "_prev_close", "_delta_sum",
                 "_delta_count", "_velocity", "_acceleration", "_shift",
                 "_bodies")

    _EMA_PERIOD = 5
    _ALPHA = 2.0 / (_EMA_PERIOD + 1)
    _BODY_WINDOW = 5

    def __init__(self, atr: IncrementalATR,
                 shift_accel_atr_ratio: float = 0.1) -> None:
        self._atr_ref = atr
        self._ratio = shift_accel_atr_ratio
        self._prev_close: float | None = None
        self._delta_sum = 0.0
        self._delta_count = 0
        self._velocity: float | None = None
        self._acceleration: float | None = None
        self._shift = False
        self._bodies: deque[float] = deque(maxlen=self._BODY_WINDOW)

    @property
    def velocity(self) -> float | None:
        return self._velocity

    @property
    def acceleration(self) -> float | None:
        return self._acceleration

    @property
    def momentum_shift(self) -> bool:
        return self._shift

    @property
    def body_dominance(self) -> float | None:
        if len(self._bodies) < self._BODY_WINDOW:
            return None
        return sum(self._bodies) / self._BODY_WINDOW

    def update(self, candle: Candle) -> None:
        """Fold one closed candle in (ATR already updated for this candle)."""
        rng = candle.h - candle.l
        self._bodies.append(abs(candle.c - candle.o) / rng if rng > 0.0 else 0.0)

        prev_close = self._prev_close
        self._prev_close = candle.c
        self._shift = False
        if prev_close is None:
            return                               # first candle: no delta
        delta = candle.c - prev_close

        prev_velocity = self._velocity
        if prev_velocity is None:
            self._delta_sum += delta
            self._delta_count += 1
            if self._delta_count < self._EMA_PERIOD:
                return
            self._velocity = self._delta_sum / self._EMA_PERIOD   # SMA seed
            return                               # first velocity: no accel yet
        self._velocity = self._ALPHA * delta + (1.0 - self._ALPHA) * prev_velocity
        self._acceleration = self._velocity - prev_velocity

        atr = self._atr_ref.value
        if atr is None:
            return                               # threshold basis unwarm
        flipped = ((prev_velocity > 0 and self._velocity < 0)
                   or (prev_velocity < 0 and self._velocity > 0))
        self._shift = flipped and abs(self._acceleration) > self._ratio * atr

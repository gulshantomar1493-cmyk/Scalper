"""Volume Engine — COMPLETE and FROZEN (engine-wise freeze after the D19
conformance audit; Architecture §4.6; Decision D19 incl. its freeze-audit
record; roadmap P2.1–P2.7). Modify only on a genuine production defect.

RVOL time-of-day normalized (1440 UTC minute-of-day buckets, ring of the
last 20 observed volumes per bucket, full-window median only — D7),
session VWAP anchored at 00:00 UTC with volume-weighted ±1σ/±2σ bands,
anchored VWAP at the last confirmed 5m pivot (A8), delta/cum-delta,
absorption, spike and exhaustion — all per §4.6 with every boundary and
fallback pinned in D19.

Two-phase cadence, LOCKED by the owner (D19.8): `update(candle)` runs
EXACTLY ONCE per closed 1m candle immediately after the momentum
utilities (the bar's rvol is then available to every later engine —
the P2.2 consumers depend on this), and `classify(candle, key_levels,
pools, extremes)` runs EXACTLY ONCE per closed 1m candle immediately
after the Liquidity Engine's update. Never re-entered; partial candles
never reach this engine (the composition guard is the sole gatekeeper).
The anchor intake `on_anchor(pivot)` receives confirmed 5m pivots in
step_5m, alongside the liquidity external-pivot feed (D19.4).

Seeding (D19.2, owner-approved): `seed(candles)` folds historical 1m
volumes into the RVOL buckets ONLY; composition performs the 20-day
storage read (live startup and replay sessions alike) — this engine is
database-unaware. Unseeded operation is fully supported: rvol stays
None until buckets fill (D7 — absent, never fabricated).

Pure fold: no clock, no randomness, no I/O; identical inputs (seed +
stream + ATR reads + phase-2 args) produce identical outputs in live
and replay (§0 rule 2).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import sqrt

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

log = logging.getLogger(__name__)

# Frozen §4.6/D19 literals — module constants, not config.
RVOL_WINDOW_DAYS = 20                  # D19.1: full window required (D7)
SPIKE_RVOL_MIN = 2.0                   # §4.6: rvol >= 2.0 (inclusive)
ABSORPTION_RANGE_ATR_RATIO = 0.5       # §4.6: range < 0.5×ATR (strict)
ABSORPTION_LEVEL_BAND_ATR_RATIO = 0.3  # the frozen D15 band constant, reused
EXHAUSTION_WICK_RATIO = 0.6            # the frozen D12 wick constant, reused
ANCHOR_BUFFER_1M = 1440                # 24h of 1m candles (D19.4)
# Calibration constant per the owner's D19 ruling (an empirical choice,
# not an algorithm change; P5-owned like the D9 family).
ABSORPTION_DELTA_RATIO = 0.5           # |delta| >= 0.5×v

_ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class AbsorptionEvent:
    """§4.6 reversal warning at a level (D19.6)."""

    level: str                 # key-level name or pool kind ('EQH'/'EQL')
    price: float               # the level / pool mean price
    delta_sign: int            # sign of the candle's delta: -1 | 0 | +1
    ts: datetime               # the absorbing candle


def candle_delta(candle: Candle) -> float:
    """§4.6 verbatim: taker_buy_v − (v − taker_buy_v)."""
    return candle.taker_buy_v - (candle.v - candle.taker_buy_v)


class VolumeEngine:
    """§4.6 for one symbol's 1m stream (cadence per D19.8, locked)."""

    __slots__ = ("_symbol", "_atr", "_buckets",
                 "_day", "_day_complete", "_last_ts",
                 "_sum_v", "_sum_tpv", "_sum_tp2v", "_cum_delta",
                 "_buffer", "_anchor_ts", "_anchor_v", "_anchor_tpv",
                 "_rvol", "_delta", "_spike", "_absorption", "_exhaustion")

    def __init__(self, symbol: str, atr: IncrementalATR) -> None:
        self._symbol = symbol
        self._atr = atr
        self._buckets: list[deque] = [deque(maxlen=RVOL_WINDOW_DAYS)
                                      for _ in range(1440)]
        self._day: date | None = None
        self._day_complete = False         # D19.3: observed from 00:00 on
        self._last_ts: datetime | None = None
        self._sum_v = self._sum_tpv = self._sum_tp2v = 0.0
        self._cum_delta = 0.0
        self._buffer: deque = deque(maxlen=ANCHOR_BUFFER_1M)
        self._anchor_ts: datetime | None = None
        self._anchor_v = self._anchor_tpv = 0.0
        self._rvol: float | None = None
        self._delta = 0.0
        self._spike = False
        self._absorption: AbsorptionEvent | None = None
        self._exhaustion: str | None = None

    # ------------------------------------------------------------ seeding

    def seed(self, candles) -> None:
        """D19.2: fold historical 1m volumes into the RVOL buckets ONLY —
        never the day/anchor state. Call once, chronologically, before
        the stream. Composition owns the storage read."""
        for candle in candles:
            self._buckets[_minute_of_day(candle.ts)].append(candle.v)

    # ---------------------------------------------------- intake: anchors

    def on_anchor(self, pivot) -> None:
        """Confirmed 5m pivot (A8) — the anchored-VWAP anchor (D19.4).
        Recomputes from the 1m buffer; anchor outside the contiguous
        buffer -> no anchor (D7)."""
        if not self._buffer or pivot.ts < self._buffer[0].ts:
            self._anchor_ts = None
            return
        self._anchor_ts = pivot.ts
        self._anchor_v = self._anchor_tpv = 0.0
        for candle in self._buffer:
            if candle.ts >= pivot.ts:
                self._anchor_v += candle.v
                self._anchor_tpv += _tp(candle) * candle.v

    # ------------------------------------------------------------ phase 1

    def update(self, candle: Candle) -> None:
        """Phase 1 (D19.8, locked: exactly once per closed 1m candle,
        before the trendline/liquidity consumers): rvol, session VWAP
        sums, anchored VWAP fold, delta, cum_delta, spike."""
        # RVOL — scored against the bucket BEFORE folding (D19.1)
        bucket = self._buckets[_minute_of_day(candle.ts)]
        if len(bucket) == RVOL_WINDOW_DAYS:
            med = _median_full(bucket)
            self._rvol = None if med <= 0 else candle.v / med
        else:
            self._rvol = None
        bucket.append(candle.v)

        # session (UTC day) accumulation — D19.3/D19.5
        day = candle.ts.date()
        contiguous = (self._last_ts is not None
                      and candle.ts == self._last_ts + _ONE_MINUTE)
        if day != self._day:
            self._day = day
            self._sum_v = self._sum_tpv = self._sum_tp2v = 0.0
            self._cum_delta = 0.0
            # complete iff the day starts at its very first minute
            self._day_complete = (candle.ts.hour == 0
                                  and candle.ts.minute == 0)
        elif not contiguous:
            self._day_complete = False     # hole inside the day (D7)
        tp = _tp(candle)
        self._sum_v += candle.v
        self._sum_tpv += tp * candle.v
        self._sum_tp2v += tp * tp * candle.v
        self._delta = candle_delta(candle)
        self._cum_delta += self._delta

        # anchored VWAP — contiguity-guarded buffer (D19.4)
        if self._buffer and candle.ts != self._buffer[-1].ts + _ONE_MINUTE:
            self._buffer.clear()           # hole: coverage broken (D7)
            self._anchor_ts = None
        self._buffer.append(candle)
        if self._anchor_ts is not None:
            self._anchor_v += candle.v
            self._anchor_tpv += tp * candle.v

        self._spike = (self._rvol is not None
                       and self._rvol >= SPIKE_RVOL_MIN)
        self._last_ts = candle.ts

    # ------------------------------------------------------------ phase 2

    def classify(self, candle: Candle, key_levels: dict, pools,
                 extremes: dict) -> None:
        """Phase 2 (D19.8, locked: exactly once per closed 1m candle,
        after liq.update): absorption + exhaustion from the frozen
        Liquidity Engine's post-update outputs — consumed, never
        recomputed."""
        self._absorption = None
        self._exhaustion = None
        atr = self._atr.value
        rng = candle.h - candle.l

        # absorption (D19.6): all four conditions on this candle
        if (self._spike and atr is not None
                and rng < ABSORPTION_RANGE_ATR_RATIO * atr
                and abs(self._delta) >= ABSORPTION_DELTA_RATIO * candle.v):
            band = ABSORPTION_LEVEL_BAND_ATR_RATIO * atr
            for name in sorted(key_levels):
                if _gap(candle.l, candle.h, key_levels[name]) <= band:
                    self._absorption = AbsorptionEvent(
                        name, key_levels[name], _sign(self._delta),
                        candle.ts)
                    break
            if self._absorption is None:
                for pool in pools:
                    if _gap(candle.l, candle.h, pool.price) <= band:
                        self._absorption = AbsorptionEvent(
                            pool.kind, pool.price, _sign(self._delta),
                            candle.ts)
                        break

        # exhaustion (D19.7): spike + dominant wick + at the day extreme
        if self._spike and rng > 0:
            upper = candle.h - max(candle.o, candle.c)
            lower = min(candle.o, candle.c) - candle.l
            day_h = extremes.get("DAY_H")
            day_l = extremes.get("DAY_L")
            if (upper / rng > EXHAUSTION_WICK_RATIO
                    and day_h is not None and candle.h >= day_h):
                self._exhaustion = "TOP"
            elif (lower / rng > EXHAUSTION_WICK_RATIO
                  and day_l is not None and candle.l <= day_l):
                self._exhaustion = "BOTTOM"

    # ---------------------------------------------------------- outputs

    @property
    def rvol(self) -> float | None:
        return self._rvol

    @property
    def session_vwap(self) -> float | None:
        if not self._day_complete or self._sum_v <= 0:
            return None
        return self._sum_tpv / self._sum_v

    def _sigma(self) -> float | None:
        vwap = self.session_vwap
        if vwap is None:
            return None
        var = max(0.0, self._sum_tp2v / self._sum_v - vwap * vwap)
        return sqrt(var)

    @property
    def band_1_up(self) -> float | None:
        s = self._sigma()
        return None if s is None else self.session_vwap + s

    @property
    def band_1_dn(self) -> float | None:
        s = self._sigma()
        return None if s is None else self.session_vwap - s

    @property
    def band_2_up(self) -> float | None:
        s = self._sigma()
        return None if s is None else self.session_vwap + 2 * s

    @property
    def band_2_dn(self) -> float | None:
        s = self._sigma()
        return None if s is None else self.session_vwap - 2 * s

    @property
    def anchored_vwap(self) -> float | None:
        if self._anchor_ts is None or self._anchor_v <= 0:
            return None
        return self._anchor_tpv / self._anchor_v

    @property
    def anchor_ts(self) -> datetime | None:
        return self._anchor_ts

    @property
    def delta(self) -> float:
        return self._delta

    @property
    def cum_delta(self) -> float | None:
        return self._cum_delta if self._day_complete else None

    @property
    def spike(self) -> bool:
        return self._spike

    @property
    def absorption(self) -> AbsorptionEvent | None:
        return self._absorption

    @property
    def exhaustion(self) -> str | None:
        return self._exhaustion


def _minute_of_day(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def _median_full(bucket) -> float:
    """Median of a FULL 20-slot bucket: mean of the two middles."""
    ordered = sorted(bucket)
    mid = RVOL_WINDOW_DAYS // 2
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _tp(candle: Candle) -> float:
    """Typical price (D19.3 pin): (h + l + c) / 3."""
    return (candle.h + candle.l + candle.c) / 3.0


def _gap(lo: float, hi: float, price: float) -> float:
    """Distance from the closed interval [lo, hi] to a point."""
    return max(0.0, max(lo, price) - min(hi, price))


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)

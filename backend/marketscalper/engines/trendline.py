"""Trendline Engine — candidate detection (roadmap P1.13; Architecture §4.3
Steps 1–2 + the frozen score formula; every rule per Decision D11).

Discovers candidate trendlines from confirmed 1m pivots (D11.2: the 1m
stream only — 5m pivots serve A8 external structure elsewhere):

    Step 1  last N=12 confirmed same-kind pivots per side; every ordered
            pair (a older, b newer) is a candidate line in LOG-price space
            on a POSITIONAL bar axis (D11.3/D11.4); direction filter —
            support slope > 0, resistance slope < 0 (strict; slope-zero is
            the Liquidity Engine's EQH/EQL territory); no close may cut
            the anchor segment (D11.6).
    Step 2  touches from the older anchor to the current bar, one per
            candle: |ln(extreme) - y(t)| <= 0.15*ATR(t)/close(t) with the
            close not strictly crossing the line; ANCHORS COUNT (D11.7);
            candidates need >= 3 touches.
    Score   touches*2 + span_bars/20 - bars_since_last_touch/100 (D11.8).

Dedup/cap (Step 3), break episodes (Step 4), lifecycle/channels (Step 5)
and persistence are the later trendline tasks — not here.

Cadence per closed 1m candle (established engine order): ATR update ->
pivot detection/labeling -> on_pivot fan-out -> update(candle).
candidates() is an on-demand pure function of the fold state — the
candidate SET changes only with pivots (D11.6) while touches/age evolve
per bar. Deterministic: no clock, no randomness; identical stream ->
identical candidate lists (replay-safe, no repaint — only confirmed
pivots and closed candles are ever consulted).

Memory is bounded: candle history is pruned to the oldest bar still
referenced by the two 12-pivot windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import Pivot
from marketscalper.providers.base import Candle

# Frozen §4.3/D11 literals — module constants, not config.
N_PIVOTS = 12
TOUCH_TOLERANCE_ATR_RATIO = 0.15
MIN_TOUCHES = 3
SCORE_TOUCH_WEIGHT = 2.0
SCORE_SPAN_DIVISOR = 20.0
SCORE_AGE_DIVISOR = 100.0


@dataclass(frozen=True)
class TrendlineCandidate:
    """One valid candidate line (>=3 touches), D11 geometry.

    y(t) = intercept + slope * (t - a_index), everything in log-price
    space over positional bar indices; intercept = ln(a_pivot.price).
    """

    side: str            # 'support' | 'resistance'
    a_index: int         # older anchor bar index
    b_index: int         # newer anchor bar index
    a_pivot: Pivot
    b_pivot: Pivot
    slope: float         # log-price per bar (D11.4)
    intercept: float     # ln(a_pivot.price)
    touches: int         # anchors included (D11.7)
    last_touch_index: int
    score: float         # D11.8


class TrendlineDetector:
    """§4.3 Step 1–2 candidate discovery for the 1m stream (one instance)."""

    __slots__ = ("_atr", "_bars", "_offset", "_next", "_ts_index",
                 "_h_pivots", "_l_pivots")

    def __init__(self, atr: IncrementalATR) -> None:
        self._atr = atr
        self._bars: list[tuple] = []     # (ts, low, high, close, atr_value)
        self._offset = 0                 # absolute index of _bars[0]
        self._next = 0                   # next bar index to assign
        self._ts_index: dict = {}        # candle ts -> absolute bar index
        self._h_pivots: list[tuple[int, Pivot]] = []   # (bar index, pivot)
        self._l_pivots: list[tuple[int, Pivot]] = []

    # ------------------------------------------------------------- intake

    def update(self, candle: Candle) -> None:
        """Fold one closed candle in (ATR already updated for it)."""
        self._ts_index[candle.ts] = self._next
        self._bars.append(
            (candle.ts, candle.l, candle.h, candle.c, self._atr.value))
        self._next += 1

    def on_pivot(self, pivot: Pivot) -> None:
        """Track a confirmed pivot; slides its kind's 12-window (D11.6)."""
        entry = (self._ts_index[pivot.ts], pivot)
        window = self._h_pivots if pivot.kind == "H" else self._l_pivots
        window.append(entry)
        if len(window) > N_PIVOTS:
            window.pop(0)
        self._prune()

    def _prune(self) -> None:
        """Drop history older than the oldest windowed pivot (bounded memory)."""
        needed = min(w[0][0] for w in (self._h_pivots, self._l_pivots) if w)
        while self._offset < needed:
            ts = self._bars.pop(0)[0]
            del self._ts_index[ts]
            self._offset += 1

    # --------------------------------------------------------- candidates

    def candidates(self) -> list[TrendlineCandidate]:
        """All valid candidates, deterministically ordered:
        score desc, then newer b, newer a, side. Empty while ATR unwarm
        (the engine is inactive until warm, D11.5)."""
        if self._atr.value is None or self._next == 0:
            return []
        cur = self._next - 1
        out: list[TrendlineCandidate] = []
        for side, window in (("support", self._l_pivots),
                             ("resistance", self._h_pivots)):
            for i in range(len(window)):
                for j in range(i + 1, len(window)):
                    cand = self._evaluate(side, window[i], window[j], cur)
                    if cand is not None:
                        out.append(cand)
        out.sort(key=lambda c: (-c.score, -c.b_index, -c.a_index, c.side))
        return out

    def _evaluate(self, side, a_entry, b_entry, cur):
        a_idx, a_pivot = a_entry
        b_idx, b_pivot = b_entry
        slope = (log(b_pivot.price) - log(a_pivot.price)) / (b_idx - a_idx)
        if side == "support":
            if not slope > 0:                      # D11.6: strictly ascending
                return None
        elif not slope < 0:                        # strictly descending
            return None
        intercept = log(a_pivot.price)

        support = side == "support"
        # D11.6 validity: no close strictly beyond the line BETWEEN anchors
        for t in range(a_idx + 1, b_idx):
            y = intercept + slope * (t - a_idx)
            close = self._bars[t - self._offset][3]
            log_close = log(close)
            if (log_close < y) if support else (log_close > y):
                return None

        # D11.7 touches: older anchor .. current bar, one per candle
        touches = 0
        last_touch = a_idx
        for t in range(a_idx, cur + 1):
            ts, low, high, close, atr = self._bars[t - self._offset]
            if atr is None:
                continue                           # bar unusable (unwarm)
            y = intercept + slope * (t - a_idx)
            log_close = log(close)
            if (log_close < y) if support else (log_close > y):
                continue                           # close crossed: no touch
            extreme = low if support else high
            tol = TOUCH_TOLERANCE_ATR_RATIO * atr / close
            if abs(log(extreme) - y) <= tol:
                touches += 1
                last_touch = t
        if touches < MIN_TOUCHES:
            return None

        span = b_idx - a_idx
        score = (SCORE_TOUCH_WEIGHT * touches + span / SCORE_SPAN_DIVISOR
                 - (cur - last_touch) / SCORE_AGE_DIVISOR)
        return TrendlineCandidate(side, a_idx, b_idx, a_pivot, b_pivot,
                                  slope, intercept, touches, last_touch, score)

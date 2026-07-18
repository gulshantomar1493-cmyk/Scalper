"""Structure Engine — pivot detection (roadmap P1.5; Architecture §4.2).

k-bar confirmed swing pivots, the atomic unit of all market structure:

    candle[i] is a SWING HIGH iff  high[i] > high[i-1..i-k]
                              AND  high[i] > high[i+1..i+k]   (strict, verbatim)

so a pivot at bar i is emitted exactly when bar i+k closes — lag accepted,
repaint rejected (§0 rule 1). Both ts (pivot candle identity, open time per
§3) and confirmed_ts (the confirming bar i+k) are recorded. Comparisons are
positional over closed candles, consistent with the no-synthetic-gap-candles
rule (P0.12). Detection is a pure fold over the candle stream: no clock, no
randomness — replay and live produce identical pivot sequences.

Persistence (R1 ruling, owner-approved with the P1.5 plan): capability only.
pivot_to_row() feeds the existing db.insert_pivot helper (P0.7, untouched);
nothing is wired in Phase 1 — engines join the composition without a pool,
so the production pivots table receives zero rows before the forward-run era.

Labeling (P1.6) happens at detection time — pivots are append-only, so the
label is part of the inserted row, never an update. Later structure tasks
(trend P1.8, BOS/CHOCH P1.9-P1.10) consume the labeled pivots.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# Frozen §4.2 confirmation depths: k=3 on 1m, k=2 on 5m. Not configuration.
K_BY_TF = {"1m": 3, "5m": 2}


@dataclass(frozen=True)
class Pivot:
    """One confirmed swing pivot — mirrors the pivots row (migration 002)."""

    symbol: str
    tf: str
    ts: datetime             # pivot candle time (candle identity = open time)
    confirmed_ts: datetime   # bar i+k, whose close confirmed the pivot
    kind: str                # 'H' | 'L'
    price: float             # the pivot high (H) / low (L)
    label: str | None = None  # 'HH'/'HL'/'LH'/'LL' — P1.6 fills before insert


def pivot_to_row(p: Pivot) -> dict:
    """Pivot -> keyword arguments of db.insert_pivot (P0.7), verbatim."""
    return {
        "symbol": p.symbol, "tf": p.tf, "ts": p.ts,
        "confirmed_ts": p.confirmed_ts, "kind": p.kind,
        "price": p.price, "label": p.label,
    }


class PivotDetector:
    """k-bar pivot detection for one (symbol, timeframe) stream.

    Sliding window of the last 2k+1 closed candles; every update evaluates
    the center bar once. Strict inequalities: an equal high/low neighbor
    blocks the pivot (frozen formula). A single bar can be both a swing
    high and a swing low (huge outside bar) — both are emitted, H first.
    """

    __slots__ = ("_symbol", "_tf", "_k", "_window")

    def __init__(self, symbol: str, tf: str) -> None:
        if tf not in K_BY_TF:
            raise ValueError(
                f"unsupported timeframe {tf!r} (supported: {tuple(K_BY_TF)})")
        self._symbol = symbol
        self._tf = tf
        self._k = K_BY_TF[tf]
        self._window: deque[Candle] = deque(maxlen=2 * self._k + 1)

    def update(self, candle: Candle) -> list[Pivot]:
        """Fold one closed candle in; return the pivots confirmed by it."""
        self._window.append(candle)
        if len(self._window) < self._window.maxlen:
            return []
        k = self._k
        center = self._window[k]
        confirmed_ts = self._window[-1].ts
        out: list[Pivot] = []
        if all(center.h > c.h for i, c in enumerate(self._window) if i != k):
            out.append(Pivot(self._symbol, self._tf, center.ts,
                             confirmed_ts, "H", center.h))
        if all(center.l < c.l for i, c in enumerate(self._window) if i != k):
            out.append(Pivot(self._symbol, self._tf, center.ts,
                             confirmed_ts, "L", center.l))
        return out


class PivotLabeler:
    """§4.2 label state machine for one (symbol, tf) stream (roadmap P1.6).

        kind H: label = HH if price > last_H.price else LH
        kind L: label = HL if price > last_L.price else LL

    Strict > verbatim — equality labels LH/LL. The first pivot of each kind
    has no comparison base: its label stays None (pinned seed rule) and its
    price still seeds the chain. H and L chains are independent. Labels are
    computed only from already-confirmed history — assigned once, never
    revised (no repaint).
    """

    __slots__ = ("_last_h", "_last_l")

    def __init__(self) -> None:
        self._last_h: float | None = None
        self._last_l: float | None = None

    def label(self, pivot: Pivot) -> Pivot:
        """Return a labeled copy (input never mutated); advance the chain."""
        if pivot.kind == "H":
            last, self._last_h = self._last_h, pivot.price
            name = None if last is None else ("HH" if pivot.price > last else "LH")
        else:
            last, self._last_l = self._last_l, pivot.price
            name = None if last is None else ("HL" if pivot.price > last else "LL")
        return replace(pivot, label=name)


class TrendState:
    """Trend-state machine BULLISH/BEARISH/RANGE (roadmap P1.8; Decision D10).

    Literal D10 transcription — memoryless classification, one evaluation
    per closed candle AFTER that candle's pivot processing (cadence:
    detector -> labeler -> on_pivot(each) -> update(candle); the just-closed
    candle is part of the last-20 window at its own evaluation). Rules, in
    the mandated order:

        1. either chain missing/unlabeled            -> None (unknown)
        2. band test (needs 20 closed candles):
           >= 12 of the last 20 bodies inside
           [min, max] of the last H/L pivot prices,
           inclusive edges, wicks irrelevant         -> RANGE (wins ties)
        3. labels HH and HL                          -> BULLISH
        4. labels LH and LL                          -> BEARISH
        5. otherwise (mixed labels)                  -> RANGE

    Timeframe-generic: the class never reads symbol/tf; instances pair with
    their stream by construction. No hysteresis — any state may follow any.
    """

    __slots__ = ("_last_h", "_last_l", "_bodies", "_state")

    _WINDOW = 20
    _INSIDE_MIN = 12          # D10: >= 60% of 20

    def __init__(self) -> None:
        self._last_h: Pivot | None = None
        self._last_l: Pivot | None = None
        self._bodies: deque[tuple[float, float]] = deque(maxlen=self._WINDOW)
        self._state: str | None = None

    @property
    def state(self) -> str | None:
        return self._state

    def on_pivot(self, pivot: Pivot) -> None:
        """Store the latest labeled pivot per kind (seeds included — their
        prices define the band while their None labels hold rule 1)."""
        if pivot.kind == "H":
            self._last_h = pivot
        else:
            self._last_l = pivot

    def update(self, candle: Candle) -> str | None:
        """Classify for the just-closed candle (D10 rules 1-5, in order)."""
        self._bodies.append((min(candle.o, candle.c), max(candle.o, candle.c)))
        h, l = self._last_h, self._last_l
        if h is None or l is None or h.label is None or l.label is None:
            self._state = None
        elif self._band_says_range(h.price, l.price):
            self._state = "RANGE"
        elif h.label == "HH" and l.label == "HL":
            self._state = "BULLISH"
        elif h.label == "LH" and l.label == "LL":
            self._state = "BEARISH"
        else:
            self._state = "RANGE"
        return self._state

    def _band_says_range(self, h_price: float, l_price: float) -> bool:
        if len(self._bodies) < self._WINDOW:
            return False                     # band asleep until 20 candles
        lo, hi = min(h_price, l_price), max(h_price, l_price)
        inside = sum(
            1 for b_lo, b_hi in self._bodies if b_lo >= lo and b_hi <= hi)
        return inside >= self._INSIDE_MIN


BOS_DISPLACEMENT_ATR_RATIO = 1.2   # frozen §4.2 literal (impulse vs drift)


@dataclass(frozen=True)
class BosEvent:
    """One break of structure — §4.2 continuation break (roadmap P1.9)."""

    symbol: str
    tf: str
    ts: datetime                 # breaking candle identity (open time)
    direction: str               # 'UP' | 'DOWN' (trend direction at break)
    broken_pivot: Pivot          # the confirmed swing that was broken
    close: float                 # the breaking close
    displacement: bool | None    # body > 1.2*ATR strict; None if ATR unwarm


class BosDetector:
    """BOS detection + displacement classification for one (symbol, tf)
    stream (roadmap P1.9; §4.2).

    BOS (continuation): CLOSE strictly beyond the last confirmed swing in
    trend direction — close > last H in BULLISH, close < last L in BEARISH.
    Nothing fires in RANGE/unknown; against-trend breaks are CHOCH (P1.10),
    never BOS. Each confirmed swing breaks at most once — the latch re-arms
    only when a NEW confirmed same-kind pivot arrives. Wicks through the
    level are not BOS (sweeps are P2 territory).

    Displacement (impulse vs drift): |c - o| > 1.2 * ATR(14) of the same
    stream at the breaking bar; strict (equality = weak); None while the
    ATR is unwarm. Cadence per closed candle: ATR update -> pivot
    detection/labeling -> on_pivot fan-out -> trend.update -> update.
    """

    __slots__ = ("_trend", "_atr", "_last_h", "_last_l", "_h_fired", "_l_fired")

    def __init__(self, trend: TrendState, atr: IncrementalATR) -> None:
        self._trend = trend
        self._atr = atr
        self._last_h: Pivot | None = None
        self._last_l: Pivot | None = None
        self._h_fired = False
        self._l_fired = False

    def on_pivot(self, pivot: Pivot) -> None:
        """Track the latest confirmed pivot per kind; a new pivot re-arms."""
        if pivot.kind == "H":
            if self._last_h is None or pivot.ts != self._last_h.ts:
                self._h_fired = False
            self._last_h = pivot
        else:
            if self._last_l is None or pivot.ts != self._last_l.ts:
                self._l_fired = False
            self._last_l = pivot

    def update(self, candle: Candle) -> BosEvent | None:
        """Evaluate the just-closed candle (trend already classified)."""
        state = self._trend.state
        if state == "BULLISH":
            pivot = self._last_h
            if pivot is None or self._h_fired or not candle.c > pivot.price:
                return None
            self._h_fired = True
            direction = "UP"
        elif state == "BEARISH":
            pivot = self._last_l
            if pivot is None or self._l_fired or not candle.c < pivot.price:
                return None
            self._l_fired = True
            direction = "DOWN"
        else:
            return None                      # no trend, no continuation
        atr = self._atr.value
        displacement = None if atr is None else (
            abs(candle.c - candle.o) > BOS_DISPLACEMENT_ATR_RATIO * atr)
        return BosEvent(candle.symbol, candle.tf, candle.ts, direction,
                        pivot, candle.c, displacement)


@dataclass(frozen=True)
class ChochEvent:
    """Reversal warning — first close beyond the last confirmed swing
    AGAINST trend (§4.2; roadmap P1.10). Alone it confirms nothing."""

    symbol: str
    tf: str
    ts: datetime                 # the CHOCH candle identity (open time)
    direction: str               # 'DOWN' (in BULLISH) | 'UP' (in BEARISH)
    broken_pivot: Pivot          # the against-side swing closed through
    close: float
    prior_trend: str             # the trend being warned against


@dataclass(frozen=True)
class ConfirmedFlip:
    """CHOCH + later same-direction BOS = confirmed trend flip (§4.2)."""

    symbol: str
    tf: str
    ts: datetime                 # the confirming BOS bar
    direction: str               # the new trend direction (== choch.direction)
    choch: ChochEvent
    bos: BosEvent


class ChochDetector:
    """CHOCH detection + confirmed-flip logic for one (symbol, tf) stream
    (roadmap P1.10; §4.2).

    CHOCH: first CLOSE strictly beyond the last confirmed against-side
    swing while a trend is established — close < last L in BULLISH (DOWN),
    close > last H in BEARISH (UP). Once per pivot (latch re-armed only by
    a NEW confirmed same-kind pivot); wick-throughs are sweeps (P2), never
    CHOCH; RANGE/unknown trend -> nothing.

    Flip: a pending CHOCH is confirmed by a LATER BOS in the same
    direction; a BOS in the old trend direction cancels it; a newer CHOCH
    replaces it. No timeout, no price-based auto-confirmation — CHOCH
    alone never flips anything. Cadence per closed candle: ATR -> pivots ->
    on_pivot fan-out -> trend.update -> bos.update -> on_bos(event if any)
    -> update(candle) — a CHOCH can never confirm on its own bar.
    """

    __slots__ = ("_trend", "_last_h", "_last_l", "_h_fired", "_l_fired",
                 "_pending")

    def __init__(self, trend: TrendState) -> None:
        self._trend = trend
        self._last_h: Pivot | None = None
        self._last_l: Pivot | None = None
        self._h_fired = False
        self._l_fired = False
        self._pending: ChochEvent | None = None

    @property
    def pending_flip(self) -> str | None:
        """Direction of the unconfirmed reversal warning, if any."""
        return None if self._pending is None else self._pending.direction

    def on_pivot(self, pivot: Pivot) -> None:
        """Track the latest confirmed pivot per kind; a new pivot re-arms."""
        if pivot.kind == "H":
            if self._last_h is None or pivot.ts != self._last_h.ts:
                self._h_fired = False
            self._last_h = pivot
        else:
            if self._last_l is None or pivot.ts != self._last_l.ts:
                self._l_fired = False
            self._last_l = pivot

    def on_bos(self, bos: BosEvent) -> ConfirmedFlip | None:
        """Feed every BOS of the stream; returns the flip it confirms."""
        pending = self._pending
        if pending is None:
            return None                      # ordinary continuation BOS
        self._pending = None                 # confirmed or cancelled below
        if bos.direction == pending.direction:
            return ConfirmedFlip(bos.symbol, bos.tf, bos.ts,
                                 pending.direction, pending, bos)
        return None                          # old trend resumed: cancelled

    def update(self, candle: Candle) -> ChochEvent | None:
        """Evaluate the just-closed candle (trend + BOS already processed)."""
        state = self._trend.state
        if state == "BULLISH":
            pivot = self._last_l
            if pivot is None or self._l_fired or not candle.c < pivot.price:
                return None
            self._l_fired = True
            direction = "DOWN"
        elif state == "BEARISH":
            pivot = self._last_h
            if pivot is None or self._h_fired or not candle.c > pivot.price:
                return None
            self._h_fired = True
            direction = "UP"
        else:
            return None                      # no trend, nothing to be against
        event = ChochEvent(candle.symbol, candle.tf, candle.ts, direction,
                           pivot, candle.c, state)
        self._pending = event
        return event

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

from marketscalper.providers.base import Candle

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

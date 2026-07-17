"""Kline reconciliation (roadmap P0.14; Decisions D5/A1).

Compares the runtime truth (tick-built 1m candles) against Binance's own
closed kline_1m reference candles. Mismatches are LOGGED — never repaired,
never overwritten, never persisted. Both inputs are frozen dataclasses; this
module modifies nothing.

Sources are wired EXPLICITLY: the composition point feeds built candles to
on_built() and reference candles to on_reference(). No bus self-subscription,
no arrival-order inference — either side may arrive first; pairing is by
(symbol, ts) and comparison is labeled by intake, not by timing.

Unpaired candles remain pending until their counterpart arrives. There is no
expiry policy: missing counterparts are a data-gap concern owned by backfill
(P0.15), not by reconciliation.

Comparison rule (D5): o/h/l/c exact; v, qv, n_trades, taker_buy_v within
0.1% relative tolerance (aggTrade-vs-kline aggregation timing differs
legitimately at the margin).
"""

from __future__ import annotations

import logging

from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)

_EXACT_FIELDS = ("o", "h", "l", "c")
_TOLERANT_FIELDS = ("v", "qv", "n_trades", "taker_buy_v")
_REL_TOLERANCE = 0.001  # 0.1%


def _within_tolerance(a: float, b: float) -> bool:
    if a == b:
        return True
    return abs(a - b) <= _REL_TOLERANCE * max(abs(a), abs(b))


def compare_candles(built: Candle, reference: Candle) -> list[str]:
    """Pure D5 comparison. Returns mismatch descriptions; empty list = match."""
    mismatches: list[str] = []
    for field in _EXACT_FIELDS:
        bv, rv = getattr(built, field), getattr(reference, field)
        if bv != rv:
            mismatches.append(f"{field}: built={bv} reference={rv}")
    for field in _TOLERANT_FIELDS:
        bv, rv = getattr(built, field), getattr(reference, field)
        if not _within_tolerance(bv, rv):
            mismatches.append(f"{field}: built={bv} reference={rv} (>0.1% apart)")
    return mismatches


class KlineReconciler:
    """Pairs built vs reference 1m candles by (symbol, ts); logs mismatches.

    Counters (read by the P0.28 gate):
      pairs_compared — completed comparisons.
      mismatches     — comparisons with at least one field mismatch.
    """

    def __init__(self) -> None:
        self._pending_built: dict[tuple[str, object], Candle] = {}
        self._pending_reference: dict[tuple[str, object], Candle] = {}
        self.pairs_compared = 0
        self.mismatches = 0

    def on_built(self, candle: Candle) -> None:
        """Intake for tick-built (runtime truth) 1m candles."""
        self._intake(candle, self._pending_built, self._pending_reference, "built")

    def on_reference(self, candle: Candle) -> None:
        """Intake for closed kline_1m reference candles."""
        self._intake(candle, self._pending_reference, self._pending_built, "reference")

    # internal ---------------------------------------------------------------

    def _intake(
        self,
        candle: Candle,
        own_pending: dict,
        other_pending: dict,
        side: str,
    ) -> None:
        if candle.tf != "1m":
            raise ValueError(f"reconciliation is 1m-only, got tf={candle.tf!r}")
        key = (candle.symbol, candle.ts)

        if key in own_pending:
            # Same side twice for one minute: keep the first, never rewrite.
            log.warning(
                "reconciler: duplicate %s candle for %s %s — keeping the first",
                side, candle.symbol, candle.ts,
            )
            return

        counterpart = other_pending.pop(key, None)
        if counterpart is None:
            own_pending[key] = candle
            return

        built, reference = (
            (candle, counterpart) if side == "built" else (counterpart, candle)
        )
        self._compare_pair(key, built, reference)

    def _compare_pair(self, key, built: Candle, reference: Candle) -> None:
        self.pairs_compared += 1
        problems = compare_candles(built, reference)
        if problems:
            self.mismatches += 1
            log.warning(
                "reconciler: MISMATCH %s %s — %s | built=%s | reference=%s",
                key[0], key[1], "; ".join(problems), built, reference,
            )
        else:
            log.debug("reconciler: match %s %s", key[0], key[1])

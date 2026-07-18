"""Recommendation lifecycle engine (Architecture §7; Decision D22/A16.1;
roadmap P4.2).

Per symbol, tracks each recommendation from `active` to exactly one
terminal state — `invalidated` (INVALID rule / opposite signal / G1
fail), `expired` (evaluation horizon reached un-resolved), or `evaluated`
(the hypothetical position reached SL or a TP, via the frozen A16.2
evaluator). Emits one LifecycleEvent per transition (the composition
routes it to the UI diff and the P4.5 persistence). Pure per-closed-1m-
candle fold over frozen outputs + the recommendation rows — no execution,
deterministic in replay and live forward-run (§0 rule 2).

Consumes the evaluator (P4.3) as the single source of truth for the
`evaluated`/`expired` decisions — no duplicated candle geometry.

COMPLETE and FROZEN (roadmap P4.2; three-agent freeze audit 2026-07-19 —
one blocker fixed pre-freeze: the recommendation identity is
(created_ts, strategy), unique per symbol, since created_ts alone
collides when S1/S2/S3 admit on one bar). Do not modify without a new
decision record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from marketscalper.engines.evaluator import (
    ENTRY_WINDOW_BARS,
    EVAL_HORIZON_BARS,
    Outcome,
    evaluate_outcome,
)
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1


@dataclass(frozen=True)
class LifecycleEvent:
    """One terminal transition (§7). `outcome` is present for
    'evaluated'/'expired' (carries the eval_* numbers), None for
    'invalidated'."""

    rec_key: tuple             # (created_ts, strategy) — unique per symbol
    direction: str             # (D20.1: <=1 signal per strategy per bar)
    status: str                # 'invalidated' | 'expired' | 'evaluated'
    reason: str
    ts: datetime               # the transition candle's ts
    outcome: Outcome | None


@dataclass
class _Active:
    rec: dict                  # direction/entry/sl/tp1/tp2/created_ts
    candles: list = field(default_factory=list)   # from the creation bar


class RecommendationLifecycle:
    """One symbol's active-recommendation set (D22.1)."""

    __slots__ = ("_symbol", "_active", "_entry_window", "_horizon")

    def __init__(self, symbol: str,
                 entry_window: int = ENTRY_WINDOW_BARS,
                 horizon: int = EVAL_HORIZON_BARS) -> None:
        self._symbol = symbol
        self._active: list[_Active] = []
        self._entry_window = entry_window
        self._horizon = horizon

    # ------------------------------------------------------------ intakes

    def on_recommendation(self, rec: dict, creation_candle: Candle) -> None:
        """Register a newly-admitted recommendation (D21.2). Its creation
        bar is candles[0]; the entry can only trigger on a LATER bar."""
        self._active.append(_Active(rec=rec, candles=[creation_candle]))

    # --------------------------------------------------------------- fold

    def update(self, candle: Candle, *, opposite_signals=(),
               g1_ok: bool = True) -> list[LifecycleEvent]:
        """Advance every active recommendation one closed 1m candle.

        opposite_signals: the (strategy, direction) pairs emitted THIS bar
        — an active rec invalidates on an opposite-direction signal of the
        same strategy family (D22.1b). g1_ok False invalidates all active
        recs (data-integrity loss, D22.1c). Returns this bar's terminal
        transitions in a deterministic order (registration order)."""
        events: list[LifecycleEvent] = []
        opp = set(opposite_signals)
        still: list[_Active] = []
        for a in self._active:
            a.candles.append(candle)
            ev = self._advance(a, candle, opp, g1_ok)
            if ev is None:
                still.append(a)            # stays active
            else:
                events.append(ev)
        self._active = still
        return events

    def _advance(self, a: _Active, candle: Candle, opp: set,
                 g1_ok: bool) -> LifecycleEvent | None:
        rec = a.rec
        family = rec.get("strategy")
        key = (rec["created_ts"], family)        # unique per symbol (D20.1)
        direction = rec["direction"]

        # (c) data-integrity loss, then (b) opposite signal — external
        # invalidations checked before the geometry (D22.1 order).
        if not g1_ok:
            return self._invalidate(key, direction, candle,
                                    "G1 data-integrity fail")
        want_opp = "SHORT" if direction == "LONG" else "LONG"
        if (family, want_opp) in opp:
            return self._invalidate(key, direction, candle,
                                    "opposite-direction signal")

        # D22.1a: the entry window is the Signal's own invalid_after_bars
        # (per-rec, falling back to the module default).
        entry_window = rec.get("invalid_after_bars", self._entry_window)
        out = evaluate_outcome(
            direction=direction, entry=rec["entry"], sl=rec["sl"],
            tp1=rec["tp1"], tp2=rec.get("tp2"), candles=a.candles,
            entry_window=entry_window, horizon=self._horizon)
        bars_elapsed = len(a.candles) - 1        # creation bar is 0

        if not out.filled:
            # (a) INVALID rule — entry zone not filled in the window
            if bars_elapsed >= entry_window:
                return self._invalidate(
                    key, direction, candle,
                    f"entry unfilled in {entry_window} candles")
            return None                          # still waiting for a fill

        if out.outcome in ("sl", "tp1", "tp2"):
            return LifecycleEvent(key, direction, "evaluated",
                                  f"hypothetical {out.outcome}", candle.ts,
                                  out)
        # filled but un-resolved: expire only at the horizon
        bars_since_fill = bars_elapsed - out.fill_index
        if bars_since_fill >= self._horizon:
            return LifecycleEvent(key, direction, "expired",
                                  "evaluation horizon reached", candle.ts,
                                  out)
        return None                              # still resolving

    @staticmethod
    def _invalidate(key, direction, candle, reason) -> LifecycleEvent:
        return LifecycleEvent(key, direction, "invalidated", reason,
                              candle.ts, None)

    @property
    def active_count(self) -> int:
        return len(self._active)

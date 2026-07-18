"""Hypothetical outcome evaluator (Architecture §7; Decision D22/A16.2;
roadmap P4.3).

A pure candle-geometry simulation of "what this recommendation would have
done" — NEVER an execution, NEVER an order (v1.2). Given a recommendation
(direction + entry/SL/TP1/TP2) and the closed 1m candles from its creation
bar onward, it computes the entry trigger, the first terminal touch
(SL-first on same-bar ambiguity, gap-through at the bar open), and the R
multiples (eval_r / eval_mae / eval_mfe). Deterministic: the same
recommendation + candle stream yields byte-identical output in replay and
live forward-run (§0 rule 2).

No wiring, no persistence here (the P1.1/risk.py pure-capability
precedent) — the lifecycle engine (P4.2) drives it and the composition
persists via db.update_recommendation_eval (P4.5).

COMPLETE and FROZEN (roadmap P4.3; three-agent freeze audit 2026-07-19 —
one conformance blocker fixed pre-freeze: R is normalized by the fill
risk (fill−SL) per A16.2, not the planned risk). Do not modify without a
new decision record.
"""

from __future__ import annotations

from dataclasses import dataclass

from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# A16.3 — post-fill resolution horizon (uncalibrated, P5-owned).
EVAL_HORIZON_BARS = 240

# A16.1 — entry-fill window default (the Signal's own invalid_after_bars;
# restated as the module default for standalone evaluation).
ENTRY_WINDOW_BARS = 5


@dataclass(frozen=True)
class Outcome:
    """One hypothetical evaluation result (§7 eval_* columns)."""

    outcome: str               # 'sl' | 'tp1' | 'tp2' | 'none'
    eval_r: float | None       # realized R (None only if never triggered)
    eval_mae: float | None     # max adverse excursion, R (<= 0)
    eval_mfe: float | None     # max favorable excursion, R (>= 0)
    filled: bool               # did the entry trigger within the window?
    fill_index: int | None     # candle offset (from creation) of the fill
    exit_index: int | None     # candle offset of the terminal resolution


def evaluate_outcome(
    *,
    direction: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float | None,
    candles: list[Candle],
    entry_window: int = ENTRY_WINDOW_BARS,
    horizon: int = EVAL_HORIZON_BARS,
) -> Outcome:
    """§7/A16.2 verbatim. `candles` start at the recommendation's creation
    bar (index 0 = the bar it was emitted on; the entry can trigger on a
    LATER bar — the creation bar itself is not an entry bar, mirroring the
    engines' never-the-creation-bar discipline)."""
    long = direction == "LONG"
    planned_risk = (entry - sl) if long else (sl - entry)
    if planned_risk <= 0:       # degenerate rec geometry — not evaluable
        return Outcome("none", None, None, None, False, None, None)

    # ---- entry trigger within [1, entry_window] (never the creation bar)
    fill_index = None
    fill_price = None
    last_entry_bar = min(entry_window, len(candles) - 1)
    for i in range(1, last_entry_bar + 1):
        c = candles[i]
        gap = (c.o <= entry) if long else (c.o >= entry)
        touch = c.l <= entry <= c.h
        if gap:
            fill_index, fill_price = i, c.o        # realistic worse fill
            break
        if touch:
            fill_index, fill_price = i, entry
            break
    if fill_index is None:
        return Outcome("none", None, None, None, False, None, None)

    # A16.2: R is normalized by the risk taken AT the fill (fill − SL),
    # which equals the planned risk on a range-touch fill but differs on
    # a gap-through fill (fill ≠ entry).
    fill_risk = (fill_price - sl) if long else (sl - fill_price)
    if fill_risk <= 0:
        # the entry gapped through the stop — an immediate catastrophic
        # stop; realized R measured against the (positive) planned risk,
        # which the gap already exceeded (A16.2 degenerate-fill guard).
        r = ((fill_price - entry) / planned_risk if long
             else (entry - fill_price) / planned_risk)
        return Outcome("sl", r, min(0.0, r), max(0.0, r), True,
                       fill_index, fill_index)

    def r_of(price: float) -> float:
        return ((price - fill_price) / fill_risk if long
                else (fill_price - price) / fill_risk)

    # ---- terminal resolution from the fill bar to the horizon
    mae = 0.0
    mfe = 0.0
    end = min(fill_index + horizon, len(candles) - 1)
    for i in range(fill_index, end + 1):
        c = candles[i]
        mae = min(mae, r_of(c.l) if long else r_of(c.h))
        mfe = max(mfe, r_of(c.h) if long else r_of(c.l))
        # gap-through at the open, evaluated before the intrabar range
        if long:
            if c.o <= sl:
                return _done("sl", r_of(c.o), mae, mfe, fill_index, i)
            if tp2 is not None and c.o >= tp2:
                return _done("tp2", r_of(c.o), mae, mfe, fill_index, i)
            if c.o >= tp1:
                return _done("tp1", r_of(c.o), mae, mfe, fill_index, i)
        else:
            if c.o >= sl:
                return _done("sl", r_of(c.o), mae, mfe, fill_index, i)
            if tp2 is not None and c.o <= tp2:
                return _done("tp2", r_of(c.o), mae, mfe, fill_index, i)
            if c.o <= tp1:
                return _done("tp1", r_of(c.o), mae, mfe, fill_index, i)
        # intrabar: SL checked before TP1 (same-bar ambiguity -> SL, §7)
        hit_sl = (c.l <= sl) if long else (c.h >= sl)
        hit_tp1 = (c.h >= tp1) if long else (c.l <= tp1)
        if hit_sl:
            return _done("sl", r_of(sl), mae, mfe, fill_index, i)
        if hit_tp1:
            return _done("tp1", r_of(tp1), mae, mfe, fill_index, i)

    # ---- horizon reached un-resolved: mark-to-market at the last close
    mtm = r_of(candles[end].c)
    return Outcome("none", mtm, mae, mfe, True, fill_index, end)


def _done(outcome, eval_r, mae, mfe, fill_index, exit_index) -> Outcome:
    # a terminal touch is itself an excursion — keep MAE/MFE consistent
    mae = min(mae, eval_r)
    mfe = max(mfe, eval_r)
    return Outcome(outcome, eval_r, mae, mfe, True, fill_index, exit_index)

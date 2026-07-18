"""SignalRecorder — persist signals + admitted recommendations (P3.18).

Composition-owned (D21.6): constructed only in live `main()` with the
pool and the D1 engine-version stamp; replay sessions and tests never
build one, so replay writes nothing (R1/F2 discipline). The pipeline
computes everything pure — this class only records what was already
decided, on the bus dispatch path, with the CandleWriter error doctrine:
failures are caught, logged and counted; the analysis chain never dies
on a database error.
"""

from __future__ import annotations

import json
import logging
import subprocess

import asyncpg

from marketscalper import db
from marketscalper.engines import (confluence, evaluator, fvg, liquidity,
                                   lifecycle, momentum, orderblock,
                                   psychology, qualification, risk,
                                   strategy, structure, trendline, volume)

log = logging.getLogger(__name__)

# The complete engine roster stamped on every signal row (D1). Includes
# the post-signal recommendation engines (evaluator/lifecycle) and the
# psychology guard (which shapes a recorded signal's G5/verdict), so every
# declared ENGINE_VERSION is forensically live — no dead constants.
_STAMP_ENGINES = (
    ("structure", structure), ("trendline", trendline),
    ("liquidity", liquidity), ("orderblock", orderblock), ("fvg", fvg),
    ("volume", volume), ("momentum", momentum), ("confluence", confluence),
    ("qualification", qualification), ("risk", risk), ("strategy", strategy),
    ("evaluator", evaluator), ("lifecycle", lifecycle),
    ("psychology", psychology),
)


def engine_version_stamp() -> str:
    """D1: `<git-short-hash>+<engine>=<n>;...` — assembled once at
    startup; `nogit` outside a repository."""
    try:
        short = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "nogit"
    except (OSError, subprocess.TimeoutExpired):
        short = "nogit"
    parts = ";".join(f"{name}={mod.ENGINE_VERSION}"
                     for name, mod in _STAMP_ENGINES)
    return f"{short}+{parts}"


def build_reason_text(symbol: str, signal, qual, plan) -> str:
    """§8 deterministic rule-trace (no LLM) — the journal AUTO context.
    Built from the signal facts + the qualification reasons (both already
    in the state_snapshot, A17) + the §7 risk line. Pure/deterministic."""
    lines = [f"{signal.direction} {symbol} @ {plan.entry:.10g} | "
             f"{signal.strategy} | Score {qual.score:.0f}"]
    lines.extend(signal.facts)
    lines.extend(qual.reasons)
    lines.append(f"Risk: SL {plan.sl:.10g} | Net RR "
                 f"{plan.net_rr_tp1:.2f} to TP1")
    return "\n".join(lines)


class SignalRecorder:
    """One per process; shared across symbols (the pool serializes)."""

    def __init__(self, pool: asyncpg.Pool, stamp: str) -> None:
        self._pool = pool
        self._stamp = stamp
        self._rec_ids: dict = {}       # (symbol, created_ts, strategy) -> id
        self.signals_written = 0
        self.recommendations_written = 0
        self.journal_written = 0
        self.lifecycle_written = 0
        self.failures = 0

    async def record(self, symbol: str, records, payload) -> None:
        """Persist one bar's (signal, qual, plan, rec|None) tuples.

        The signal row always inserts (D21.1); the recommendation row
        only when admitted AND its signal row landed (FK discipline)."""
        snapshot = json.dumps(payload, sort_keys=True) if payload else None
        for signal, qual, plan, rec in records:
            try:
                async with self._pool.acquire() as conn:
                    signal_id = await db.insert_signal(
                        conn,
                        ts=signal.created_ts, symbol=symbol, tf="1m",
                        strategy=signal.strategy,
                        direction=signal.direction,
                        score=qual.score,
                        gates=json.dumps({
                            "verdict": qual.verdict,
                            "data_integrity": qual.data_integrity,
                            "agreement": qual.agreement,
                            "gates": [{"name": g.name, "passed": g.passed,
                                       "flagged": g.flagged,
                                       "detail": g.detail}
                                      for g in qual.gates],
                        }, sort_keys=True),
                        components=(json.dumps(qual.components,
                                               sort_keys=True)
                                    if qual.components is not None
                                    else None),
                        state_snapshot=snapshot,
                        engine_version=self._stamp,
                    )
                    self.signals_written += 1
                    if rec is not None:
                        rec_id = await db.insert_recommendation(
                            conn,
                            signal_id=signal_id, ts=signal.created_ts,
                            direction=signal.direction,
                            entry_px=plan.entry, sl=plan.sl,
                            tp1=plan.tp1, tp2=plan.tp2,
                            suggested_qty=plan.qty,
                            risk_amt=plan.risk_amt,
                            est_fees=plan.qty * plan.fee_per_unit,
                            net_rr_tp1=plan.net_rr_tp1,
                        )
                        self.recommendations_written += 1
                        # P4.7: write the DB row id back onto the shared rec
                        # dict so the NEXT payload carries it (the deque
                        # holds this same object) — the quick-log form needs
                        # it to target /journal/{id}. Live-only; replay never
                        # sets it (no recorder), so the form is disabled there.
                        rec["id"] = rec_id
                        # remember the row id for the P4.5 lifecycle writes,
                        # keyed by the UNIQUE rec identity (created_ts alone
                        # is not unique — S1/S2/S3 can admit on one bar)
                        self._rec_ids[(symbol, signal.created_ts.isoformat(),
                                       signal.strategy)] = rec_id
                        # P4.6: seed the journal AUTO context (§8 rule-trace;
                        # A17 — no PNG dependency, psychology is P4.9)
                        await db.insert_journal_seed(
                            conn, recommendation_id=rec_id,
                            reason_text=build_reason_text(symbol, signal,
                                                          qual, plan),
                            chart_snapshot_path=None, rule_violations=None)
                        self.journal_written += 1
            except Exception:
                self.failures += 1
                log.exception("recorder: failed to persist %s %s signal "
                              "at %s (analysis continues)",
                              symbol, signal.strategy, signal.created_ts)

    async def record_lifecycle(self, symbol: str, events) -> None:
        """Persist a bar's lifecycle transitions (P4.5): the status columns
        always, and the eval_* columns for evaluated/expired. Same error
        doctrine — a DB failure never stops the analysis chain. A rec whose
        insert failed (no id) is skipped; a terminal transition frees its
        id (each rec transitions exactly once, D22.1)."""
        for ev in events:
            # rec_key = (created_ts, strategy) — unique per symbol (D20.1)
            key = (symbol, ev.rec_key[0], ev.rec_key[1])
            rec_id = self._rec_ids.get(key)
            if rec_id is None:
                continue                       # signal insert failed earlier
            try:
                async with self._pool.acquire() as conn:
                    # status + eval in one transaction so an evaluated row
                    # never lands with NULL eval_* (the invariant holds)
                    async with conn.transaction():
                        await db.update_recommendation_status(
                            conn, rec_id, status=ev.status, status_ts=ev.ts,
                            status_reason=ev.reason)
                        if ev.outcome is not None:
                            await db.update_recommendation_eval(
                                conn, rec_id,
                                eval_outcome=ev.outcome.outcome,
                                eval_r=ev.outcome.eval_r,
                                eval_mae=ev.outcome.eval_mae,
                                eval_mfe=ev.outcome.eval_mfe)
                    self.lifecycle_written += 1
                # each recommendation transitions once -> free the id
                self._rec_ids.pop(key, None)
            except Exception:
                self.failures += 1
                log.exception("recorder: failed to persist %s lifecycle "
                              "%s at %s (analysis continues)",
                              symbol, ev.status, ev.ts)

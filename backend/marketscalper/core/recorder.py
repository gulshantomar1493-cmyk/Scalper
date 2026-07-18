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
from marketscalper.engines import (confluence, fvg, liquidity, momentum,
                                   orderblock, qualification, risk,
                                   strategy, structure, trendline, volume)

log = logging.getLogger(__name__)

_STAMP_ENGINES = (
    ("structure", structure), ("trendline", trendline),
    ("liquidity", liquidity), ("orderblock", orderblock), ("fvg", fvg),
    ("volume", volume), ("momentum", momentum), ("confluence", confluence),
    ("qualification", qualification), ("risk", risk), ("strategy", strategy),
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


class SignalRecorder:
    """One per process; shared across symbols (the pool serializes)."""

    def __init__(self, pool: asyncpg.Pool, stamp: str) -> None:
        self._pool = pool
        self._stamp = stamp
        self.signals_written = 0
        self.recommendations_written = 0
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
                        await db.insert_recommendation(
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
            except Exception:
                self.failures += 1
                log.exception("recorder: failed to persist %s %s signal "
                              "at %s (analysis continues)",
                              symbol, signal.strategy, signal.created_ts)

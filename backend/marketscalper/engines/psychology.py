"""Psychology guards (Architecture §8; Decision D23; roadmap P4.9).

Rule-based (not judgmental) behavioral circuit-breakers over the owner's
manually-logged TAKEN trades, feeding the §6 G5 risk-budget gate:

- revenge: a new signal within 5 minutes of a logged LOSS on the same
  symbol → G5 fail.
- hard lock: more than 8 taken trades in the UTC day → G5 fail until the
  next day.
- overtrade warn: more than 6 taken trades in the UTC day → warn (G5
  still passes).

Composition-owned, account-level, in-memory, LIVE-ONLY (manual journal
data exists only in a forward-run; replay/tests build no guard, so G5
stays the D16.2 flagged placeholder — the legacy behavior byte-identical).
The guard is therefore outside the deterministic replay contract. Pure
fold over the recorded taken trades; no clock of its own (the caller
passes `now`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# Frozen §8/D23 thresholds — uncalibrated, P5-owned.
REVENGE_WINDOW_MIN = 5              # new signal < 5 min after a logged loss
OVERTRADE_WARN = 6                  # > 6 taken/day -> warn
OVERTRADE_LOCK = 8                  # > 8 taken/day -> hard lock (fail)


@dataclass(frozen=True)
class G5State:
    """The per-bar G5 verdict (D23.2)."""

    passed: bool
    locked: bool
    warn: bool
    detail: str


class PsychologyGuard:
    """One per account; shared across symbols. In-memory, live-only."""

    __slots__ = ("_taken",)

    def __init__(self) -> None:
        # recommendation_id -> (logged_at, symbol, result) — keyed so a
        # re-logged journal row updates in place (no double-count, D23.5)
        self._taken: dict = {}

    def record_taken(self, recommendation_id, logged_at: datetime,
                     symbol: str, result: str) -> None:
        """Record (or update) a taken trade. Only call for taken=True with
        a non-null result (win/loss/be); skipped trades are never here.
        Self-prunes so the map stays bounded to ~the current UTC day on a
        long forward-run (records only enter here, so this is sufficient —
        evaluate() already filters stale records out of its output)."""
        self._taken[recommendation_id] = (logged_at, symbol, result)
        self.prune(logged_at)

    def forget(self, recommendation_id) -> None:
        """Drop a record (e.g. the owner un-takes a row)."""
        self._taken.pop(recommendation_id, None)

    def evaluate(self, now: datetime, symbol: str) -> G5State:
        """The G5 verdict for a signal on `symbol` at time `now` (D23.2)."""
        taken_today = sum(1 for (ts, _sym, _r) in self._taken.values()
                          if ts.date() == now.date())
        window = timedelta(minutes=REVENGE_WINDOW_MIN)
        revenge = any(
            r == "loss" and sym == symbol
            and timedelta(0) <= now - ts < window
            for (ts, sym, r) in self._taken.values())
        locked = taken_today > OVERTRADE_LOCK
        warn = (not locked) and taken_today > OVERTRADE_WARN

        if revenge:
            return G5State(False, locked, warn,
                           "revenge guard: <5 min after a logged loss")
        if locked:
            return G5State(False, True, False,
                           f"hard lock: {taken_today} taken today (> "
                           f"{OVERTRADE_LOCK})")
        if warn:
            return G5State(True, False, True,
                           f"overtrade warn: {taken_today} taken today (> "
                           f"{OVERTRADE_WARN})")
        return G5State(True, False, False,
                       f"{taken_today} taken today")

    def prune(self, now: datetime) -> None:
        """Drop records older than the current UTC day and the revenge
        window — bounds the map on a long forward-run (D23.1)."""
        cutoff = datetime(now.year, now.month, now.day,
                          tzinfo=now.tzinfo) - timedelta(
            minutes=REVENGE_WINDOW_MIN)
        self._taken = {rid: v for rid, v in self._taken.items()
                       if v[0] >= cutoff}

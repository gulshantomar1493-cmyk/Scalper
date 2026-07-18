"""Liquidity Engine — COMPLETE and FROZEN (engine-wise freeze after the
D12 conformance audit; Architecture §4.4; Decision D12 incl. its
freeze-audit addenda; roadmap P2.8–P2.13). Modify only on a genuine
production defect.

EQH/EQL pools, key levels (PDH/PDL, PWH/PWL, session H/L per the D12.1
map), sweep detection with the D12.5 sweep+shift tag, and premium/discount
on the A8 external (5m-pivot) range.

Pure consumer of frozen upstream outputs: 1m confirmed labeled pivots
(pools), 5m confirmed pivots (external range), CHOCH events (shift tag) and
closed candles. Everything is a pure fold — no clock, no randomness; replay
and live produce identical pools/levels/events (§0 rule 2).

Persistence is capability-only per the approved R1 ruling: row helpers feed
the existing P0.7 insert_level; nothing is wired in this phase. PWH/PWL and
running extremes are state-only (the frozen levels.kind vocabulary has no
PWH/PWL — D12.3, channels precedent).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import ChochEvent, Pivot
from marketscalper.providers.base import Candle

# Frozen §4.4/D12 literals — module constants, not config.
EQ_TOLERANCE_ATR_RATIO = 0.1           # cluster membership (strict <)
POOL_MIN_SIZE = 2
POOL_PIVOT_WINDOW = 20                 # D12.2, explicitly arbitrary (P5)
POOL_RECENCY_DECAY_BARS = 1440         # strength decay: one day of 1m bars
SWEEP_WICK_RATIO = 0.6                 # wick > 60% of candle range (strict)
SWEEP_SHIFT_WINDOW = 3                 # CHOCH within bars +1..+3 (D12.5)
# FLAGGED PLACEHOLDER (D12.4): the RVOL >= 1.5 OR-arm of sweep detection
# evaluates False until the Volume Engine lands (a True placeholder would
# make every wick-through a sweep). The swap re-runs determinism.
SWEEP_RVOL_PLACEHOLDER_PASSES = False

# D12.1 session map (A9): UTC hour -> session name.
SESSIONS = ("ASIA", "LONDON", "NY", "LATE")
_LEVEL_NAMES = ("PDH", "PDL", "PWH", "PWL",
                "ASIA_H", "ASIA_L", "LONDON_H", "LONDON_L",
                "NY_H", "NY_L", "LATE_H", "LATE_L")
_PERSISTABLE_KINDS = {"PDH": "PDH", "PDL": "PDL"} | {
    f"{s}_{side}": f"SESSION_{side}" for s in SESSIONS for side in ("H", "L")}


def session_of(hour: int) -> str:
    """D12.1: ASIA 00-08, LONDON 08-13, NY 13-21, LATE 21-00 (UTC)."""
    if hour < 8:
        return "ASIA"
    if hour < 13:
        return "LONDON"
    if hour < 21:
        return "NY"
    return "LATE"


@dataclass(frozen=True)
class LiquidityPool:
    """An EQH/EQL cluster (§4.4): 2+ confirmed pivots within tolerance."""

    kind: str                  # 'EQH' | 'EQL'
    price: float               # arithmetic mean of member prices (D12.2)
    size: int
    strength: float            # size / (1 + newest_age/1440), at recompute
    member_ts: tuple           # identity — the sweep latch key (D12.2)


@dataclass(frozen=True)
class SweepEvent:
    """A liquidity grab (§4.4): wick through, body rejected."""

    symbol: str
    ts: datetime               # sweeping candle identity
    bar_index: int
    side: str                  # 'HIGH' | 'LOW'
    target: str                # 'EQH'/'EQL' or a level name (D12.3)
    target_price: float


@dataclass(frozen=True)
class SweepShift:
    """Sweep + CHOCH within 3 candles = A+ reversal context (§4.4)."""

    sweep: SweepEvent
    choch_ts: datetime
    ts: datetime               # the tagging bar


def pool_to_row(pool: LiquidityPool, symbol: str, created_ts: datetime,
                tf: str = "1m") -> dict:
    """LiquidityPool -> db.insert_level kwargs (capability only, R1)."""
    return {"symbol": symbol, "tf": tf, "kind": pool.kind,
            "p1": pool.price, "p2": pool.price, "created_ts": created_ts}


def key_level_to_row(name: str, price: float, symbol: str,
                     created_ts: datetime, tf: str = "1m") -> dict:
    """Key level -> db.insert_level kwargs for the schema-covered kinds.
    PWH/PWL and running extremes are state-only (D12.3) — rejected loudly."""
    kind = _PERSISTABLE_KINDS.get(name)
    if kind is None:
        raise ValueError(f"level {name!r} is state-only (D12.3)")
    return {"symbol": symbol, "tf": tf, "kind": kind,
            "p1": price, "p2": price, "created_ts": created_ts}


class LiquidityEngine:
    """§4.4 for one symbol's 1m stream (cadence per D12.7)."""

    __slots__ = ("_symbol", "_atr", "_bar", "_pivots_h", "_pivots_l",
                 "_pools_dirty", "_pools", "_swept_pools", "_swept_levels",
                 "_levels", "_day", "_day_hi", "_day_lo", "_week_start",
                 "_week_hi", "_week_lo", "_session", "_sess_hi", "_sess_lo",
                 "_day_complete", "_week_complete", "_sess_complete",
                 "_pending_sweeps", "_choch_ts", "_ext_h", "_ext_l",
                 "_premium_discount")

    def __init__(self, symbol: str, atr: IncrementalATR) -> None:
        self._symbol = symbol
        self._atr = atr
        self._bar = -1
        self._pivots_h: deque = deque(maxlen=POOL_PIVOT_WINDOW)
        self._pivots_l: deque = deque(maxlen=POOL_PIVOT_WINDOW)
        self._pools_dirty = False
        self._pools: list[LiquidityPool] = []
        # sweep latches: (kind, frozenset(member ts)) — subset rule (a pool
        # whose members are a subset of a swept set is the same grabbed
        # liquidity, D12.2); pruned when members leave the pivot windows
        self._swept_pools: set[tuple] = set()
        self._swept_levels: set[tuple] = set()
        self._levels: dict[str, tuple] = {}      # name -> (price, period_key)
        self._day: date | None = None
        self._day_hi = self._day_lo = 0.0
        self._week_start: date | None = None
        self._week_hi = self._week_lo = 0.0
        self._session = ""
        self._sess_hi = self._sess_lo = 0.0
        # D7 doctrine (freeze-audit fix): a period not observed from its
        # exact boundary start is PARTIAL and must never be promoted.
        self._day_complete = False
        self._week_complete = False
        self._sess_complete = False
        self._pending_sweeps: list[tuple[SweepEvent, int]] = []
        self._choch_ts: datetime | None = None
        self._ext_h: float | None = None
        self._ext_l: float | None = None
        self._premium_discount: str | None = None

    # ------------------------------------------------------------ intakes

    def on_pivot(self, pivot: Pivot) -> None:
        """Confirmed labeled 1m pivot (pool input)."""
        window = self._pivots_h if pivot.kind == "H" else self._pivots_l
        window.append((self._bar + 1, pivot.ts, pivot.price))
        self._pools_dirty = True

    def on_external_pivot(self, pivot: Pivot) -> None:
        """Confirmed 5m pivot (A8 external range for premium/discount)."""
        if pivot.kind == "H":
            self._ext_h = pivot.price
        else:
            self._ext_l = pivot.price

    def on_choch(self, event: ChochEvent) -> None:
        """Same-bar CHOCH from the frozen structure chain (D12.7)."""
        self._choch_ts = event.ts

    # ----------------------------------------------------------- fold

    def update(self, candle: Candle) -> list:
        """Fold one closed 1m candle; returns SweepEvent/SweepShift list."""
        self._bar += 1
        cur = self._bar
        events: list = []

        self._roll_periods(candle)
        self._fold_extremes(candle)

        if self._pools_dirty and self._atr.value is not None:
            self._recompute_pools(cur)
            self._pools_dirty = False

        # sweep + shift resolution (D12.5): pending windows first
        if self._choch_ts is not None:
            still = []
            for sweep, window_end in self._pending_sweeps:
                if sweep.bar_index < cur <= window_end:
                    events.append(SweepShift(sweep, self._choch_ts, candle.ts))
                else:
                    still.append((sweep, window_end))
            self._pending_sweeps = still
        self._pending_sweeps = [(s, w) for s, w in self._pending_sweeps
                                if w > cur]
        self._choch_ts = None

        # sweep detection (D12.4) against pools then levels, fixed order
        for pool in self._pools:
            if self._pool_swept(pool):
                continue
            side = "HIGH" if pool.kind == "EQH" else "LOW"
            if self._is_sweep(candle, pool.price, side):
                self._swept_pools.add(
                    (pool.kind, frozenset(pool.member_ts)))
                events.append(self._sweep(candle, cur, side, pool.kind,
                                          pool.price))
        for name in _LEVEL_NAMES:
            entry = self._levels.get(name)
            if entry is None:
                continue
            price, period_key = entry
            if (name, period_key) in self._swept_levels:
                continue
            side = "HIGH" if name.endswith("H") else "LOW"
            if self._is_sweep(candle, price, side):
                self._swept_levels.add((name, period_key))
                events.append(self._sweep(candle, cur, side, name, price))
        for event in events:
            if isinstance(event, SweepEvent):
                self._pending_sweeps.append(
                    (event, cur + SWEEP_SHIFT_WINDOW))

        # premium / discount (D12.6)
        if self._ext_h is not None and self._ext_l is not None:
            mid = (self._ext_h + self._ext_l) / 2.0
            self._premium_discount = ("premium" if candle.c > mid
                                      else "discount")
        else:
            self._premium_discount = None
        return events

    # -------------------------------------------------------- accessors

    def _pool_swept(self, pool: LiquidityPool) -> bool:
        """Latched iff this pool's members are a subset of a same-kind swept
        set: shrinking a grabbed pool is not fresh liquidity; gaining a new
        member is (D12.2)."""
        members = frozenset(pool.member_ts)
        return any(kind == pool.kind and members <= swept
                   for kind, swept in self._swept_pools)

    @property
    def pools(self) -> list[LiquidityPool]:
        """Active (unswept) pools, recompute order (newest anchor first)."""
        return [p for p in self._pools if not self._pool_swept(p)]

    @property
    def key_levels(self) -> dict[str, float]:
        """Completed-period levels by name (D12.3)."""
        return {name: entry[0] for name, entry in self._levels.items()}

    @property
    def running_extremes(self) -> dict[str, float]:
        """Current-period extremes — state only, never sweep targets."""
        if self._day is None:
            return {}
        return {"DAY_H": self._day_hi, "DAY_L": self._day_lo,
                "WEEK_H": self._week_hi, "WEEK_L": self._week_lo,
                "SESSION_H": self._sess_hi, "SESSION_L": self._sess_lo}

    @property
    def premium_discount(self) -> str | None:
        return self._premium_discount

    # -------------------------------------------------------- internals

    def _roll_periods(self, candle: Candle) -> None:
        ts = candle.ts
        day = ts.date()
        week_start = day - timedelta(days=day.weekday())
        session = session_of(ts.hour)
        at_day_start = ts.hour == 0 and ts.minute == 0
        at_session_start = ts.minute == 0 and ts.hour in (0, 8, 13, 21)
        if self._day is None:                        # stream start
            self._day, self._week_start = day, week_start
            self._session = session
            self._day_hi = self._week_hi = self._sess_hi = candle.h
            self._day_lo = self._week_lo = self._sess_lo = candle.l
            self._day_complete = at_day_start
            self._week_complete = at_day_start and day == week_start
            self._sess_complete = at_session_start
            return
        if day != self._day:
            if self._day_complete:                   # D7: partial never promotes
                self._promote("PDH", self._day_hi, self._day)
                self._promote("PDL", self._day_lo, self._day)
            self._complete_session()
            if week_start != self._week_start:
                if self._week_complete:
                    self._promote("PWH", self._week_hi, self._week_start)
                    self._promote("PWL", self._week_lo, self._week_start)
                self._week_start = week_start
                self._week_hi, self._week_lo = candle.h, candle.l
                self._week_complete = at_day_start and day == week_start
            self._day = day
            self._day_hi, self._day_lo = candle.h, candle.l
            self._day_complete = at_day_start
            self._session = session
            self._sess_hi, self._sess_lo = candle.h, candle.l
            self._sess_complete = at_session_start
        elif session != self._session:
            self._complete_session()
            self._session = session
            self._sess_hi, self._sess_lo = candle.h, candle.l
            self._sess_complete = at_session_start

    def _promote(self, name: str, price: float, period_key) -> None:
        """Install a completed-period level; superseded latch entries for
        this name are pruned (bounded latch set, freeze-audit fix)."""
        self._levels[name] = (price, period_key)
        self._swept_levels = {(n, k) for n, k in self._swept_levels
                              if n != name}

    def _complete_session(self) -> None:
        if not self._sess_complete:                  # D7: partial session
            return
        key = (self._day, self._session)
        self._promote(f"{self._session}_H", self._sess_hi, key)
        self._promote(f"{self._session}_L", self._sess_lo, key)

    def _fold_extremes(self, candle: Candle) -> None:
        self._day_hi = max(self._day_hi, candle.h)
        self._day_lo = min(self._day_lo, candle.l)
        self._week_hi = max(self._week_hi, candle.h)
        self._week_lo = min(self._week_lo, candle.l)
        self._sess_hi = max(self._sess_hi, candle.h)
        self._sess_lo = min(self._sess_lo, candle.l)

    def _recompute_pools(self, cur: int) -> None:
        tol = EQ_TOLERANCE_ATR_RATIO * self._atr.value
        pools: list[LiquidityPool] = []
        for kind, window in (("EQH", self._pivots_h), ("EQL", self._pivots_l)):
            entries = list(window)                   # oldest -> newest
            claimed = [False] * len(entries)
            for i in range(len(entries) - 1, -1, -1):    # newest first
                if claimed[i]:
                    continue
                anchor_index, _anchor_ts, anchor_price = entries[i]
                members = []
                for j in range(len(entries) - 1, -1, -1):
                    if claimed[j]:
                        continue
                    if abs(entries[j][2] - anchor_price) < tol:  # strict
                        members.append(j)
                if len(members) >= POOL_MIN_SIZE:
                    for j in members:
                        claimed[j] = True
                    prices = [entries[j][2] for j in members]
                    newest_index = max(entries[j][0] for j in members)
                    pools.append(LiquidityPool(
                        kind,
                        sum(prices) / len(prices),
                        len(members),
                        len(members) / (1 + (cur - newest_index)
                                        / POOL_RECENCY_DECAY_BARS),
                        tuple(sorted(entries[j][1] for j in members)),
                    ))
        self._pools = pools
        # prune latches that can never match again: a swept set with no
        # member left in its kind's window has no possible future subset
        window_ts = {"EQH": {ts for _i, ts, _p in self._pivots_h},
                     "EQL": {ts for _i, ts, _p in self._pivots_l}}
        self._swept_pools = {(kind, swept) for kind, swept in self._swept_pools
                             if swept & window_ts[kind]}

    @staticmethod
    def _is_sweep(candle: Candle, price: float, side: str) -> bool:
        rng = candle.h - candle.l
        if rng <= 0:
            return False                             # zero-range never sweeps
        if side == "HIGH":
            if not (candle.h > price and candle.c < price):
                return False
            wick = candle.h - max(candle.o, candle.c)
        else:
            if not (candle.l < price and candle.c > price):
                return False
            wick = min(candle.o, candle.c) - candle.l
        return (SWEEP_RVOL_PLACEHOLDER_PASSES
                or wick > SWEEP_WICK_RATIO * rng)    # strict (D12.4)

    def _sweep(self, candle: Candle, cur: int, side: str, target: str,
               price: float) -> SweepEvent:
        return SweepEvent(self._symbol, candle.ts, cur, side, target, price)

"""Order Block Engine — COMPLETE and FROZEN (engine-wise freeze after the
D13 conformance audit; Architecture §4.5; Decision D13 incl. its
freeze-audit addenda; roadmap P2.14, P2.15, P2.17). Modify only on a
genuine production defect.

Detection: on a displacement BOS (qualified by the frozen BosDetector —
never re-computed here), the last opposite-color candle before the BOS bar
becomes the order block; zone [low, open] for bullish OBs, [open, high]
for bearish (§4.5 verbatim). Lifecycle per closed candle: active →
mitigated (first range overlap) → broken (close strictly through the far
side; break precedes mitigation on the same bar). A broken non-breaker
spawns a breaker: same zone, flipped direction, one flip only (D13.3).

Pure consumer of frozen upstream outputs (closed candles + BosEvents);
pure fold — no clock, no randomness; replay and live produce identical
zones and events (§0 rule 2). Persistence is capability-only per R1:
OB_BULL/OB_BEAR rows via the existing P0.7 helpers; breakers are
state-only (D13.4).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from marketscalper.engines.structure import BosEvent
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# Frozen §4.5/D13 literals — module constants, not config.
OB_LOOKBACK_BARS = 20                  # source-candle scan bound (D13.1)
OB_TRACKED_PER_BUCKET = 10             # per (direction, breaker) (D13.4)


@dataclass
class OrderBlock:
    """One OB or breaker zone (mutable lifecycle: active→mitigated→broken)."""

    direction: str                 # 'BULL' | 'BEAR' (the zone's trade side)
    zone_lo: float
    zone_hi: float
    source_ts: datetime            # the opposite-color candle's identity
    created_index: int             # bar the zone was created (BOS/break bar)
    created_ts: datetime
    breaker: bool = False          # True = role-flipped broken OB (D13.3)
    status: str = "active"         # 'active' | 'mitigated' | 'broken'


def ob_to_row(block: OrderBlock, symbol: str, tf: str = "1m") -> dict:
    """OrderBlock -> db.insert_level kwargs (capability only, R1).
    Breakers are state-only (D13.4) — rejected loudly."""
    if block.breaker:
        raise ValueError("breakers are state-only (D13.4)")
    return {"symbol": symbol, "tf": tf,
            "kind": "OB_BULL" if block.direction == "BULL" else "OB_BEAR",
            "p1": block.zone_hi, "p2": block.zone_lo,   # §3: top / bottom
            "created_ts": block.created_ts}


class OrderBlockEngine:
    """§4.5 for one symbol's 1m stream (cadence per D13.5)."""

    __slots__ = ("_symbol", "_bar", "_history", "_pending_bos", "_blocks",
                 "_seen")

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._bar = -1
        self._history: deque = deque(maxlen=OB_LOOKBACK_BARS + 1)
        self._pending_bos: BosEvent | None = None
        self._blocks: list[OrderBlock] = []
        self._seen: set[tuple] = set()     # (direction, source_ts) identities

    # ------------------------------------------------------------ intakes

    def on_bos(self, event: BosEvent) -> None:
        """Same-bar BOS from the frozen structure chain (D13.5); only a
        displacement BOS (strictly True) can create an order block."""
        if event.displacement is True:
            self._pending_bos = event

    # ----------------------------------------------------------- fold

    def update(self, candle: Candle) -> list[OrderBlock]:
        """Fold one closed 1m candle; returns zones CREATED this bar."""
        self._bar += 1
        cur = self._bar
        created: list[OrderBlock] = []

        # 1. lifecycle of existing zones (never their own creation bar);
        #    break precedes mitigation (D13.2)
        for block in self._blocks:
            if block.status == "broken" or block.created_index == cur:
                continue
            bull = block.direction == "BULL"
            broken = (candle.c < block.zone_lo if bull
                      else candle.c > block.zone_hi)
            if broken:
                block.status = "broken"
                if not block.breaker:              # D13.3: one flip only
                    breaker = OrderBlock(
                        "BEAR" if bull else "BULL",
                        block.zone_lo, block.zone_hi, block.source_ts,
                        cur, candle.ts, breaker=True)
                    created.append(breaker)
                continue
            if (block.status == "active"
                    and candle.l <= block.zone_hi
                    and candle.h >= block.zone_lo):
                block.status = "mitigated"         # sticky (D13.2)
        # broken zones drop immediately: their breaker (if any) is spawned,
        # re-detection of their identity is provably impossible, and keeping
        # them would evict live zones from the D13.4 buckets (freeze-audit)
        self._blocks = [b for b in self._blocks if b.status != "broken"]

        # 2. detection on the pending displacement BOS (D13.1)
        self._history.append(candle)
        if self._pending_bos is not None:
            bos = self._pending_bos
            self._pending_bos = None
            # armor (freeze-audit): a latch that survived a mid-cadence
            # exception belongs to an earlier bar — never consume it late
            if bos.ts == candle.ts:
                block = self._detect(bos, cur, candle.ts)
                if block is not None:
                    created.append(block)

        # 3. register + per-bucket bound (D13.4)
        for block in created:
            self._blocks.append(block)
        if created:
            self._trim()
        return created

    # -------------------------------------------------------- accessors

    @property
    def blocks(self) -> list[OrderBlock]:
        """Unbroken order blocks (non-breaker), tracking order."""
        return [b for b in self._blocks
                if not b.breaker and b.status != "broken"]

    @property
    def breakers(self) -> list[OrderBlock]:
        """Unbroken breaker zones, tracking order."""
        return [b for b in self._blocks
                if b.breaker and b.status != "broken"]

    # -------------------------------------------------------- internals

    def _detect(self, bos: BosEvent, cur: int, ts) -> OrderBlock | None:
        bull = bos.direction == "UP"
        candles = list(self._history)
        # strictly before the BOS bar, newest first, bounded scan (D13.1)
        for candle in reversed(candles[:-1]):
            opposite = (candle.c < candle.o if bull else candle.c > candle.o)
            if not opposite:
                continue                            # dojis skipped too
            direction = "BULL" if bull else "BEAR"
            identity = (direction, candle.ts)
            if identity in self._seen:
                return None                         # duplicate (D13.1)
            self._seen.add(identity)
            if bull:
                zone_lo, zone_hi = candle.l, candle.o    # §4.5: [low, open]
            else:
                zone_lo, zone_hi = candle.o, candle.h    # §4.5: [open, high]
            return OrderBlock(direction, zone_lo, zone_hi, candle.ts,
                              cur, ts)
        return None

    def _trim(self) -> None:
        for direction in ("BULL", "BEAR"):
            for breaker in (False, True):
                bucket = [b for b in self._blocks
                          if b.direction == direction and b.breaker == breaker]
                for stale in bucket[:-OB_TRACKED_PER_BUCKET]:
                    self._blocks.remove(stale)
        # identity set stays bounded: drop identities no longer tracked
        # (safe — the freeze audit proved a pruned identity cannot recur)
        self._seen &= {(b.direction, b.source_ts) for b in self._blocks}

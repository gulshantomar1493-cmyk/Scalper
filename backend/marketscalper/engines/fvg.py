"""Fair Value Gap Engine — COMPLETE and FROZEN (engine-wise freeze after the
D14 conformance audit; Architecture §4.5; Decision D14 incl. its
freeze-audit addenda; roadmap P2.16). Modify only on a genuine production
defect.

3-candle imbalances: bullish iff candle1.high < candle3.low (gap
[c1.high, c3.low]); bearish iff candle1.low > candle3.high (gap
[c3.high, c1.low]) — §4.5 verbatim, with the 0.3×ATR minimum-size noise
filter (inclusive boundary, D14.1). Lifecycle per closed candle (never the
creation bar): 50% fill = "CE tested" (sticky), full fill = filled and
archived (dropped from tracking). No invalidation state exists in §4.5.

Pure consumer of closed candles + the frozen ATR; pure fold — no clock,
no randomness; replay and live produce identical gaps (§0 rule 2).
Persistence capability-only per R1 (FVG_BULL/FVG_BEAR rows via the
existing P0.7 helpers; ce_tested/filled are documented app-layer status
vocabulary additions).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# Frozen §4.5/D14 literals — module constants, not config.
FVG_MIN_GAP_ATR_RATIO = 0.3            # inclusive minimum size (D14.1)
FVG_TRACKED_PER_DIRECTION = 10         # unfilled gaps kept (D14.3)


@dataclass
class FairValueGap:
    """One FVG (mutable lifecycle: active -> ce_tested -> filled)."""

    direction: str                 # 'BULL' | 'BEAR'
    lo: float
    hi: float
    created_index: int             # the candle-3 bar
    created_ts: datetime
    status: str = "active"         # 'active' | 'ce_tested' | 'filled'

    @property
    def ce(self) -> float:
        """Consequent encroachment — the 50% level (§4.5)."""
        return (self.lo + self.hi) / 2.0


def fvg_to_row(gap: FairValueGap, symbol: str, tf: str = "1m") -> dict:
    """FairValueGap -> db.insert_level kwargs (capability only, R1)."""
    return {"symbol": symbol, "tf": tf,
            "kind": "FVG_BULL" if gap.direction == "BULL" else "FVG_BEAR",
            "p1": gap.hi, "p2": gap.lo,             # §3: top / bottom
            "created_ts": gap.created_ts}


class FvgEngine:
    """§4.5 FVG detection + fill tracking for one symbol's 1m stream."""

    __slots__ = ("_symbol", "_atr", "_bar", "_window", "_gaps")

    def __init__(self, symbol: str, atr: IncrementalATR) -> None:
        self._symbol = symbol
        self._atr = atr
        self._bar = -1
        self._window: deque = deque(maxlen=3)
        self._gaps: list[FairValueGap] = []

    def update(self, candle: Candle) -> list[FairValueGap]:
        """Fold one closed 1m candle; returns gaps CREATED this bar."""
        self._bar += 1
        cur = self._bar

        # 1. fill tracking on existing gaps (never their creation bar);
        #    full fill checked before the CE test (D14.2)
        for gap in self._gaps:
            if gap.direction == "BULL":
                if candle.l <= gap.lo:
                    gap.status = "filled"
                elif gap.status == "active" and candle.l <= gap.ce:
                    gap.status = "ce_tested"        # sticky
            else:
                if candle.h >= gap.hi:
                    gap.status = "filled"
                elif gap.status == "active" and candle.h >= gap.ce:
                    gap.status = "ce_tested"
        # filled = archived: dropped from tracking (§4.5; OB precedent)
        self._gaps = [g for g in self._gaps if g.status != "filled"]

        # 2. detection with the candle as candle-3 (D14.1)
        self._window.append(candle)
        created: list[FairValueGap] = []
        atr = self._atr.value
        if len(self._window) == 3 and atr is not None:
            c1, _c2, c3 = self._window
            gap = None
            if c1.h < c3.l:                        # bullish imbalance
                gap = FairValueGap("BULL", c1.h, c3.l, cur, candle.ts)
            elif c1.l > c3.h:                      # bearish imbalance
                gap = FairValueGap("BEAR", c3.h, c1.l, cur, candle.ts)
            if (gap is not None
                    and gap.hi - gap.lo >= FVG_MIN_GAP_ATR_RATIO * atr):
                created.append(gap)
                self._gaps.append(gap)
                self._trim()
        return created

    @property
    def gaps(self) -> list[FairValueGap]:
        """Unfilled gaps, creation order."""
        return list(self._gaps)

    def _trim(self) -> None:
        for direction in ("BULL", "BEAR"):
            bucket = [g for g in self._gaps if g.direction == direction]
            for stale in bucket[:-FVG_TRACKED_PER_DIRECTION]:
                self._gaps.remove(stale)

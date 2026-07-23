"""V3 core domain objects — mirrors docs/V3/DOMAIN-MODEL.md 1:1.

Every object: id · symbol · tf · created_at · state · history[] (append-only
(ts, event, reason)). Every state transition MUST go through `_transition`,
which records the reason — no silent mutation. Objects fold closed candles;
they never look at the wall clock and never mutate each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- enums (plain strings — stable, JSON-friendly) -----------------------

HIGH, LOW = "HIGH", "LOW"
BULLISH, BEARISH, RANGE = "BULLISH", "BEARISH", "RANGE"

# zone kinds
SR, DEMAND, SUPPLY, ORDER_BLOCK, FVG, TRENDLINE_ZONE = (
    "SR", "DEMAND", "SUPPLY", "ORDER_BLOCK", "FVG", "TRENDLINE")

# zone lifecycle
FRESH, TESTED, WEAK, BROKEN, RETIRED = "FRESH", "TESTED", "WEAK", "BROKEN", "RETIRED"

# trendline lifecycle
TL_NEW, TL_VALID, TL_STRONG, TL_WEAK, TL_BROKEN, TL_INVALID = (
    "NEW", "VALID", "STRONG", "WEAK", "BROKEN", "INVALID")

# liquidity
BUYSIDE, SELLSIDE = "BUYSIDE", "SELLSIDE"
UNSWEPT, SWEPT = "UNSWEPT", "SWEPT"


@dataclass
class DomainObject:
    id: str
    symbol: str
    tf: str
    created_at: int                    # bar ts (epoch s) that created it
    state: str
    history: list = field(default_factory=list)   # [(ts, event, reason)]

    def _log(self, ts: int, event: str, reason: str) -> None:
        self.history.append((ts, event, reason))

    def _transition(self, ts: int, new_state: str, reason: str) -> None:
        if new_state == self.state:
            return
        self._log(ts, f"{self.state}->{new_state}", reason)
        self.state = new_state


# ------------------------------------------------------------------ Swing

@dataclass
class Swing(DomainObject):
    kind: str = HIGH                   # HIGH | LOW
    price: float = 0.0
    ts: int = 0                        # bar ts of the extreme
    confirmed_ts: int = 0              # bar ts that confirmed it (ts + k bars)
    label: str | None = None           # HH | HL | LH | LL (None = first of kind)


# -------------------------------------------------------------- Structure

@dataclass
class StructureEvent:
    kind: str                          # BOS | CHOCH
    direction: str                     # UP | DOWN
    ts: int
    displaced: bool
    broken_swing_id: str


@dataclass
class Structure:
    """Per (symbol, tf) structure state — folds swings + closes."""
    trend: str = RANGE
    last_bos: StructureEvent | None = None
    last_choch: StructureEvent | None = None
    events: list = field(default_factory=list)     # recent StructureEvents


# -------------------------------------------------------------- Trendline

@dataclass
class Trendline(DomainObject):
    side: str = "SUPPORT"              # SUPPORT (lows) | RESISTANCE (highs)
    anchor_ids: tuple = ()             # (swing_id, swing_id)
    # geometry in log space over bar-index axis: log(p) = slope*idx + intercept
    slope: float = 0.0
    intercept: float = 0.0
    a_idx: int = 0                     # anchor bar indices
    b_idx: int = 0
    touches: list = field(default_factory=list)    # [(ts, price)]
    role_flipped: bool = False
    broken_at: int | None = None
    last_touch_idx: int = 0

    def price_at(self, idx: int) -> float:
        import math
        return math.exp(self.slope * idx + self.intercept)


# ------------------------------------------------------------------- Zone

@dataclass
class Zone(DomainObject):
    kind: str = SR                     # SR|DEMAND|SUPPLY|ORDER_BLOCK|FVG|TRENDLINE
    lo: float = 0.0
    hi: float = 0.0
    origin: str = ""                   # human-readable origin (swings / impulse / candle)
    touches: list = field(default_factory=list)    # [(ts, "HELD"|"PIERCED")]
    flipped_from: str | None = None    # zone id it role-flipped from
    invalidated_at: int | None = None

    @property
    def touch_count(self) -> int:
        return len(self.touches)

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


# ---------------------------------------------------------- LiquidityPool

@dataclass
class LiquidityPool(DomainObject):
    kind: str = "EQH"                  # PWH|PWL|PDH|PDL|EQH|EQL|SESSION_H|SESSION_L|...
    price: float = 0.0
    side: str = BUYSIDE                # BUYSIDE above price | SELLSIDE below
    priority: int = 1                  # ★1..5
    member_ids: tuple = ()             # swings forming the pool (EQH/EQL)
    swept_at: int | None = None
    post_sweep: str = "PENDING"        # PENDING | REVERSED | CONTINUED (later phases)
    session: str | None = None         # ASIA|LONDON|NY for session pools

"""StateStore — single source of truth per symbol (roadmap P0.20; §1, §9).

Holds exactly the per-symbol state the current pipeline produces: the latest
closed 1m candle and the latest closed 5m candle. Engine fields are added by
the engine phases that produce them (P1+), never in advance.

Subscribes to Candle events on the truth-only EventBus (P0.17 flow) — the
same self-subscription pattern as CandleBuilder and CandleWriter. diff()
yields, per symbol, only the fields changed since the previous diff() call —
the exact payload shape the P0.21 WebSocket push consumes ("renders diffs
only", §9). snapshot() serves full-state reads (e.g. a newly connected
client).

In-process plain Python objects per the frozen §2 hot-state row: no Redis,
no persistence, no locks — single asyncio process, sequential bus delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)

_FIELD_BY_TF = {"1m": "last_candle_1m", "5m": "last_candle_5m"}


@dataclass
class SymbolState:
    """Per-symbol state — exactly what the pipeline produces today."""

    last_candle_1m: Candle | None = None
    last_candle_5m: Candle | None = None


class StateStore:
    """Single source of truth per symbol + change-diff generation."""

    def __init__(self, bus: EventBus) -> None:
        self._states: dict[str, SymbolState] = {}
        self._pending: dict[str, dict[str, Candle]] = {}
        bus.subscribe(Candle, self.on_candle)

    async def on_candle(self, candle: Candle) -> None:
        """Record a closed candle in its symbol's state and in the pending diff."""
        field = _FIELD_BY_TF.get(candle.tf)
        if field is None:
            log.warning("state: ignoring candle with unknown tf %r", candle.tf)
            return
        state = self._states.setdefault(candle.symbol, SymbolState())
        setattr(state, field, candle)
        self._pending.setdefault(candle.symbol, {})[field] = candle

    def snapshot(self, symbol: str) -> SymbolState | None:
        """Full current state for one symbol (a copy), or None if never seen."""
        state = self._states.get(symbol)
        return None if state is None else replace(state)

    def diff(self) -> dict[str, dict[str, Candle]]:
        """Fields changed per symbol since the previous diff() call.

        Consecutive updates between calls collapse to the latest value;
        an empty dict means nothing changed."""
        pending, self._pending = self._pending, {}
        return pending

"""FeedProvider contract + normalized market-data types (roadmap P0.9).

This module is the provider boundary: the ONLY module in the providers
package that engines may import (pinned by the P0.19 CI import check).
Nothing past this layer is provider-shaped — no raw provider JSON reaches
engines; only the normalized types below travel on the EventBus.

Interface contracts only — required methods, return types, capability
flags, normalized event types. How a provider behaves internally
(reconnection, retries, timing, recovery) is defined by each provider's
own implementation task (P0.10, P0.24, P6.2), not here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


# ------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class Capabilities:
    """Capability flags every provider declares (Architecture §1)."""

    supports_live_data: bool
    supports_historical_data: bool
    supports_orderbook: bool
    supports_trades: bool


# ------------------------------------------------- normalized market data


@dataclass(frozen=True)
class Tick:
    """Bare price update (the §1 TICK event)."""

    symbol: str
    price: float
    ts: datetime  # UTC


@dataclass(frozen=True)
class Trade:
    """Executed trade — exactly the fields the candle builder consumes (§4.1)."""

    symbol: str
    price: float
    qty: float
    ts: datetime  # UTC
    is_buyer_maker: bool


@dataclass(frozen=True)
class BookTicker:
    """Best bid/ask snapshot (input to the spread gate, A10/G2)."""

    symbol: str
    bid_px: float
    bid_qty: float
    ask_px: float
    ask_qty: float
    ts: datetime  # UTC


@dataclass(frozen=True)
class Candle:
    """Normalized candle, mirroring the §3 candles schema.

    Return type of FeedProvider.fetch_historical_candles().
    """

    symbol: str
    tf: str  # '1m' | '5m'
    ts: datetime  # candle open time, UTC
    o: float
    h: float
    l: float
    c: float
    v: float  # base volume
    qv: float  # quote volume
    n_trades: int
    taker_buy_v: float


# ----------------------------------------------------------- the contract


class FeedProvider(ABC):
    """The single interface every feed provider implements.

    Implementations: BinanceFeed (P0.10), ReplayFeed (P0.24),
    DeltaFeed (P6.2, market data only).
    """

    #: Short provider name, e.g. "binance", "replay" (used in logs).
    name: str

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """The provider's declared capability flags."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """True while the provider's live stream is up."""

    @abstractmethod
    async def start(self) -> None:
        """Begin publishing normalized events to the EventBus."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop publishing and release resources."""

    @abstractmethod
    async def fetch_historical_candles(
        self, symbol: str, tf: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Return candles for [start, end), ordered by ts ascending."""

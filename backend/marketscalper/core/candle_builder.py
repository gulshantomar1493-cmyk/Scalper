"""Candle Builder (roadmap P0.12 + P0.13; Architecture §4.1).

One responsibility: consume normalized Trade events, aggregate them into
deterministic 1-minute candles, publish the existing normalized Candle
(tf='1m') on the EventBus when a bucket closes — and roll closed 1m candles
into epoch-aligned 5-minute candles (tf='5m'), published per the A2 boundary
rule: when the just-closed 1m bucket satisfies (bucket + 1) % 5 == 0.
The 5m fold is an internal call at the 1m-close point, NOT a bus
subscription — provider reference klines travel as the same Candle type and
must never be folded.

Rules (frozen):
  * A candle closes ONLY when the first trade of a later bucket arrives —
    §4.1: the only place candles close. No timers.
  * Open candles are never emitted; a closed candle never changes.
  * Every trade belongs to exactly one candle (its floor(ts/60s) bucket).
  * Out-of-order trades (bucket earlier than the open one) are dropped with
    a WARNING — they never mutate a closed candle.
  * Empty minutes during feed gaps produce no candles here; kline backfill
    (P0.15, Decision D5) fills those. No synthetic candles.
  * A 5m candle publishes ONLY when its window is complete: seeded at the
    window head and folded contiguously through all five minutes. Any
    incomplete window — entered mid-way, holed by a gap, or cut by the
    boundary — is discarded with a WARNING (D7: incomplete = false data;
    verified-defect fix, 2026-07-18).
  * The first bucket per symbol after process start is DISCARDED at rollover
    (owner decision at P0.28): observation may have begun mid-minute, and a
    partially observed candle is false data — correctness over completeness.
    The first published candle is the first fully observed one.

Per owner clarification at P0.12: the builder publishes the EXISTING Candle
dataclass — no wrapper event types, no parallel event hierarchies. Telling
built candles apart from a provider's reference klines belongs to the
implementation flow and the reconciliation task (P0.14), not to event classes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle, Trade

log = logging.getLogger(__name__)

_BUCKET_S = 60   # 1-minute buckets
_WINDOW = 5      # 1m buckets per 5m window (frozen 1m/5m design — not a parameter)


class _OpenCandle:
    """Mutable per-symbol aggregation state. Internal — never leaves the module."""

    __slots__ = ("bucket", "o", "h", "l", "c", "v", "qv", "n_trades", "taker_buy_v")

    def __init__(self, bucket: int, t: Trade) -> None:
        self.bucket = bucket
        self.o = self.h = self.l = self.c = t.price
        self.v = t.qty
        self.qv = t.qty * t.price
        self.n_trades = t.n_trades
        # §4.1: taker bought when the buyer was NOT the maker
        self.taker_buy_v = t.qty if not t.is_buyer_maker else 0.0

    def add(self, t: Trade) -> None:
        self.h = max(self.h, t.price)
        self.l = min(self.l, t.price)
        self.c = t.price
        self.v += t.qty
        self.qv += t.qty * t.price
        self.n_trades += t.n_trades
        if not t.is_buyer_maker:
            self.taker_buy_v += t.qty


class _Open5m:
    """Mutable per-symbol 5m aggregate of closed 1m candles. Internal only.

    `complete` (D7 fix): True iff the aggregate was seeded at the window
    head and every fold was the immediately next bucket — closed 1m
    candles arrive in strictly increasing bucket order, so contiguity
    from the head means all five minutes are present at the boundary."""

    __slots__ = ("window_start", "last_bucket", "complete",
                 "o", "h", "l", "c", "v", "qv", "n_trades", "taker_buy_v")

    def __init__(self, window_start: int, closed_bucket: int, c1: Candle) -> None:
        self.window_start = window_start
        self.last_bucket = closed_bucket
        self.complete = closed_bucket == window_start
        self.o = c1.o
        self.h = c1.h
        self.l = c1.l
        self.c = c1.c
        self.v = c1.v
        self.qv = c1.qv
        self.n_trades = c1.n_trades
        self.taker_buy_v = c1.taker_buy_v

    def fold(self, closed_bucket: int, c1: Candle) -> None:
        if closed_bucket != self.last_bucket + 1:
            self.complete = False              # hole inside the window
        self.last_bucket = closed_bucket
        self.h = max(self.h, c1.h)
        self.l = min(self.l, c1.l)
        self.c = c1.c
        self.v += c1.v
        self.qv += c1.qv
        self.n_trades += c1.n_trades
        self.taker_buy_v += c1.taker_buy_v


class CandleBuilder:
    """Normalized Trade events -> deterministic 1m + 5m Candle events (§4.1)."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._open: dict[str, _OpenCandle] = {}
        self._open_5m: dict[str, _Open5m] = {}
        # Symbols whose currently-open bucket is their first since process
        # start. Observation may have begun mid-minute, so that bucket is
        # discarded at rollover — correctness over completeness: the first
        # persisted candle is the first fully observed one (owner decision).
        self._startup: set[str] = set()
        bus.subscribe(Trade, self.on_trade)

    async def on_trade(self, trade: Trade) -> None:
        """Route one trade into its bucket; close/publish on bucket rollover."""
        bucket = int(trade.ts.timestamp() // _BUCKET_S)
        current = self._open.get(trade.symbol)

        if current is None:
            self._open[trade.symbol] = _OpenCandle(bucket, trade)
            self._startup.add(trade.symbol)
            return

        if bucket < current.bucket:
            log.warning(
                "candle_builder: dropped out-of-order trade %s ts=%s "
                "(open bucket starts %s)",
                trade.symbol, trade.ts, _bucket_start(current.bucket),
            )
            return

        if bucket > current.bucket:
            if trade.symbol in self._startup:
                self._startup.discard(trade.symbol)
                log.warning(
                    "candle_builder: discarding partial startup candle %s %s "
                    "(observation began mid-minute)",
                    trade.symbol, _bucket_start(current.bucket),
                )
                self._open[trade.symbol] = _OpenCandle(bucket, trade)
                return
            closed_1m = _to_candle(trade.symbol, current)
            await self._bus.publish(closed_1m)                      # 1m first,
            await self._roll_5m(trade.symbol, current.bucket, closed_1m)  # then 5m (§4.1)
            self._open[trade.symbol] = _OpenCandle(bucket, trade)
            return

        current.add(trade)

    async def _roll_5m(self, symbol: str, closed_bucket: int, closed_1m: Candle) -> None:
        """Fold a just-closed 1m candle into its 5m window; publish at the
        A2 boundary: (closed_bucket + 1) % 5 == 0, epoch-aligned."""
        window_start = closed_bucket - (closed_bucket % _WINDOW)
        agg = self._open_5m.get(symbol)

        if agg is not None and agg.window_start != window_start:
            # The previous window's boundary minute never closed (gap across
            # the boundary): an incomplete 5m candle is false data — discard.
            log.warning(
                "candle_builder: discarding partial 5m aggregate %s window %s "
                "(boundary minute never closed)",
                symbol, _bucket_start(agg.window_start),
            )
            agg = None

        if agg is None:
            agg = _Open5m(window_start, closed_bucket, closed_1m)
            self._open_5m[symbol] = agg
        else:
            agg.fold(closed_bucket, closed_1m)

        if (closed_bucket + 1) % _WINDOW == 0:  # A2 boundary rule
            if agg.complete:
                await self._bus.publish(_to_5m_candle(symbol, agg))
            else:
                # D7 (verified-defect fix): a window entered mid-way or
                # holed by a gap is false data — never published, never
                # persisted.
                log.warning(
                    "candle_builder: discarding incomplete 5m candle %s "
                    "window %s (missing minutes are false data, D7)",
                    symbol, _bucket_start(agg.window_start),
                )
            del self._open_5m[symbol]


def _bucket_start(bucket: int) -> datetime:
    return datetime.fromtimestamp(bucket * _BUCKET_S, tz=timezone.utc)


def _to_candle(symbol: str, oc: _OpenCandle) -> Candle:
    return Candle(
        symbol=symbol,
        tf="1m",
        ts=_bucket_start(oc.bucket),
        o=oc.o, h=oc.h, l=oc.l, c=oc.c,
        v=oc.v, qv=oc.qv,
        n_trades=oc.n_trades,
        taker_buy_v=oc.taker_buy_v,
    )


def _to_5m_candle(symbol: str, agg: _Open5m) -> Candle:
    return Candle(
        symbol=symbol,
        tf="5m",
        ts=_bucket_start(agg.window_start),
        o=agg.o, h=agg.h, l=agg.l, c=agg.c,
        v=agg.v, qv=agg.qv,
        n_trades=agg.n_trades,
        taker_buy_v=agg.taker_buy_v,
    )

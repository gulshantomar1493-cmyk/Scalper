"""Live forming-bar tracker (chart UX item 5/6) — DISPLAY-ONLY.

Binance/TradingView charts move the current (still-building) candle with every
trade. MarketScalper's decision engine deliberately works only on CLOSED candles
(no-repaint, Architecture §0), so the forming bar must never reach it. This
tracker is the display path: it subscribes to the same normalized Trade events
the CandleBuilder consumes and maintains the current 1-minute bar's running
OHLCV, publishing a throttled `FormingBar` event.

Isolation guarantees:
  * `FormingBar` is a NEW event type — no engine subscribes to it (engines take
    Candle / BookTicker only), so it can't influence structure/liquidity/signals.
  * The tracker is composed only in the LIVE main() — replay sessions and tests
    run on their own bus without it, and the determinism harness never sees a
    FormingBar. So V1-V4 stay byte-identical.
  * It publishes nothing to the DB and produces no canonical candle — the
    CandleBuilder remains the sole authority for closed candles.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from marketscalper.core import indicators as ind
from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle, Trade

log = logging.getLogger(__name__)

_BUCKET_S = 60  # 1-minute buckets, matching the CandleBuilder


@dataclass(frozen=True)
class FormingBar:
    """The current 1m bar's live state (display-only; never persisted)."""

    symbol: str
    ts: datetime          # bucket start (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float
    n_trades: int


class _Forming:
    __slots__ = ("bucket", "o", "h", "l", "c", "v", "n")

    def __init__(self, bucket: int, trade: Trade) -> None:
        self.bucket = bucket
        self.o = self.h = self.l = self.c = trade.price
        self.v = trade.qty
        self.n = trade.n_trades

    def add(self, trade: Trade) -> None:
        if trade.price > self.h:
            self.h = trade.price
        if trade.price < self.l:
            self.l = trade.price
        self.c = trade.price
        self.v += trade.qty
        self.n += trade.n_trades

    def to_event(self, symbol: str) -> FormingBar:
        return FormingBar(
            symbol=symbol,
            ts=datetime.fromtimestamp(self.bucket * _BUCKET_S, tz=timezone.utc),
            o=self.o, h=self.h, l=self.l, c=self.c, v=self.v, n_trades=self.n,
        )


class LiveBarTracker:
    """Trade -> throttled FormingBar. Publishes on the first trade of a new
    bucket (so a new bar appears at once) and at most every `min_interval_s`
    otherwise. Uses the event loop's monotonic clock for throttling — never
    wall-clock, and never runs in a replayed/determinism path anyway."""

    def __init__(self, bus: EventBus, *, min_interval_s: float = 0.15) -> None:
        self._bus = bus
        self._min = min_interval_s
        self._bar: dict[str, _Forming] = {}
        self._last_pub: dict[str, float] = {}
        bus.subscribe(Trade, self.on_trade)

    async def on_trade(self, trade: Trade) -> None:
        bucket = int(trade.ts.timestamp() // _BUCKET_S)
        cur = self._bar.get(trade.symbol)
        new_bucket = cur is None or bucket > cur.bucket
        if new_bucket:
            cur = _Forming(bucket, trade)
            self._bar[trade.symbol] = cur
        elif bucket < cur.bucket:
            return                                   # out-of-order trade: ignore
        else:
            cur.add(trade)

        now = asyncio.get_running_loop().time()
        if new_bucket or (now - self._last_pub.get(trade.symbol, 0.0)) >= self._min:
            self._last_pub[trade.symbol] = now
            await self._bus.publish(cur.to_event(trade.symbol))

    def current_price(self, symbol: str) -> float | None:
        """Latest live forming-bar close (last trade price) for `symbol`, or None
        if none seen yet. Paper V2 (B4) fills market orders against this so they
        execute at the live price, not the last closed 1m candle."""
        bar = self._bar.get(symbol)
        return bar.c if bar is not None else None


class LiveIndicatorTracker:
    """Display-only interim indicator values for the live forming stream
    (chart UX item 2; owner rule: backend computes, frontend renders).

    Maintains the DEFAULT indicators' incremental state per symbol on the 1m
    stream — EMA 20/50/200 + RSI 14 — seeded from history at composition and
    advanced on each CLOSED 1m candle. `interim(symbol, forming_close)` projects
    them onto the current forming close so the forming message can carry them.
    Live-only and NON-mutating for the engine: no engine subscribes to this, it
    publishes nothing, and it only reads the closed candles the engines produce.
    The defaults match the frontend's default indicator set; a user length
    change reflows the HISTORICAL line via /api/chart (the live last point uses
    these defaults — a single-user cosmetic detail on the final bar only)."""

    _EMA = (20, 50, 200)
    _RSI = 14

    def __init__(self, bus: EventBus, symbols, seed_candles=None) -> None:
        self._st: dict[str, dict] = {}
        for sym in symbols:
            st = {"ema": {p: ind.EmaState(p) for p in self._EMA},
                  "rsi": ind.RsiState(self._RSI)}
            closes = [c.c for c in (seed_candles or {}).get(sym, [])]
            if closes:
                for p in self._EMA:
                    st["ema"][p].seed(closes)
                st["rsi"].seed(closes)
            self._st[sym] = st
        bus.subscribe(Candle, self._on_candle)

    async def _on_candle(self, candle: Candle) -> None:
        if candle.tf != "1m":
            return
        st = self._st.get(candle.symbol)
        if st is None:
            return
        for p in self._EMA:
            st["ema"][p].update(candle.c)
        st["rsi"].update(candle.c)

    def interim(self, symbol: str, forming_close: float):
        """Indicator values as if the forming close were the next close — for
        the live stream. Returns a dict (only warm indicators) or None."""
        st = self._st.get(symbol)
        if st is None:
            return None
        out: dict = {}
        for p in self._EMA:
            v = st["ema"][p].peek(forming_close)
            if v is not None:
                out[f"ema{p}"] = v
        r = st["rsi"].peek(forming_close)
        if r is not None:
            out["rsi"] = r
        return out or None

"""ReplayFeed — historical replay provider (roadmap P0.24; Architecture §10).

Implements the SAME FeedProvider interface as live providers: reads stored
1m candles from PostgreSQL and publishes them as the identical normalized
Candle events at speed ×{1, 10, 60, max} — the pipeline cannot tell replay
from live. Replay exists for learning, validation, testing and strategy
improvement only: no replay broker, no replay execution (frozen v1.2).

Fully self-contained by owner direction: the internal 5m fold below exists
ONLY to satisfy P0.24's "identical CANDLE_CLOSE events" (the live bus
carries both 1m and 5m closes) and must NOT become a shared component. It
applies the same frozen rules as the live builder — epoch-aligned windows,
the A2 boundary publish ((closed bucket + 1) % 5 == 0), and the D7
completeness rule (verified-defect fix, 2026-07-18): a 5m candle publishes
only when its window was seeded at the head and folded contiguously
through all five minutes; any incomplete window is discarded with a
WARNING. Equivalence with the live path is enforced permanently by the
pipeline-identity test in the suite.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from marketscalper import db
from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle, Capabilities, FeedProvider

log = logging.getLogger(__name__)

SPEEDS = (1, 10, 60, "max")  # §10: speed × {1, 10, 60, max}
_BUCKET_S = 60
_WINDOW = 5


def delay_for_speed(speed) -> float:
    """Pure pacing rule: seconds between candle emissions.

    ×1 = real-time cadence (60s per 1m candle), ×10 = 6s, ×60 = 1s,
    'max' = no sleep. Pacing never affects event order."""
    if speed == "max":
        return 0.0
    return _BUCKET_S / float(speed)


def _row_to_candle(r) -> Candle:
    return Candle(
        symbol=r["symbol"], tf=r["tf"], ts=r["ts"],
        o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
        v=float(r["v"]), qv=float(r["qv"]),
        n_trades=r["n_trades"], taker_buy_v=float(r["taker_buy_v"]),
    )


class ReplayFeed(FeedProvider):
    """Just another FeedProvider: Postgres candles -> normalized bus events."""

    name = "replay"

    def __init__(
        self,
        symbols,
        bus: EventBus,
        pool,
        start: datetime,
        end: datetime,
        speed=1,
    ) -> None:
        if speed not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS}, got {speed!r}")
        self._symbols = list(symbols)
        self._bus = bus
        self._pool = pool
        self._range = (start, end)
        self._delay = delay_for_speed(speed)
        self._connected = False
        self._task: asyncio.Task | None = None
        self._agg: dict[str, dict] = {}  # per-symbol open 5m window (internal only)

    # ------------------------------------------------- interface: contract

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_live_data=True,        # it streams events
            supports_historical_data=True,  # served from Postgres
            supports_orderbook=False,       # pinned by the roadmap
            supports_trades=False,          # candles, not trades
        )

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Begin publishing the stored range as normalized events."""
        if self._task is not None:
            raise RuntimeError("ReplayFeed already started")
        self._task = asyncio.create_task(self._run(), name="replay-feed")

    async def stop(self) -> None:
        """Halt the replay (mid-stream or after completion)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._connected = False

    async def fetch_historical_candles(
        self, symbol: str, tf: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Same [start, end) ascending contract, served from Postgres."""
        async with self._pool.acquire() as conn:
            rows = await db.select_candles(conn, symbol, tf, start, end)
        return [_row_to_candle(r) for r in rows]

    # ------------------------------------------------------------ internal

    async def _run(self) -> None:
        # Lifecycle visibility (approved at P0.25): connected means "the
        # ReplayFeed task is active" — True from task start (before the
        # candle load), False when replay finishes or is stopped.
        self._connected = True
        start, end = self._range
        stream: list[Candle] = []
        async with self._pool.acquire() as conn:
            for symbol in self._symbols:
                rows = await db.select_candles(conn, symbol, "1m", start, end)
                stream.extend(_row_to_candle(r) for r in rows)
        stream.sort(key=lambda c: (c.ts, c.symbol))  # deterministic interleave

        log.info(
            "replay: %d candles, %d symbols, [%s, %s), delay %.1fs/candle",
            len(stream), len(self._symbols), start, end, self._delay,
        )
        try:
            for candle in stream:
                await self._bus.publish(candle)      # 1m first, then roll (§4.1 order)
                await self._roll_5m(candle)
                if self._delay:
                    await asyncio.sleep(self._delay)
            log.info("replay: complete (%d candles)", len(stream))
        finally:
            self._connected = False                  # exhausted or cancelled

    async def _roll_5m(self, c1: Candle) -> None:
        """Self-contained 5m fold (P0.24-only; see module docstring)."""
        bucket = int(c1.ts.timestamp() // _BUCKET_S)
        window_start = bucket - (bucket % _WINDOW)
        agg = self._agg.get(c1.symbol)

        if agg is not None and agg["window_start"] != window_start:
            log.warning(
                "replay: discarding partial 5m aggregate %s window %s "
                "(boundary minute never closed)",
                c1.symbol, agg["ts"],
            )
            agg = None

        if agg is None:
            agg = {
                "window_start": window_start,
                "ts": datetime.fromtimestamp(window_start * _BUCKET_S, tz=timezone.utc),
                "last_bucket": bucket,
                "complete": bucket == window_start,   # D7 fix: head-seeded
                "o": c1.o, "h": c1.h, "l": c1.l, "c": c1.c,
                "v": c1.v, "qv": c1.qv,
                "n_trades": c1.n_trades, "taker_buy_v": c1.taker_buy_v,
            }
            self._agg[c1.symbol] = agg
        else:
            if bucket != agg["last_bucket"] + 1:
                agg["complete"] = False              # hole inside the window
            agg["last_bucket"] = bucket
            agg["h"] = max(agg["h"], c1.h)
            agg["l"] = min(agg["l"], c1.l)
            agg["c"] = c1.c
            agg["v"] += c1.v
            agg["qv"] += c1.qv
            agg["n_trades"] += c1.n_trades
            agg["taker_buy_v"] += c1.taker_buy_v

        if (bucket + 1) % _WINDOW == 0:              # A2 boundary rule
            if agg["complete"]:
                await self._bus.publish(Candle(
                    symbol=c1.symbol, tf="5m", ts=agg["ts"],
                    o=agg["o"], h=agg["h"], l=agg["l"], c=agg["c"],
                    v=agg["v"], qv=agg["qv"],
                    n_trades=agg["n_trades"], taker_buy_v=agg["taker_buy_v"],
                ))
            else:
                log.warning(
                    "replay: discarding incomplete 5m candle %s window %s "
                    "(missing minutes are false data, D7)",
                    c1.symbol, agg["ts"],
                )
            del self._agg[c1.symbol]

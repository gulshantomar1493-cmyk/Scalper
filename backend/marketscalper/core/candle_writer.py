"""Candle persistence writer (roadmap P0.17).

Subscribes to Candle events on the EventBus and inserts each one
immediately into the candles table via db.insert_candles().

Persistence rule (Decision D5, composition flow): the bus carries ONLY
truth candles — CandleBuilder closes (1m + 5m) and gap-backfill candles.
Live reference klines never reach the bus; BinanceFeed routes them to
reconciler.on_reference() through its explicit callback. Therefore the
writer persists everything it sees, with no source-sniffing.

Per owner direction: no buffering, no timer batching, no queues, no
workers — one immediate insert per candle event, deterministic.

Error handling: insert failures are caught HERE — logged with the candle
key, counted in write_errors — and the pipeline stays alive. Letting the
exception propagate would travel up the bus into the feed's read loop and
crash-loop the reconnect. db.py itself stays policy-free.
"""

from __future__ import annotations

import logging

from marketscalper import db
from marketscalper.bootstrap import candle_to_row
from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)


class CandleWriter:
    """Bus Candle events -> immediate inserts into the candles table.

    Counters (for ops / the P0.28 gate):
      rows_written — successful inserts.
      write_errors — failed inserts (logged and skipped; pipeline continues).
    """

    def __init__(self, bus: EventBus, pool) -> None:
        self._pool = pool
        self.rows_written = 0
        self.write_errors = 0
        bus.subscribe(Candle, self.on_candle)

    async def on_candle(self, candle: Candle) -> None:
        """Persist one closed candle; never lets a DB error escape."""
        try:
            async with self._pool.acquire() as conn:
                await db.insert_candles(conn, [candle_to_row(candle)])
        except Exception as exc:
            self.write_errors += 1
            log.error(
                "candle_writer: insert failed for %s %s %s: %s",
                candle.symbol, candle.tf, candle.ts, exc,
            )
            return
        self.rows_written += 1

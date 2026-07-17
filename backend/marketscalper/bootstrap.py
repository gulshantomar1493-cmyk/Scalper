"""One-time bootstrap job (roadmap P0.16; Decision A3).

Loads the initial 1m candle history (>= 90 days by default) into PostgreSQL:

    FeedProvider -> fetch_historical_candles() -> validate minimum history
    -> ensure partitions -> insert candles -> ready for replay/live pipeline

Provider-blind by construction: this module never imports a concrete
provider and touches exactly two members of the interface —
capabilities.supports_historical_data and fetch_historical_candles().
ReplayFeed/DeltaFeed plug in without changing bootstrap.

Safety (A3): if any symbol's returned history covers fewer than min_days,
a clear error is raised and NOTHING is inserted — validation of every
symbol completes before the first insert (two-phase), so invalid bootstrap
data is never partially written.

Transaction ownership stays with the caller (db.py philosophy). Duplicate
handling is NOT added here: db.insert_candles() is a plain INSERT, so
re-running bootstrap over already-loaded data raises UniqueViolationError.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from marketscalper import db
from marketscalper.providers.base import Candle, FeedProvider

log = logging.getLogger(__name__)

TARGET_DAYS = 90  # A3: bulk-load at least 90 days
MIN_DAYS = 20     # A3: refuse to run below 20 days of coverage


def candle_to_row(c: Candle) -> tuple:
    """Normalized Candle -> row tuple in db.insert_candles column order."""
    return (c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
            c.n_trades, c.taker_buy_v)


def _months_spanned(start: datetime, end: datetime) -> int:
    """Whole month steps from start's month to end's month (for D2's helper)."""
    return (end.year * 12 + end.month) - (start.year * 12 + start.month)


async def bootstrap_history(
    provider: FeedProvider,
    conn,
    symbols,
    target_days: int = TARGET_DAYS,
    min_days: int = MIN_DAYS,
) -> dict[str, int]:
    """Fetch and persist initial 1m history for every symbol.

    Returns {symbol: inserted_row_count}. Raises RuntimeError (before any
    insert) when the provider cannot serve history or any symbol's coverage
    is below min_days.
    """
    if not provider.capabilities.supports_historical_data:
        raise RuntimeError(
            f"bootstrap refused: provider {provider.name!r} does not support "
            "historical data (supports_historical_data=False)"
        )

    end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=target_days)

    # Partitions exist before any insert (D2: same helper, historical range).
    await db.ensure_partitions(conn, start, _months_spanned(start, end))

    # Phase 1 — fetch + validate EVERY symbol before inserting anything.
    fetched: dict[str, list[Candle]] = {}
    for symbol in symbols:
        candles = await provider.fetch_historical_candles(symbol, "1m", start, end)
        if not candles:
            raise RuntimeError(
                f"bootstrap refused: {symbol} returned no history — nothing inserted"
            )
        coverage = end - candles[0].ts
        if coverage < timedelta(days=min_days):
            raise RuntimeError(
                f"bootstrap refused: {symbol} history covers only "
                f"{coverage.total_seconds() / 86400:.1f} days "
                f"(minimum {min_days}) — nothing inserted"
            )
        fetched[symbol] = candles

    # Phase 2 — persist, ascending order as returned by the provider contract.
    inserted: dict[str, int] = {}
    for symbol, candles in fetched.items():
        await db.insert_candles(conn, [candle_to_row(c) for c in candles])
        inserted[symbol] = len(candles)
        log.info(
            "bootstrap: %s — %d candles [%s .. %s]",
            symbol, len(candles), candles[0].ts, candles[-1].ts,
        )
    return inserted

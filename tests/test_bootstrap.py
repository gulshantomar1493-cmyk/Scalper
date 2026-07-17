"""Tests for the bootstrap job (roadmap P0.16; Decision A3).

Uses a hand-written in-test FeedProvider (a real implementation of the ABC,
not a mock) and the existing dev-database rollback fixtures.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

import marketscalper.bootstrap as bootstrap_module
from marketscalper.bootstrap import bootstrap_history, candle_to_row
from marketscalper.providers.base import Candle, Capabilities, FeedProvider

UTC = timezone.utc


class FakeHistoryProvider(FeedProvider):
    """Minimal provider serving pre-configured history; records every call."""

    name = "fake-history"

    def __init__(self, history: dict[str, list[Candle]], historical: bool = True):
        self._history = history
        self._historical = historical
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            supports_live_data=False,
            supports_historical_data=self._historical,
            supports_orderbook=False,
            supports_trades=False,
        )

    @property
    def connected(self) -> bool:
        return False

    async def start(self) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    async def fetch_historical_candles(self, symbol, tf, start, end):
        self.calls.append((symbol, tf, start, end))
        return [c for c in self._history.get(symbol, ()) if start <= c.ts < end]


def _candle(symbol: str, ts: datetime, o: float = 100.0) -> Candle:
    return Candle(symbol=symbol, tf="1m", ts=ts, o=o, h=o + 1, l=o - 1,
                  c=o + 0.5, v=1.0, qv=o, n_trades=3, taker_buy_v=0.4)


def _sparse_history(symbol: str, days: int, end: datetime) -> list[Candle]:
    """One candle per day covering `days` days back from end — small and
    legitimate (coverage is measured, not density)."""
    first = end - timedelta(days=days)
    return [_candle(symbol, first + timedelta(days=i), o=100 + i) for i in range(days)]


def _now_floor() -> datetime:
    return datetime.now(tz=UTC).replace(second=0, microsecond=0)


# --------------------------------------------------------- provider blindness


def test_bootstrap_module_never_references_binance():
    src = inspect.getsource(bootstrap_module).lower()
    assert "binance" not in src


async def test_provider_without_historical_capability_refused_before_fetch():
    provider = FakeHistoryProvider({}, historical=False)
    with pytest.raises(RuntimeError, match="does not support historical"):
        await bootstrap_history(provider, conn=None, symbols=["BTCUSDT"])
    assert provider.calls == []  # refused before any fetch (and before any DB use)


# ------------------------------------------------------------ request contract


async def test_requests_1m_over_target_range(db_conn):
    before = _now_floor()
    provider = FakeHistoryProvider(
        {"BTCUSDT": _sparse_history("BTCUSDT", 90, before)})
    await bootstrap_history(provider, db_conn, ["BTCUSDT"])
    after = datetime.now(tz=UTC).replace(second=0, microsecond=0)

    (symbol, tf, start, end), = provider.calls
    assert symbol == "BTCUSDT" and tf == "1m"
    assert before <= end <= after                    # end = floor(now) to the minute
    assert end.second == 0 and end.microsecond == 0
    assert end - start == timedelta(days=90)         # start = end - target_days


# ---------------------------------------------------------------- safety guard


async def test_insufficient_history_raises_and_inserts_nothing(db_conn):
    end = _now_floor()
    provider = FakeHistoryProvider(
        {"BTCUSDT": _sparse_history("BTCUSDT", 5, end)})   # only 5 days
    with pytest.raises(RuntimeError, match="covers only"):
        await bootstrap_history(provider, db_conn, ["BTCUSDT"])
    count = await db_conn.fetchval("SELECT count(*) FROM candles")
    assert count == 0


async def test_empty_history_raises_and_inserts_nothing(db_conn):
    provider = FakeHistoryProvider({"BTCUSDT": []})
    with pytest.raises(RuntimeError, match="no history"):
        await bootstrap_history(provider, db_conn, ["BTCUSDT"])
    assert await db_conn.fetchval("SELECT count(*) FROM candles") == 0


async def test_one_invalid_symbol_blocks_all_inserts(db_conn):
    """Two-phase rule: validation of every symbol precedes the first insert."""
    end = _now_floor()
    provider = FakeHistoryProvider({
        "BTCUSDT": _sparse_history("BTCUSDT", 90, end),   # valid
        "ETHUSDT": _sparse_history("ETHUSDT", 3, end),    # invalid
    })
    with pytest.raises(RuntimeError, match="ETHUSDT"):
        await bootstrap_history(provider, db_conn, ["BTCUSDT", "ETHUSDT"])
    assert await db_conn.fetchval("SELECT count(*) FROM candles") == 0


# ------------------------------------------------------- persistence + routing


async def test_successful_bootstrap_persists_values_exactly(db_conn):
    end = _now_floor()
    history = _sparse_history("BTCUSDT", 30, end)
    provider = FakeHistoryProvider({"BTCUSDT": history})
    inserted = await bootstrap_history(
        provider, db_conn, ["BTCUSDT"], target_days=30, min_days=20)
    assert inserted == {"BTCUSDT": 30}

    rows = await db_conn.fetch(
        "SELECT symbol, tf, ts, o, h, l, c, v, qv, n_trades, taker_buy_v"
        " FROM candles ORDER BY ts")
    assert len(rows) == 30
    first, expected = rows[0], history[0]
    assert (first["symbol"], first["tf"], first["ts"]) == ("BTCUSDT", "1m", expected.ts)
    assert (float(first["o"]), float(first["taker_buy_v"])) == (expected.o, 0.4)
    assert first["n_trades"] == 3


async def test_partitions_created_and_rows_routed_by_month(db_conn):
    end = _now_floor()
    history = _sparse_history("BTCUSDT", 45, end)          # spans >= 2 months
    provider = FakeHistoryProvider({"BTCUSDT": history})
    await bootstrap_history(
        provider, db_conn, ["BTCUSDT"], target_days=45, min_days=20)

    routed = await db_conn.fetch(
        "SELECT DISTINCT tableoid::regclass::text AS part,"
        " to_char(ts AT TIME ZONE 'UTC', 'YYYY_MM') AS month"
        " FROM candles ORDER BY part")
    assert len(routed) >= 2                                # multiple monthly partitions
    for row in routed:
        assert row["part"] == f"candles_{row['month']}"    # each row in its month


async def test_deterministic_same_input_same_table_contents(db_dsn):
    import asyncpg

    end = _now_floor()
    provider = FakeHistoryProvider({"BTCUSDT": _sparse_history("BTCUSDT", 25, end)})

    async def run() -> list[tuple]:
        conn = await asyncpg.connect(db_dsn)
        tx = conn.transaction()
        await tx.start()
        try:
            await bootstrap_history(
                provider, conn, ["BTCUSDT"], target_days=25, min_days=20)
            rows = await conn.fetch("SELECT * FROM candles ORDER BY symbol, tf, ts")
            return [tuple(r) for r in rows]
        finally:
            await tx.rollback()
            await conn.close()

    assert await run() == await run()


# ----------------------------------------------------------------- row mapping


def test_candle_to_row_matches_insert_column_order():
    ts = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)
    c = _candle("BTCUSDT", ts, o=67200.0)
    assert candle_to_row(c) == (
        "BTCUSDT", "1m", ts, 67200.0, 67201.0, 67199.0, 67200.5,
        1.0, 67200.0, 3, 0.4,
    )

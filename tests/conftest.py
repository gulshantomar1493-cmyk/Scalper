"""Shared fixtures for the MarketScalper test suite (roadmap P0.3 + P0.8).

Config tests never touch the real backend/ config files and never inherit
MARKETSCALPER_* variables from the outer environment.

Database tests (P0.8) run against the existing local PostgreSQL development
database addressed by MARKETSCALPER_DB_DSN:
  * DSN unset       -> database tests SKIP (suite stays runnable anywhere).
  * schema missing  -> tests FAIL with instructions to apply migrations
                       001/002 first. The suite NEVER applies migrations or
                       modifies database structure itself.
  * every test runs inside a transaction that is rolled back, so no test
    leaves persistent data behind.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
import pytest

_REQUIRED_TABLES = ("candles", "pivots", "levels", "signals", "recommendations", "journal")


class TxPool:
    """Pool-shaped test helper handing out the rollback-wrapped test
    connection, so component inserts stay inside the test transaction.

    Each acquire() opens a SAVEPOINT (nested transaction): a failed statement
    rolls back only its own savepoint — exactly like production, where every
    acquire gets a fresh pool connection — and the outer test transaction
    still rolls everything back at the end. Shared by writer/API tests."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        tx = self._conn.transaction()  # savepoint under the test transaction
        await tx.start()
        try:
            yield self._conn
        except BaseException:
            await tx.rollback()
            raise
        else:
            await tx.commit()

# Minimal mirror of backend/config.example.yaml (layer 1) for isolated tests.
EXAMPLE_YAML = """\
app:
  log_level: INFO
  log_dir: logs
symbols:
  - BTCUSDT
  - ETHUSDT
timeframes:
  - 1m
  - 5m
database:
  dsn: ""
"""


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """DSN of the prepared local development database.

    Skips all database tests when MARKETSCALPER_DB_DSN is unset. Fails loudly
    when the schema is missing — migration execution is NOT this suite's job.
    """
    dsn = os.environ.get("MARKETSCALPER_DB_DSN")
    if not dsn:
        pytest.skip("MARKETSCALPER_DB_DSN not set — database tests skipped")

    async def _inspect() -> tuple[list[str], bool]:
        conn = await asyncpg.connect(dsn)
        try:
            missing = [
                t for t in _REQUIRED_TABLES
                if await conn.fetchval("SELECT to_regclass($1)", t) is None
            ]
            has_fn = bool(await conn.fetchval(
                "SELECT count(*) FROM pg_proc WHERE proname = 'ensure_candle_partitions'"
            ))
            return missing, has_fn
        finally:
            await conn.close()

    missing, has_fn = asyncio.run(_inspect())
    if missing or not has_fn:
        pytest.fail(
            f"Development database schema incomplete (missing tables: {missing or 'none'};"
            f" ensure_candle_partitions: {'present' if has_fn else 'MISSING'})."
            " Apply the migrations first — this suite never modifies database structure:\n"
            '  psql "$MARKETSCALPER_DB_DSN" -f database/migrations/001_candles_up.sql\n'
            '  psql "$MARKETSCALPER_DB_DSN" -f database/migrations/002_analysis_and_journal_up.sql'
        )
    return dsn


@pytest.fixture
async def db_conn(db_dsn):
    """One connection per test, wrapped in a transaction that always rolls
    back — deterministic, independent, leaves no persistent data behind."""
    conn = await asyncpg.connect(db_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Isolated config directory containing only the example layer.

    Clears every MARKETSCALPER_* env var so the env layer starts empty;
    individual tests set exactly the variables they assert on.
    """
    for var in list(os.environ):
        if var.startswith("MARKETSCALPER_"):
            monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.example.yaml").write_text(EXAMPLE_YAML, encoding="utf-8")
    return tmp_path

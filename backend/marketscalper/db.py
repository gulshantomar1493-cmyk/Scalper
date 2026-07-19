"""Async database access layer (roadmap P0.7). asyncpg, plain parameterized SQL.

Policy-free by design: this module exposes plain database operations only —
no duplicate-handling policy, no lifecycle rules, no business logic, no
caching, no ORM. Callers own transactions (pass a connection or acquire one
from the pool) and own every policy decision. Errors (e.g. unique-key or
foreign-key violations) propagate as asyncpg exceptions.

Append-only guard (Architecture §3, assigned to this layer by the roadmap),
implemented structurally:
  * signals: INSERT + SELECT only. No update function exists in this module.
  * recommendations: core columns are written once at insert; the ONLY
    mutations exposed are update_recommendation_status() and
    update_recommendation_eval(), each touching exactly its own columns.
  * journal: insert_journal_seed() writes the AUTO context columns;
    update_journal_manual() touches only the MANUAL outcome columns.

jsonb parameters/results are plain JSON strings (asyncpg default mapping).
timestamptz parameters are timezone-aware datetimes supplied by the caller.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

# --------------------------------------------------------------------------- pool


async def create_pool(dsn: str, *, min_size: int = 2,
                      max_size: int = 10) -> asyncpg.Pool:
    """Create the process-wide connection pool for the configured DSN.

    Sizing is tuned for the single-user (multi-device) production profile:
    a small floor keeps idle PostgreSQL backends — and VPS memory — low,
    while the ceiling leaves burst headroom for the concurrent WS push, the
    chart/analytics reads, and the background rollover/watchdog tasks. asyncpg
    would otherwise pin 10 connections open at all times."""
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)


# --------------------------------------------------------------- partitions (001)


async def ensure_partitions(
    conn: asyncpg.Connection,
    p_from: datetime | None = None,
    months_ahead: int | None = None,
) -> int:
    """Invoke migration 001's ensure_candle_partitions(); returns created count.

    With no arguments the SQL function's own defaults apply (current + next
    month, UTC). Passing p_from/months_ahead forwards them (bootstrap case).
    """
    if p_from is None and months_ahead is None:
        return await conn.fetchval("SELECT ensure_candle_partitions()")
    if p_from is None or months_ahead is None:
        raise ValueError("pass both p_from and months_ahead, or neither")
    return await conn.fetchval(
        "SELECT ensure_candle_partitions($1, $2)", p_from, months_ahead
    )


# ------------------------------------------------------------------ candles (001)

_CANDLE_COLS = "symbol, tf, ts, o, h, l, c, v, qv, n_trades, taker_buy_v"

_INSERT_CANDLES = f"""
INSERT INTO candles ({_CANDLE_COLS})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""


async def insert_candles(conn: asyncpg.Connection, rows: list[tuple]) -> None:
    """Batch-insert candle rows (tuples in _CANDLE_COLS order). Plain INSERT:
    duplicates raise asyncpg.UniqueViolationError — duplicate handling is the
    caller's policy, not this layer's."""
    await conn.executemany(_INSERT_CANDLES, rows)


async def select_candles(
    conn: asyncpg.Connection,
    symbol: str,
    tf: str,
    start: datetime,
    end: datetime,
) -> list[asyncpg.Record]:
    """Candles for [start, end), ordered by ts ascending."""
    return await conn.fetch(
        f"SELECT {_CANDLE_COLS} FROM candles"
        " WHERE symbol = $1 AND tf = $2 AND ts >= $3 AND ts < $4"
        " ORDER BY ts",
        symbol, tf, start, end,
    )


async def select_candle_coverage(
    conn: asyncpg.Connection, symbol: str, tf: str
) -> asyncpg.Record:
    """Earliest/latest stored candle + row count for a symbol/tf — the ops
    dashboard's data-coverage / backfill readout. Read-only."""
    return await conn.fetchrow(
        "SELECT min(ts) AS earliest, max(ts) AS latest, count(*) AS n"
        " FROM candles WHERE symbol = $1 AND tf = $2",
        symbol, tf,
    )


# ------------------------------------------------------------------- pivots (002)


async def insert_pivot(
    conn: asyncpg.Connection,
    *,
    symbol: str,
    tf: str,
    ts: datetime,
    confirmed_ts: datetime,
    kind: str,
    price,
    label: str | None,
) -> int:
    """Insert one confirmed pivot; returns its id."""
    return await conn.fetchval(
        "INSERT INTO pivots (symbol, tf, ts, confirmed_ts, kind, price, label)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        symbol, tf, ts, confirmed_ts, kind, price, label,
    )


async def select_pivots(
    conn: asyncpg.Connection, symbol: str, tf: str
) -> list[asyncpg.Record]:
    """All pivots for a symbol/tf, ordered by ts ascending."""
    return await conn.fetch(
        "SELECT id, symbol, tf, ts, confirmed_ts, kind, price, label"
        " FROM pivots WHERE symbol = $1 AND tf = $2 ORDER BY ts",
        symbol, tf,
    )


# ------------------------------------------------------------------- levels (002)


async def insert_level(
    conn: asyncpg.Connection,
    *,
    symbol: str,
    tf: str,
    kind: str,
    p1,
    p2,
    t1: datetime | None = None,
    t2: datetime | None = None,
    slope=None,
    created_ts: datetime | None = None,
) -> int:
    """Insert one level/zone/trendline; returns its id.
    touches/status take their schema defaults (0 / 'active')."""
    return await conn.fetchval(
        "INSERT INTO levels (symbol, tf, kind, p1, p2, t1, t2, slope, created_ts)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
        symbol, tf, kind, p1, p2, t1, t2, slope, created_ts,
    )


async def update_level_lifecycle(
    conn: asyncpg.Connection,
    level_id: int,
    *,
    touches: int,
    status: str,
    status_ts: datetime,
) -> None:
    """Update exactly the mutable lifecycle columns of one level
    (touches, status, status_ts — Architecture §3)."""
    await conn.execute(
        "UPDATE levels SET touches = $2, status = $3, status_ts = $4 WHERE id = $1",
        level_id, touches, status, status_ts,
    )


async def select_levels(
    conn: asyncpg.Connection, symbol: str, tf: str
) -> list[asyncpg.Record]:
    """All levels for a symbol/tf, ordered by id ascending."""
    return await conn.fetch(
        "SELECT id, symbol, tf, kind, p1, p2, t1, t2, slope, touches, status,"
        " created_ts, status_ts"
        " FROM levels WHERE symbol = $1 AND tf = $2 ORDER BY id",
        symbol, tf,
    )


# ------------------------------------------------------------------ signals (002)
# Append-only: INSERT + SELECT only. No update function exists, by design.


async def insert_signal(
    conn: asyncpg.Connection,
    *,
    ts: datetime,
    symbol: str,
    tf: str,
    strategy: str,
    direction: str,
    score,
    gates: str | None,
    components: str | None,
    state_snapshot: str | None,
    engine_version: str,
) -> int:
    """Insert one immutable signal row; returns its id.
    gates/components/state_snapshot are JSON strings (jsonb columns)."""
    return await conn.fetchval(
        "INSERT INTO signals (ts, symbol, tf, strategy, direction, score,"
        " gates, components, state_snapshot, engine_version)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
        ts, symbol, tf, strategy, direction, score,
        gates, components, state_snapshot, engine_version,
    )


async def select_signal(
    conn: asyncpg.Connection, signal_id: int
) -> asyncpg.Record | None:
    """One signal by id, or None."""
    return await conn.fetchrow(
        "SELECT id, ts, symbol, tf, strategy, direction, score,"
        " gates, components, state_snapshot, engine_version"
        " FROM signals WHERE id = $1",
        signal_id,
    )


# ---------------------------------------------------------- recommendations (002)
# Core columns are written once at insert. The only mutations exposed are the
# two functions below, each touching exactly its own column set (§3).


async def insert_recommendation(
    conn: asyncpg.Connection,
    *,
    signal_id: int,
    ts: datetime,
    direction: str,
    entry_px,
    sl,
    tp1,
    tp2,
    suggested_qty,
    risk_amt,
    est_fees,
    net_rr_tp1,
) -> int:
    """Insert one recommendation (status defaults to 'active'); returns its id."""
    return await conn.fetchval(
        "INSERT INTO recommendations (signal_id, ts, direction, entry_px, sl,"
        " tp1, tp2, suggested_qty, risk_amt, est_fees, net_rr_tp1)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
        signal_id, ts, direction, entry_px, sl, tp1, tp2,
        suggested_qty, risk_amt, est_fees, net_rr_tp1,
    )


async def update_recommendation_status(
    conn: asyncpg.Connection,
    rec_id: int,
    *,
    status: str,
    status_ts: datetime,
    status_reason: str | None,
) -> None:
    """Transition exactly the status columns (status, status_ts, status_reason)."""
    await conn.execute(
        "UPDATE recommendations SET status = $2, status_ts = $3, status_reason = $4"
        " WHERE id = $1",
        rec_id, status, status_ts, status_reason,
    )


async def update_recommendation_eval(
    conn: asyncpg.Connection,
    rec_id: int,
    *,
    eval_outcome: str,
    eval_r,
    eval_mae,
    eval_mfe,
) -> None:
    """Write exactly the hypothetical-evaluator columns (eval_outcome/r/mae/mfe)."""
    await conn.execute(
        "UPDATE recommendations SET eval_outcome = $2, eval_r = $3,"
        " eval_mae = $4, eval_mfe = $5 WHERE id = $1",
        rec_id, eval_outcome, eval_r, eval_mae, eval_mfe,
    )


async def select_recommendation(
    conn: asyncpg.Connection, rec_id: int
) -> asyncpg.Record | None:
    """One recommendation by id, or None."""
    return await conn.fetchrow(
        "SELECT id, signal_id, ts, direction, entry_px, sl, tp1, tp2,"
        " suggested_qty, risk_amt, est_fees, net_rr_tp1,"
        " status, status_ts, status_reason,"
        " eval_outcome, eval_r, eval_mae, eval_mfe"
        " FROM recommendations WHERE id = $1",
        rec_id,
    )


# ------------------------------------------------------------------ journal (002)
# AUTO context is written once by insert_journal_seed(); MANUAL outcome fields
# are written by update_journal_manual() — each touches only its own columns.


async def insert_journal_seed(
    conn: asyncpg.Connection,
    *,
    recommendation_id: int,
    reason_text: str | None,
    chart_snapshot_path: str | None,
    rule_violations: str | None,
) -> None:
    """Insert the journal row with its AUTO context columns (one per
    recommendation — enforced by the table's PK)."""
    await conn.execute(
        "INSERT INTO journal (recommendation_id, reason_text,"
        " chart_snapshot_path, rule_violations)"
        " VALUES ($1, $2, $3, $4)",
        recommendation_id, reason_text, chart_snapshot_path, rule_violations,
    )


async def update_journal_manual(
    conn: asyncpg.Connection,
    recommendation_id: int,
    *,
    taken: bool | None,
    result: str | None,
    actual_entry,
    actual_exit,
    actual_pnl,
    actual_r,
    notes: str | None,
    tags: list[str] | None,
) -> None:
    """Write exactly the MANUAL outcome columns of one journal row."""
    await conn.execute(
        "UPDATE journal SET taken = $2, result = $3, actual_entry = $4,"
        " actual_exit = $5, actual_pnl = $6, actual_r = $7, notes = $8, tags = $9"
        " WHERE recommendation_id = $1",
        recommendation_id, taken, result, actual_entry, actual_exit,
        actual_pnl, actual_r, notes, tags,
    )


async def select_journal(
    conn: asyncpg.Connection, recommendation_id: int
) -> asyncpg.Record | None:
    """One journal row by recommendation_id, or None."""
    return await conn.fetchrow(
        "SELECT recommendation_id, reason_text, chart_snapshot_path, taken,"
        " result, actual_entry, actual_exit, actual_pnl, actual_r,"
        " rule_violations, notes, tags"
        " FROM journal WHERE recommendation_id = $1",
        recommendation_id,
    )

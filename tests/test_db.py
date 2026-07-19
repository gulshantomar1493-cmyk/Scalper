"""Unit tests for the P0.7 database access layer (roadmap P0.8).

Three groups, per the roadmap task: schema round-trip, partition routing,
append-only enforcement. Runs against the prepared local development
database (see conftest.db_dsn); every test rolls back its transaction.
Expected-error statements are always the LAST database operation of their
test, since a failed statement aborts the surrounding transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from marketscalper import db

UTC = timezone.utc
T0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)


def _candle(ts, o=67000):
    return ("BTCUSDT", "1m", ts, o, o + 10, o - 10, o + 5, 12.5, 838125, 420, 7.1)


async def _seed_signal(conn) -> int:
    return await db.insert_signal(
        conn, ts=T0, symbol="BTCUSDT", tf="1m", strategy="S1", direction="LONG",
        score=88, gates='{"G1": true}', components='{"structure": 91}',
        state_snapshot='{}', engine_version="abc1234+structure=1",
    )


async def _seed_recommendation(conn) -> int:
    sid = await _seed_signal(conn)
    return await db.insert_recommendation(
        conn, signal_id=sid, ts=T0, direction="LONG", entry_px=67215, sl=67006,
        tp1=67450, tp2=67700, suggested_qty=0.05, risk_amt=50, est_fees=3.4,
        net_rr_tp1=1.7,
    )


# ------------------------------------------------------------------ pool


async def test_create_pool_connects_and_queries(db_dsn):
    pool = await db.create_pool(db_dsn)
    try:
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await pool.close()


# ------------------------------------------------- group 1: schema round-trip


async def test_candles_roundtrip_batch_insert_and_range_select(db_conn):
    rows = [_candle(T0 + timedelta(minutes=i), 67000 + i) for i in range(3)]
    await db.insert_candles(db_conn, rows)
    got = await db.select_candles(db_conn, "BTCUSDT", "1m", T0, T0 + timedelta(minutes=5))
    assert len(got) == 3
    assert [r["ts"] for r in got] == [r[2] for r in rows]          # ordered by ts
    assert float(got[0]["o"]) == 67000 and got[2]["n_trades"] == 420
    # half-open interval [start, end): end candle excluded
    got2 = await db.select_candles(db_conn, "BTCUSDT", "1m", T0, T0 + timedelta(minutes=2))
    assert len(got2) == 2


async def test_pivot_roundtrip(db_conn):
    pid = await db.insert_pivot(
        db_conn, symbol="BTCUSDT", tf="1m", ts=T0,
        confirmed_ts=T0 + timedelta(minutes=3), kind="H", price=67010, label="HH")
    rows = await db.select_pivots(db_conn, "BTCUSDT", "1m")
    assert [r["id"] for r in rows] == [pid]
    r = rows[0]
    assert (r["kind"], r["label"], float(r["price"])) == ("H", "HH", 67010)
    assert r["confirmed_ts"] - r["ts"] == timedelta(minutes=3)     # repaint audit pair


async def test_level_roundtrip_with_schema_defaults(db_conn):
    lid = await db.insert_level(
        db_conn, symbol="BTCUSDT", tf="1m", kind="TRENDLINE", p1=66900, p2=67050,
        t1=T0, t2=T0 + timedelta(minutes=2), slope=75.0, created_ts=T0)
    r = (await db.select_levels(db_conn, "BTCUSDT", "1m"))[0]
    assert r["id"] == lid and r["kind"] == "TRENDLINE"
    assert (float(r["p1"]), float(r["p2"]), float(r["slope"])) == (66900, 67050, 75.0)
    assert r["touches"] == 0 and r["status"] == "active"           # schema defaults


async def test_signal_roundtrip_jsonb_and_missing_id(db_conn):
    sid = await _seed_signal(db_conn)
    r = await db.select_signal(db_conn, sid)
    assert float(r["score"]) == 88 and r["strategy"] == "S1"
    assert r["gates"] == '{"G1": true}'                            # jsonb as str
    assert r["engine_version"] == "abc1234+structure=1"
    assert await db.select_signal(db_conn, sid + 100000) is None


async def test_recommendation_roundtrip_default_status_and_missing_id(db_conn):
    rid = await _seed_recommendation(db_conn)
    r = await db.select_recommendation(db_conn, rid)
    assert r["status"] == "active" and r["status_ts"] is None      # schema default
    assert (float(r["entry_px"]), float(r["net_rr_tp1"])) == (67215, 1.7)
    assert await db.select_recommendation(db_conn, rid + 100000) is None


async def test_journal_roundtrip_seed_then_manual(db_conn):
    rid = await _seed_recommendation(db_conn)
    await db.insert_journal_seed(
        db_conn, recommendation_id=rid, reason_text="rule trace",
        chart_snapshot_path="/snaps/1.png", rule_violations=None)
    await db.update_journal_manual(
        db_conn, rid, taken=True, result="win", actual_entry=67210,
        actual_exit=67440, actual_pnl=11.5, actual_r=1.1,
        notes="clean", tags=["s1", "asia-low"])
    j = await db.select_journal(db_conn, rid)
    assert j["taken"] is True and j["result"] == "win"
    assert j["tags"] == ["s1", "asia-low"]                         # text[] round-trip
    assert await db.select_journal(db_conn, rid + 100000) is None


# ---------------------------------------------- group 2: partition routing


async def test_ensure_partitions_default_is_idempotent(db_conn):
    await db.ensure_partitions(db_conn)                            # may create on fresh month
    assert await db.ensure_partitions(db_conn) == 0                # second call: nothing new


async def test_ensure_partitions_creates_missing_historical_months(db_conn):
    start, months_ahead = datetime(2024, 1, 15, tzinfo=UTC), 2     # 2024_01..2024_03
    names = ["candles_2024_01", "candles_2024_02", "candles_2024_03"]
    missing_before = [
        n for n in names
        if await db_conn.fetchval("SELECT to_regclass($1)", n) is None
    ]
    created = await db.ensure_partitions(db_conn, start, months_ahead)
    assert created == len(missing_before)
    for n in names:
        assert await db_conn.fetchval("SELECT to_regclass($1)", n) is not None


async def test_ensure_partitions_rejects_half_supplied_args(db_conn):
    with pytest.raises(ValueError):
        await db.ensure_partitions(db_conn, T0, None)
    with pytest.raises(ValueError):
        await db.ensure_partitions(db_conn, None, 2)


async def test_candles_route_to_correct_month_partition(db_conn):
    await db.ensure_partitions(db_conn, datetime(2024, 1, 1, tzinfo=UTC), 1)
    jan = datetime(2024, 1, 20, 10, 0, tzinfo=UTC)
    boundary = datetime(2024, 2, 1, 0, 0, tzinfo=UTC)              # FROM-inclusive edge
    await db.insert_candles(db_conn, [_candle(jan), _candle(boundary)])
    routed = {
        r["ts"]: r["part"] for r in await db_conn.fetch(
            "SELECT ts, tableoid::regclass::text AS part FROM candles"
            " WHERE symbol = 'BTCUSDT' AND ts = ANY($1::timestamptz[])",
            [jan, boundary])
    }
    assert routed[jan] == "candles_2024_01"
    assert routed[boundary] == "candles_2024_02"                   # boundary belongs to next month


async def test_insert_into_month_without_partition_fails_loudly(db_conn):
    far = datetime(2030, 6, 15, tzinfo=UTC)
    if await db_conn.fetchval("SELECT to_regclass('candles_2030_06')") is not None:
        pytest.skip("candles_2030_06 unexpectedly exists in this dev database")
    with pytest.raises(asyncpg.PostgresError, match="no partition"):
        await db.insert_candles(db_conn, [_candle(far)])


# ------------------------------------------ group 3: append-only enforcement


def test_module_exposes_no_signal_mutation():
    offenders = [
        n for n in dir(db)
        if "signal" in n and any(v in n for v in ("update", "delete", "upsert"))
    ]
    assert offenders == []


def test_recommendation_mutations_are_exactly_status_and_eval():
    muts = sorted(
        n for n in dir(db)
        if n.startswith(("update_recommendation", "delete_recommendation"))
    )
    assert muts == ["update_recommendation_eval", "update_recommendation_status"]


async def test_status_transition_touches_only_status_columns(db_conn):
    rid = await _seed_recommendation(db_conn)
    before = dict(await db.select_recommendation(db_conn, rid))
    await db.update_recommendation_status(
        db_conn, rid, status="expired", status_ts=T0 + timedelta(minutes=15),
        status_reason="time window elapsed")
    after = dict(await db.select_recommendation(db_conn, rid))
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"status", "status_ts", "status_reason"}
    assert after["status"] == "expired"


async def test_eval_transition_touches_only_eval_columns(db_conn):
    rid = await _seed_recommendation(db_conn)
    before = dict(await db.select_recommendation(db_conn, rid))
    await db.update_recommendation_eval(
        db_conn, rid, eval_outcome="tp1", eval_r=1.7, eval_mae=-0.3, eval_mfe=1.9)
    after = dict(await db.select_recommendation(db_conn, rid))
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"eval_outcome", "eval_r", "eval_mae", "eval_mfe"}
    assert after["eval_outcome"] == "tp1" and float(after["eval_mfe"]) == 1.9


async def test_level_lifecycle_touches_only_lifecycle_columns(db_conn):
    lid = await db.insert_level(
        db_conn, symbol="BTCUSDT", tf="1m", kind="EQH", p1=67000, p2=67010,
        created_ts=T0)
    before = dict((await db.select_levels(db_conn, "BTCUSDT", "1m"))[0])
    await db.update_level_lifecycle(
        db_conn, lid, touches=3, status="swept", status_ts=T0 + timedelta(minutes=9))
    after = dict((await db.select_levels(db_conn, "BTCUSDT", "1m"))[0])
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"touches", "status", "status_ts"}


async def test_journal_manual_update_preserves_auto_columns(db_conn):
    rid = await _seed_recommendation(db_conn)
    await db.insert_journal_seed(
        db_conn, recommendation_id=rid, reason_text="trace",
        chart_snapshot_path="/snaps/2.png", rule_violations='{"revenge": false}')
    await db.update_journal_manual(
        db_conn, rid, taken=False, result=None, actual_entry=None,
        actual_exit=None, actual_pnl=None, actual_r=None, notes="skipped", tags=None)
    j = await db.select_journal(db_conn, rid)
    assert j["reason_text"] == "trace"                              # AUTO preserved
    assert j["chart_snapshot_path"] == "/snaps/2.png"               # AUTO preserved
    assert j["rule_violations"] == '{"revenge": false}'             # AUTO preserved
    assert j["taken"] is False and j["notes"] == "skipped"


async def test_duplicate_candle_propagates_unique_violation(db_conn):
    await db.insert_candles(db_conn, [_candle(T0)])
    with pytest.raises(asyncpg.UniqueViolationError):               # policy-free layer
        await db.insert_candles(db_conn, [_candle(T0)])


async def test_orphan_recommendation_propagates_fk_violation(db_conn):
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.insert_recommendation(
            db_conn, signal_id=999_999_999, ts=T0, direction="LONG", entry_px=1,
            sl=1, tp1=1, tp2=1, suggested_qty=1, risk_amt=1, est_fees=1, net_rr_tp1=1)


async def test_second_journal_seed_rejected_by_primary_key(db_conn):
    rid = await _seed_recommendation(db_conn)
    await db.insert_journal_seed(
        db_conn, recommendation_id=rid, reason_text="first",
        chart_snapshot_path=None, rule_violations=None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.insert_journal_seed(
            db_conn, recommendation_id=rid, reason_text="second",
            chart_snapshot_path=None, rule_violations=None)


async def test_create_pool_is_bounded(monkeypatch):
    """Single-user (multi-device) production tuning: the pool floors low (few
    idle PostgreSQL backends -> low VPS memory) and caps at a modest burst —
    never asyncpg's default of pinning 10 connections open at all times."""
    captured = {}

    async def fake_create_pool(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return "POOL"

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    pool = await db.create_pool("postgresql://example")
    assert pool == "POOL"
    assert captured["dsn"] == "postgresql://example"
    assert captured["min_size"] <= 2                     # low idle floor
    assert 2 <= captured["max_size"] <= 20               # modest burst cap
    assert captured["min_size"] <= captured["max_size"]

"""Tests for the multi-timeframe ChartService (D26/D27/D28, Phase 1).

Verifies: compute-on-read aggregation correctness (independent hand-fold),
1m/5m short-circuit from canonical rows, per-candle completeness (D28/rule 5),
calendar alignment (D27: UTC day / ISO-Monday week / calendar month),
closed-buckets-only (no-repaint), determinism (double-query identical),
validation, and the DB-first→provider-second 1m gap-fill (rule 6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import TxPool
from marketscalper import db
from marketscalper.core.chart_service import ChartService, TIMEFRAMES
from marketscalper.providers.base import Candle

UTC = timezone.utc
# July 2026 — the test DB has this monthly partition; 2026-07-13 is a Monday.
BASE = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
MONDAY = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def _rows(symbol, base, n, *, o0=100.0):
    """n deterministic 1m rows: o=o0+i, h=o+2, l=o-1, c=o+0.5, v=10."""
    out = []
    for i in range(n):
        o = o0 + i
        out.append((symbol, "1m", base + timedelta(minutes=i),
                    o, o + 2, o - 1, o + 0.5, 10.0, 1000.0, 5, 6.0))
    return out


async def _pool(db_conn):
    return TxPool(db_conn)


# ------------------------------------------------------ aggregation correctness


async def test_15m_aggregation_matches_hand_fold(db_conn):
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 60))
    cs = ChartService(await _pool(db_conn))
    res = await cs.get_chart("BTCUSDT", "15m", BASE, BASE + timedelta(hours=1))
    c = res["candles"]
    assert len(c) == 4                                   # 4 x 15m in an hour
    b0 = c[0]
    # 1m i=0..14: open=o[0]=100, high=max(o+2)=116, low=min(o-1)=99, close=o[14]+.5=114.5
    assert (b0["o"], b0["h"], b0["l"], b0["c"]) == (100.0, 116.0, 99.0, 114.5)
    assert b0["v"] == 150.0                              # 15 x 10
    assert b0["n"] == 15 and b0["complete"] is True
    assert res["metadata"]["aggregated"] is True
    assert res["metadata"]["source_tf"] == "1m"
    assert res["metadata"]["timeframe"] == "15m"
    assert res["metadata"]["count"] == 4
    assert res["overlays"] is None                       # engine-isolated (D26.7)


async def test_1h_and_4h_counts_and_open_close(db_conn):
    await db.insert_candles(db_conn, _rows("ETHUSDT", BASE, 240))   # 4 hours
    cs = ChartService(await _pool(db_conn))
    h1 = await cs.get_chart("ETHUSDT", "1h", BASE, BASE + timedelta(hours=4))
    assert len(h1["candles"]) == 4 and h1["candles"][0]["n"] == 60
    assert h1["candles"][0]["o"] == 100.0 and h1["candles"][0]["c"] == 159.5
    h4 = await cs.get_chart("ETHUSDT", "4h", BASE, BASE + timedelta(hours=4))
    assert len(h4["candles"]) == 1 and h4["candles"][0]["n"] == 240
    assert h4["candles"][0]["complete"] is True


# --------------------------------------------------------- 1m/5m short-circuit


async def test_1m_and_5m_served_from_stored_rows_not_aggregated(db_conn):
    # store BOTH a 1m and an independent 5m row; the service must return the
    # stored rows verbatim (never fold 1m into 5m).
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 5))
    await db.insert_candles(db_conn, [
        ("BTCUSDT", "5m", BASE, 42.0, 43.0, 41.0, 42.5, 99.0, 9.0, 7, 3.0)])
    cs = ChartService(await _pool(db_conn))
    one = await cs.get_chart("BTCUSDT", "1m", BASE, BASE + timedelta(minutes=5))
    assert one["metadata"]["aggregated"] is False
    assert one["metadata"]["source_tf"] == "1m"
    assert len(one["candles"]) == 5 and one["candles"][0]["complete"] is True
    five = await cs.get_chart("BTCUSDT", "5m", BASE, BASE + timedelta(minutes=5))
    assert five["metadata"]["aggregated"] is False
    assert len(five["candles"]) == 1
    assert five["candles"][0]["o"] == 42.0 and five["candles"][0]["c"] == 42.5  # stored, not folded


# ------------------------------------------------------------ completeness (D28)


async def test_incomplete_bucket_flagged_not_dropped(db_conn):
    # 14 of 15 minutes present in the first 15m bucket -> complete=False, n=14
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 14))
    cs = ChartService(await _pool(db_conn))
    res = await cs.get_chart("BTCUSDT", "15m", BASE, BASE + timedelta(minutes=15))
    assert len(res["candles"]) == 1                      # emitted, not dropped
    assert res["candles"][0]["n"] == 14
    assert res["candles"][0]["complete"] is False


# ------------------------------------------------------ closed-buckets-only (D26.6)


async def test_forming_trailing_bucket_excluded(db_conn):
    # 50 minutes of data; the 4th 15m bucket [45,60) is not fully closed at end=+50
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 50))
    cs = ChartService(await _pool(db_conn))
    res = await cs.get_chart("BTCUSDT", "15m", BASE, BASE + timedelta(minutes=50))
    assert len(res["candles"]) == 3                      # 0-15, 15-30, 30-45 only
    assert res["candles"][-1]["ts"] == (BASE + timedelta(minutes=30)).isoformat()


# ------------------------------------------------------ calendar alignment (D27)


async def test_1d_bucket_is_utc_midnight(db_conn):
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 1440))    # full UTC day
    cs = ChartService(await _pool(db_conn))
    res = await cs.get_chart("BTCUSDT", "1d", BASE, BASE + timedelta(days=1))
    assert len(res["candles"]) == 1
    ts = datetime.fromisoformat(res["candles"][0]["ts"])
    assert ts == BASE and ts.hour == 0                   # 00:00 UTC boundary
    assert res["candles"][0]["n"] == 1440 and res["candles"][0]["complete"] is True


async def test_1w_bucket_starts_monday_utc(db_conn):
    await db.insert_candles(db_conn, _rows("BTCUSDT", MONDAY, 60))    # Mon 00:00+
    cs = ChartService(await _pool(db_conn))
    res = await cs.get_chart("BTCUSDT", "1w", MONDAY, MONDAY + timedelta(days=7))
    assert len(res["candles"]) == 1
    ts = datetime.fromisoformat(res["candles"][0]["ts"])
    assert ts == MONDAY and ts.weekday() == 0            # ISO week = Monday
    assert res["candles"][0]["n"] == 60 and res["candles"][0]["complete"] is False


async def test_1M_month_bucket_and_case_distinct_from_1m(db_conn):
    await db.ensure_partitions(db_conn, datetime(2026, 8, 1, tzinfo=UTC), 1)
    await db.insert_candles(db_conn,
                            _rows("BTCUSDT", datetime(2026, 7, 1, tzinfo=UTC), 60))
    cs = ChartService(await _pool(db_conn))
    # "1M" == month (aggregated); "1m" == minute (stored) — case-sensitive
    res = await cs.get_chart("BTCUSDT", "1M", datetime(2026, 7, 1, tzinfo=UTC),
                             datetime(2026, 8, 1, tzinfo=UTC))
    assert res["metadata"]["aggregated"] is True
    ts = datetime.fromisoformat(res["candles"][0]["ts"])
    assert ts == datetime(2026, 7, 1, tzinfo=UTC) and ts.day == 1
    assert res["candles"][0]["n"] == 60 and res["candles"][0]["complete"] is False
    minute = await cs.get_chart("BTCUSDT", "1m",
                                datetime(2026, 7, 1, tzinfo=UTC),
                                datetime(2026, 7, 1, 1, tzinfo=UTC))
    assert minute["metadata"]["aggregated"] is False     # "1m" != "1M"


# ------------------------------------------------------------------ determinism


async def test_double_query_is_identical(db_conn):
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 240))
    cs = ChartService(await _pool(db_conn))
    a = await cs.get_chart("BTCUSDT", "1h", BASE, BASE + timedelta(hours=4))
    b = await cs.get_chart("BTCUSDT", "1h", BASE, BASE + timedelta(hours=4))
    assert a == b                                        # pure function of stored 1m


# ------------------------------------------------------------------- validation


async def test_validation_errors(db_conn):
    cs = ChartService(await _pool(db_conn))
    with pytest.raises(ValueError):
        await cs.get_chart("BTCUSDT", "3m", BASE, BASE + timedelta(hours=1))  # unknown tf
    with pytest.raises(ValueError):
        await cs.get_chart("BTCUSDT", "15m", BASE, BASE)                      # from==to
    assert set(TIMEFRAMES) == {"1m", "5m", "15m", "30m", "1h", "4h",
                               "1d", "1w", "1M"}


# ---------------------------------------------------------- gap-fill (rule 6)


class _FakeProvider:
    """Duck-typed FeedProvider for gap-fill: serves canonical 1m for a range."""

    def __init__(self, candles):
        self._candles = candles
        self.calls = []
        self.capabilities = type("Caps", (), {"supports_historical_data": True})()

    async def fetch_historical_candles(self, symbol, tf, start, end):
        self.calls.append((symbol, tf, start, end))
        return [c for c in self._candles if start <= c.ts < end]


async def test_gapfill_fetches_only_canonical_1m_before_earliest(db_conn):
    # stored data begins at BASE+1h; the provider can supply the earlier hour
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE + timedelta(hours=1), 60))
    older = [Candle("BTCUSDT", "1m", BASE + timedelta(minutes=i),
                    200.0 + i, 202.0 + i, 199.0 + i, 200.5 + i, 10.0, 1000.0, 5, 6.0)
             for i in range(60)]
    provider = _FakeProvider(older)
    cs = ChartService(await _pool(db_conn), provider=provider)
    res = await cs.get_chart("BTCUSDT", "15m", BASE, BASE + timedelta(hours=2))
    # provider was asked for canonical 1m over the missing head range only
    assert provider.calls == [("BTCUSDT", "1m", BASE, BASE + timedelta(hours=1))]
    # the fetched hour is now aggregated in (4 buckets) alongside the stored hour
    assert len(res["candles"]) == 8                      # 2 hours x 4 x 15m
    assert res["candles"][0]["o"] == 200.0               # from the gap-filled data


async def test_gapfill_absent_provider_serves_stored_only(db_conn):
    await db.insert_candles(db_conn, _rows("BTCUSDT", BASE, 60))
    cs = ChartService(await _pool(db_conn))               # no provider
    res = await cs.get_chart("BTCUSDT", "15m", BASE - timedelta(hours=1),
                             BASE + timedelta(hours=1))
    assert len(res["candles"]) == 4                       # only the stored hour

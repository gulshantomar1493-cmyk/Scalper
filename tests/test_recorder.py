"""Tests for P3.18 — recommendation admission, SignalRecorder, D1 stamp.

Admission (D21.2) is a pure composition function tested directly on the
pipeline; persistence (D21.1/D21.6) runs against the real schema inside
the rolled-back test transaction (TxPool precedent).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from conftest import TxPool
from marketscalper.core.bus import EventBus
from marketscalper.core.recorder import SignalRecorder, engine_version_stamp
from marketscalper.core.state import StateStore
from marketscalper.engines.qualification import QualificationResult
from marketscalper.engines.strategy import Signal
from marketscalper.main import DEFAULT_EQUITY_USD, _StructurePipeline

UTC = timezone.utc
T0 = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _qual(verdict="TRADEABLE", score=80.0):
    components = (None if verdict == "NO_SIGNAL"
                  else {"structure": 80.0, "liquidity": 50.0,
                        "volume": 70.0, "momentum": 50.0})
    return QualificationResult(
        gates=(), data_integrity="PASS", components=components,
        score=None if verdict == "NO_SIGNAL" else score, verdict=verdict,
        aligned=8, evaluable=14, agreement="8 of 14 rules aligned",
        reasons=("✓ test",))


def _signal(entry=100.0, sl=99.0, tp1=102.0, tp2=103.5):
    return Signal("S1", "LONG", entry, sl, tp1, tp2, T0,
                  ("swept EQL (LOW)", "test fact"))


def _pipeline():
    return _StructurePipeline("BTCUSDT", StateStore(EventBus()))


# ------------------------------------------------------------- admission


def test_admit_creates_recommendation_with_plan_numbers():
    plan, rec = _pipeline()._admit(_signal(), _qual())
    assert plan.status == "suggested" and plan.rr_floor_ok
    # equity 10000 -> risk 50, r 1.0, qty 50, fee/unit 0.1, fees 5.0
    assert rec["qty"] == 50.0 and rec["risk_amt"] == 50.0
    assert abs(rec["est_fees"] - 5.0) < 1e-9
    assert rec["entry"] == 100.0 and rec["sl"] == 99.0
    assert rec["tp1"] == 102.0 and rec["tp2"] == 103.5
    assert rec["net_rr_tp1"] == plan.net_rr_tp1 > 1.0
    assert rec["score"] == 80.0 and rec["verdict"] == "TRADEABLE"
    assert rec["strategy"] == "S1" and rec["direction"] == "LONG"
    assert rec["created_ts"] == T0.isoformat()
    assert len(rec["guidance"]) == 4               # §7 display-only lines


def test_admit_a_plus_admits_too():
    _, rec = _pipeline()._admit(_signal(), _qual("A_PLUS", 90.0))
    assert rec is not None and rec["verdict"] == "A_PLUS"


def test_admit_refuses_below_threshold_and_no_signal():
    pipe = _pipeline()
    plan, rec = pipe._admit(_signal(), _qual("BELOW_THRESHOLD", 60.0))
    assert plan.status == "suggested" and rec is None   # plan fine, §6 not
    plan, rec = pipe._admit(_signal(), _qual("NO_SIGNAL"))
    assert rec is None


def test_admit_refuses_planner_rejects_and_rr_floor():
    pipe = _pipeline()
    # net RR(TP1) < 1.0 after fees -> plan rejected -> no recommendation
    plan, rec = pipe._admit(_signal(tp1=100.9, tp2=None), _qual())
    assert plan.status == "rejected" and rec is None
    # tp1 floor passes (net RR ~1.05) but tp2 floor fails (< 1.5) ->
    # suggested plan with rr_floor_ok False -> D21.2 refuses (G6)
    plan, rec = pipe._admit(_signal(tp1=101.255, tp2=101.53), _qual())
    assert plan.status == "suggested" and plan.rr_floor_ok is False
    assert rec is None


def test_drain_records_empties_and_returns():
    pipe = _pipeline()
    sentinel = (_signal(), _qual(), None, None)
    pipe._records.append(sentinel)
    assert pipe.drain_records() == [sentinel]
    assert pipe.drain_records() == []              # emptied


# ------------------------------------------------------------- D1 stamp


def test_engine_version_stamp_format():
    stamp = engine_version_stamp()
    head, engines = stamp.split("+", 1)
    assert re.fullmatch(r"[0-9a-f]{4,}|nogit", head)
    parts = dict(p.split("=") for p in engines.split(";"))
    assert set(parts) == {"structure", "trendline", "liquidity",
                          "orderblock", "fvg", "volume", "momentum",
                          "confluence", "qualification", "risk",
                          "strategy"}
    assert all(v == "1" for v in parts.values())   # D1: all at version 1


# ----------------------------------------------------------- persistence


async def test_recorder_round_trip_signal_and_recommendation(db_conn):
    pipe = _pipeline()
    signal = _signal()
    qual = _qual()
    plan, rec = pipe._admit(signal, qual)
    payload = {"trend": "BEARISH", "signals": [{"strategy": "S1"}]}
    recorder = SignalRecorder(TxPool(db_conn), "abc1234+strategy=1")
    await recorder.record("BTCUSDT", [(signal, qual, plan, rec)], payload)
    assert recorder.signals_written == 1
    assert recorder.recommendations_written == 1
    assert recorder.failures == 0

    row = await db_conn.fetchrow(
        "SELECT * FROM signals ORDER BY id DESC LIMIT 1")
    assert row["symbol"] == "BTCUSDT" and row["tf"] == "1m"
    assert row["strategy"] == "S1" and row["direction"] == "LONG"
    assert float(row["score"]) == 80.0
    assert row["engine_version"] == "abc1234+strategy=1"
    gates = json.loads(row["gates"])
    assert gates["verdict"] == "TRADEABLE"
    assert gates["agreement"] == "8 of 14 rules aligned"
    assert json.loads(row["components"])["volume"] == 70.0
    assert json.loads(row["state_snapshot"]) == payload

    rec_row = await db_conn.fetchrow(
        "SELECT * FROM recommendations WHERE signal_id = $1", row["id"])
    assert rec_row is not None
    assert rec_row["status"] == "active"           # D21.2 / schema default
    assert float(rec_row["entry_px"]) == 100.0
    assert float(rec_row["sl"]) == 99.0
    assert float(rec_row["tp1"]) == 102.0
    assert float(rec_row["tp2"]) == 103.5
    assert float(rec_row["suggested_qty"]) == 50.0
    assert abs(float(rec_row["est_fees"]) - 5.0) < 1e-9


async def test_recorder_multi_record_one_bar(db_conn):
    """Two signals on one bar (S1+S3 shape) -> two signal rows, and only
    the admitted one gets a recommendation (per-record independence)."""
    pipe = _pipeline()
    s1, q1 = _signal(), _qual("TRADEABLE", 80.0)
    plan1, rec1 = pipe._admit(s1, q1)
    s3 = Signal("S3", "LONG", 100.0, 99.0, 100.9, None, T0,
                ("fake break of validated support line",))
    q3 = _qual("BELOW_THRESHOLD", 60.0)            # S3 not admitted
    plan3, rec3 = pipe._admit(s3, q3)
    assert rec1 is not None and rec3 is None
    recorder = SignalRecorder(TxPool(db_conn), "abc1234+strategy=1")
    await recorder.record("BTCUSDT",
                          [(s1, q1, plan1, rec1), (s3, q3, plan3, rec3)],
                          None)
    assert recorder.signals_written == 2
    assert recorder.recommendations_written == 1
    n_sig = await db_conn.fetchval(
        "SELECT count(*) FROM signals WHERE ts = $1", T0)
    assert n_sig == 2
    strategies = await db_conn.fetch(
        "SELECT strategy FROM signals WHERE ts = $1 ORDER BY strategy", T0)
    assert [r["strategy"] for r in strategies] == ["S1", "S3"]


async def test_recorder_signal_only_when_not_admitted(db_conn):
    pipe = _pipeline()
    signal = _signal()
    qual = _qual("BELOW_THRESHOLD", 60.0)
    plan, rec = pipe._admit(signal, qual)
    assert rec is None
    recorder = SignalRecorder(TxPool(db_conn), "abc1234+strategy=1")
    await recorder.record("BTCUSDT", [(signal, qual, plan, rec)], None)
    assert recorder.signals_written == 1
    assert recorder.recommendations_written == 0
    row = await db_conn.fetchrow(
        "SELECT * FROM signals ORDER BY id DESC LIMIT 1")
    assert row["state_snapshot"] is None
    n = await db_conn.fetchval(
        "SELECT count(*) FROM recommendations WHERE signal_id = $1",
        row["id"])
    assert n == 0


async def test_recorder_survives_database_failure(db_conn):
    class _BrokenPool:
        def acquire(self):
            raise RuntimeError("pool down")

    pipe = _pipeline()
    signal = _signal()
    qual = _qual()
    plan, rec = pipe._admit(signal, qual)
    recorder = SignalRecorder(_BrokenPool(), "nogit+strategy=1")
    await recorder.record("BTCUSDT", [(signal, qual, plan, rec)], None)
    assert recorder.failures == 1                  # logged, never raised
    assert recorder.signals_written == 0


def test_default_equity_matches_env_example():
    assert DEFAULT_EQUITY_USD == 10000.0           # D21.5 pin


def test_recommendation_payload_is_json_serializable():
    """D21.7: an admitted recommendation dict must survive the WS/state-
    snapshot JSON path (every leaf a primitive)."""
    _, rec = _pipeline()._admit(_signal(), _qual())
    payload = {"symbol": "BTCUSDT", "recommendations": [rec]}
    restored = json.loads(json.dumps(payload, sort_keys=True))
    assert restored["recommendations"][0]["strategy"] == "S1"
    assert restored["recommendations"][0]["tp2"] == 103.5
    assert isinstance(restored["recommendations"][0]["guidance"], list)


def test_admit_short_direction():
    """_admit is direction-blind; the SHORT plan mirror flows through."""
    short = Signal("S1", "SHORT", 100.0, 101.0, 98.0, 96.5, T0,
                   ("swept EQH (HIGH)",))
    plan, rec = _pipeline()._admit(short, _qual())
    assert rec is not None and rec["direction"] == "SHORT"
    assert rec["sl"] == 101.0 and rec["tp1"] == 98.0
    assert rec["net_rr_tp1"] == plan.net_rr_tp1 > 1.0


def test_equity_override_propagates_to_admission():
    """D21.5: the pipeline's equity scales the display-only qty/risk."""
    pipe = _StructurePipeline("BTCUSDT", StateStore(EventBus()),
                              equity=50000.0)
    _, rec = pipe._admit(_signal(), _qual())
    assert rec["risk_amt"] == 250.0                # 50000 * 0.5%
    assert rec["qty"] == 250.0                     # / r_per_unit 1.0

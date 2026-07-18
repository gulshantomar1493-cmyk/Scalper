"""P4 GATE — machinery-readiness verification (roadmap P4.14).

The roadmap P4 gate is a 2-week LIVE forward run (>=60 recommendations,
every evaluator outcome recorded, every taken trade manually logged) —
an OWNER-OPERATED campaign requiring real market data and calendar time
(deferred, the P1.22 / P2.24-manual / P3.21-rule-quality class; record:
docs/decisions/P4.14-gate.md).

What IS automatable, and gated here: the complete P4 machinery is ready
end-to-end. A single recommendation is driven through the ENTIRE chain
against the real schema — admission -> SignalRecorder (signal + rec +
journal seed rows) -> RecommendationLifecycle + evaluator -> persisted
status/eval -> manual journal log -> the analytics read-model -> the
daily stats snapshot — proving the pipeline the owner's live run relies
on is wired and correct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import TxPool
from marketscalper import db
from marketscalper.analytics import compute_analytics, journal_list
from marketscalper.core.bus import EventBus
from marketscalper.core.recorder import SignalRecorder
from marketscalper.core.state import StateStore
from marketscalper.engines.lifecycle import RecommendationLifecycle
from marketscalper.engines.qualification import QualificationResult
from marketscalper.engines.strategy import Signal
from marketscalper.main import _StructurePipeline
from marketscalper.ops import format_daily_summary
from marketscalper.providers.base import Candle

UTC = timezone.utc
T0 = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


def _candle(i, o, h, l, c):
    return Candle(symbol="BTCUSDT", tf="1m", ts=T0 + timedelta(minutes=i),
                  o=float(o), h=float(h), l=float(l), c=float(c),
                  v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)


def _qual():
    return QualificationResult(
        gates=(), data_integrity="PASS",
        components={"structure": 80.0, "liquidity": 90.0, "volume": 80.0,
                    "momentum": 100.0},
        score=88.0, verdict="A_PLUS", aligned=10, evaluable=14,
        agreement="10 of 14 rules aligned",
        reasons=("✓ established trend BULLISH (+30 structure)",
                 "✓ sweep of multi-touch pool (+40 liquidity)"))


async def test_p4_gate_end_to_end_machinery(db_conn):
    pool = TxPool(db_conn)
    recorder = SignalRecorder(pool, "gate+strategy=1")
    pipe = _StructurePipeline("BTCUSDT", StateStore(EventBus()))

    # 1) admission -> a real recommendation
    signal = Signal("S1", "LONG", 100.0, 99.0, 102.0, None, T0,
                    ("swept EQL (LOW)", "CHOCH within 3 candles"))
    qual = _qual()
    plan, rec = pipe._admit(signal, qual)
    assert rec is not None and qual.verdict == "A_PLUS"

    # 2) persist: signal + recommendation + journal seed
    await recorder.record("BTCUSDT", [(signal, qual, plan, rec)], None)
    assert recorder.signals_written == 1
    assert recorder.recommendations_written == 1
    assert recorder.journal_written == 1
    rec_id = rec["id"]
    assert rec_id is not None                       # written back (P4.7)

    # 3) lifecycle + evaluator: the entry fills, then TP1 -> evaluated
    lc = RecommendationLifecycle("BTCUSDT")
    lc.on_recommendation(rec, _candle(0, 101, 101.2, 100.6, 101))
    events = []
    events += lc.update(_candle(1, 100.5, 100.6, 99.8, 100.2))   # fill 100
    events += lc.update(_candle(2, 100.2, 102.3, 100.1, 102.1))  # TP1
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "evaluated" and ev.outcome.outcome == "tp1"
    assert ev.outcome.eval_r == 2.0

    # 4) persist the transition (status + eval_*)
    await recorder.record_lifecycle("BTCUSDT", events)
    assert recorder.lifecycle_written == 1
    row = await db.select_recommendation(db_conn, rec_id)
    assert row["status"] == "evaluated"
    assert row["eval_outcome"] == "tp1" and float(row["eval_r"]) == 2.0

    # 5) manual log: the owner took it and won (actual R just under hyp)
    await db.update_journal_manual(
        db_conn, rec_id, taken=True, result="win", actual_entry=100.1,
        actual_exit=101.9, actual_pnl=180.0, actual_r=1.8, notes="clean",
        tags=["A+", "sweep"])

    # 6) analytics read-model reflects the full chain
    a = await compute_analytics(db_conn)
    assert a["n_recommendations"] == 1
    s1 = a["by_strategy"]["S1"]
    assert s1["hypothetical"]["wins"] == 1
    assert abs(s1["hypothetical"]["expectancy"] - 2.0) < 1e-9
    assert s1["manual"]["n_taken"] == 1 and s1["manual"]["wins"] == 1
    assert abs(s1["manual"]["expectancy"] - 1.8) < 1e-9
    # system-vs-actual: the owner captured 1.8R vs the 2.0R hypothetical
    sva = s1["system_vs_actual"]
    assert sva["n"] == 1 and abs(sva["delta"] - (-0.2)) < 1e-9
    # LONDON session (09:00 UTC)
    assert "LONDON" in a["by_session"]

    # 7) the daily stats snapshot renders the day's line
    summary = format_daily_summary(a)
    assert "daily stats snapshot: 1 recommendation" in summary
    assert "S1: n=1" in summary and "hyp_exp=+2.00R" in summary

    # 8) the journal tab list surfaces it with the rule-trace + outcomes
    listing = await journal_list(db_conn, 10)
    assert len(listing) == 1
    j = listing[0]
    assert j["id"] == rec_id and j["status"] == "evaluated"
    assert j["eval_outcome"] == "tp1" and j["result"] == "win"
    assert j["tags"] == ["A+", "sweep"] and j["reason_text"].startswith("LONG")


async def test_p4_gate_skipped_trade_excluded_from_manual(db_conn):
    """A recommendation the owner SKIPPED is in the hypothetical stats but
    not the manual results (the system-vs-actual gap the campaign studies)."""
    pool = TxPool(db_conn)
    recorder = SignalRecorder(pool, "gate+strategy=1")
    pipe = _StructurePipeline("BTCUSDT", StateStore(EventBus()))
    signal = Signal("S2", "LONG", 100.0, 99.0, 102.0, None, T0, ("f",))
    qual = _qual()
    plan, rec = pipe._admit(signal, qual)
    await recorder.record("BTCUSDT", [(signal, qual, plan, rec)], None)
    lc = RecommendationLifecycle("BTCUSDT")
    lc.on_recommendation(rec, _candle(0, 101, 101.2, 100.6, 101))
    lc.update(_candle(1, 100.5, 100.6, 99.8, 100.2))
    events = lc.update(_candle(2, 100.2, 100.3, 98.9, 99.0))    # SL
    await recorder.record_lifecycle("BTCUSDT", events)
    await db.update_journal_manual(
        db_conn, rec["id"], taken=False, result=None, actual_entry=None,
        actual_exit=None, actual_pnl=None, actual_r=None, notes="skipped it",
        tags=None)
    a = await compute_analytics(db_conn)
    s2 = a["by_strategy"]["S2"]
    assert s2["hypothetical"]["losses"] == 1        # hypothetically a loss
    assert s2["manual"]["n_taken"] == 0             # never taken
    assert s2["system_vs_actual"]["n"] == 0         # not in the comparison

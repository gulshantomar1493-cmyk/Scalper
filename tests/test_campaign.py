"""Tests for the validation-campaign tooling (§11 P5.5 + P5.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import TxPool
from marketscalper import db
from marketscalper.campaign import (
    TRUSTED_MIN_RECOMMENDATIONS,
    data_quality_audit,
    expectancy_report,
)

UTC = timezone.utc
M0 = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


# ------------------------------------------------ expectancy report (P5.7)


def _analytics(strategies):
    by = {}
    for name, (n, hyp_exp, man_exp, wr, delta) in strategies.items():
        by[name] = {"n": n,
                    "hypothetical": {"expectancy": hyp_exp, "win_rate": wr},
                    "manual": {"expectancy": man_exp},
                    "system_vs_actual": {"delta": delta}}
    return {"n_recommendations": sum(v[0] for v in strategies.values()),
            "by_strategy": by}


def test_expectancy_report_trusted_eligibility():
    rep = expectancy_report(_analytics({
        "S1": (250, 0.35, 0.30, 0.62, -0.05),   # enough + positive -> eligible
        "S2": (300, -0.10, -0.2, 0.45, -0.1),   # enough but NEGATIVE
        "S3": (50, 0.80, 0.7, 0.7, 0.0),        # positive but too few
    }))
    assert rep["trusted_threshold"] == TRUSTED_MIN_RECOMMENDATIONS
    s = rep["strategies"]
    assert s["S1"]["trusted_eligible"] is True
    assert s["S2"]["sample_sufficient"] and not s["S2"]["positive_after_fees"]
    assert s["S2"]["trusted_eligible"] is False
    assert s["S3"]["positive_after_fees"] and not s["S3"]["sample_sufficient"]
    assert s["S3"]["trusted_eligible"] is False
    assert rep["any_trusted_eligible"] is True      # S1


def test_expectancy_report_none_expectancy_not_positive():
    rep = expectancy_report(_analytics({"S1": (250, None, None, None, None)}))
    assert rep["strategies"]["S1"]["positive_after_fees"] is False
    assert rep["any_trusted_eligible"] is False


def test_expectancy_report_empty():
    rep = expectancy_report({"n_recommendations": 0, "by_strategy": {}})
    assert rep["strategies"] == {} and rep["any_trusted_eligible"] is False


# ------------------------------------------------ data-quality audit (P5.5)


async def _rec(db_conn, minute, status="evaluated", outcome="tp1",
               eval_r=2.0):
    ts = M0 + timedelta(minutes=minute)
    sig = await db.insert_signal(
        db_conn, ts=ts, symbol="BTCUSDT", tf="1m", strategy="S1",
        direction="LONG", score=80.0, gates=None, components=None,
        state_snapshot=None, engine_version="t")
    rid = await db.insert_recommendation(
        db_conn, signal_id=sig, ts=ts, direction="LONG", entry_px=100.0,
        sl=99.0, tp1=102.0, tp2=None, suggested_qty=1.0, risk_amt=50.0,
        est_fees=0.1, net_rr_tp1=1.7)
    if status != "active":
        await db.update_recommendation_status(
            db_conn, rid, status=status, status_ts=ts, status_reason="x")
    if outcome is not None:
        await db.update_recommendation_eval(
            db_conn, rid, eval_outcome=outcome, eval_r=eval_r,
            eval_mae=-0.3, eval_mfe=2.2)
    return rid


async def test_audit_clean_when_consistent(db_conn):
    await _rec(db_conn, 0, "evaluated", "tp1", 2.0)
    await _rec(db_conn, 5, "evaluated", "sl", -1.0)
    audit = await data_quality_audit(db_conn)
    assert audit["clean"] is True and audit["violations"] == []
    assert audit["n_recommendations"] == 2


async def test_audit_flags_eval_inconsistency(db_conn):
    # status 'evaluated' but no eval_outcome -> half-written row
    await _rec(db_conn, 0, "evaluated", outcome=None)
    audit = await data_quality_audit(db_conn)
    assert not audit["clean"]
    assert any("eval inconsistency" in v for v in audit["violations"])


async def test_audit_flags_stuck_active_recommendation(db_conn):
    # an old 'active' rec + a much newer rec -> the old one is stuck
    await _rec(db_conn, 0, "active", outcome=None)
    await _rec(db_conn, 600, "evaluated", "tp1", 2.0)   # 10h later
    audit = await data_quality_audit(db_conn)
    assert any("stuck recommendation" in v for v in audit["violations"])

"""Tests for the analytics read-model (§11 P4; roadmap P4.11).

The pure `aggregate`: win rate / avg R / expectancy for the manual
journal AND the hypothetical evaluator, the system-vs-actual delta, and
the overall / per-strategy / per-session breakdown — hand-computed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from marketscalper.analytics import _stats, aggregate

UTC = timezone.utc


def _row(strategy="S1", hour=9, outcome="tp1", eval_r=2.0, mae=-0.4,
         mfe=2.3, taken=True, result="win", actual_r=1.8):
    return {"strategy": strategy,
            "ts": datetime(2026, 7, 22, hour, 0, tzinfo=UTC),
            "eval_outcome": outcome, "eval_r": eval_r, "eval_mae": mae,
            "eval_mfe": mfe, "status": "evaluated", "taken": taken,
            "result": result, "actual_r": actual_r}


def test_hypothetical_win_rate_and_expectancy():
    rows = [_row(outcome="tp1", eval_r=2.0), _row(outcome="tp1", eval_r=2.0),
            _row(outcome="sl", eval_r=-1.0), _row(outcome="none", eval_r=0.3)]
    h = _stats(rows)["hypothetical"]
    assert h["n_evaluated"] == 3                     # 'none' excluded
    assert h["wins"] == 2 and h["losses"] == 1
    assert abs(h["win_rate"] - 2 / 3) < 1e-9
    # expectancy = mean eval_r over the 3 evaluated = (2+2-1)/3 = 1.0
    assert abs(h["expectancy"] - 1.0) < 1e-9
    assert abs(h["avg_r"] - 1.0) < 1e-9


def test_hypothetical_tp2_counts_as_win():
    h = _stats([_row(outcome="tp2", eval_r=5.0)])["hypothetical"]
    assert h["wins"] == 1 and h["win_rate"] == 1.0


def test_manual_win_rate_be_excluded_from_denominator():
    rows = [_row(result="win", actual_r=2.0), _row(result="loss", actual_r=-1.0),
            _row(result="be", actual_r=0.0), _row(result="win", actual_r=1.5)]
    m = _stats(rows)["manual"]
    assert m["n_taken"] == 4
    assert m["wins"] == 2 and m["losses"] == 1 and m["be"] == 1
    assert abs(m["win_rate"] - 2 / 3) < 1e-9         # BE not in denominator
    # expectancy = mean actual_r over taken = (2-1+0+1.5)/4 = 0.625
    assert abs(m["expectancy"] - 0.625) < 1e-9


def test_skipped_and_untaken_excluded_from_manual():
    rows = [_row(taken=False, result=None, actual_r=None),
            _row(taken=True, result="win", actual_r=2.0)]
    m = _stats(rows)["manual"]
    assert m["n_taken"] == 1 and m["wins"] == 1


def test_system_vs_actual_delta():
    # taken + evaluated, both R known: the user did worse than the system
    rows = [_row(eval_r=2.0, actual_r=1.5), _row(eval_r=-1.0, actual_r=-1.0)]
    sva = _stats(rows)["system_vs_actual"]
    assert sva["n"] == 2
    assert abs(sva["mean_eval_r"] - 0.5) < 1e-9      # (2-1)/2
    assert abs(sva["mean_actual_r"] - 0.25) < 1e-9   # (1.5-1)/2
    assert abs(sva["delta"] - (-0.25)) < 1e-9        # actual - eval


def test_system_vs_actual_needs_both_r():
    # a taken trade never hypothetically evaluated -> not in the comparison
    rows = [_row(outcome="none", eval_r=None, taken=True, actual_r=1.5)]
    sva = _stats(rows)["system_vs_actual"]
    assert sva["n"] == 0 and sva["delta"] is None


def test_empty_group_all_none():
    s = _stats([])
    assert s["n"] == 0
    assert s["hypothetical"]["win_rate"] is None
    assert s["manual"]["expectancy"] is None
    assert s["system_vs_actual"]["delta"] is None


def test_aggregate_by_strategy_and_session():
    rows = [
        _row(strategy="S1", hour=9, outcome="tp1", eval_r=2.0),   # LONDON
        _row(strategy="S1", hour=15, outcome="sl", eval_r=-1.0),  # NY
        _row(strategy="S2", hour=3, outcome="tp1", eval_r=3.0),   # ASIA
    ]
    a = aggregate(rows)
    assert a["n_recommendations"] == 3
    assert set(a["by_strategy"]) == {"S1", "S2"}
    assert a["by_strategy"]["S1"]["n"] == 2
    assert a["by_strategy"]["S2"]["hypothetical"]["wins"] == 1
    assert set(a["by_session"]) == {"ASIA", "LONDON", "NY"}
    assert a["by_session"]["LONDON"]["hypothetical"]["wins"] == 1
    assert a["by_session"]["NY"]["hypothetical"]["losses"] == 1
    # overall expectancy = mean eval_r = (2-1+3)/3
    assert abs(a["overall"]["hypothetical"]["expectancy"] - 4 / 3) < 1e-9
